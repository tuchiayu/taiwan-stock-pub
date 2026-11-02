"""ETF distributions per market and the held-ETF refresh (design 071).

Retry, circuit breaker and the shared upserts live in `sync.py`.
"""

import sqlite3
from collections import defaultdict
from datetime import date

import httpx2

from taiwan_stock.catalog import (
    delistings,
    frequency_inference,
    host_pool,
    sync,
    sync_history,
    tpex_adapter,
    twse_adapter,
)
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared import sync_progress
from taiwan_stock.shared.dates import now_utc_iso, today_taipei
from taiwan_stock.shared.ids import new_id

# Pacing measured live; see the notes above `_RETRY_BACKOFF_SECONDS` in sync.py.
_ETF_DISTRIBUTION_DELAY_SECONDS = 2.0


def upsert_etf_distribution(
    conn: sqlite3.Connection,
    security_id: str,
    fields: dict,
    *,
    source: str,
    fetched_at: str,
) -> str:
    """The ETF distribution list is authoritative for the dates/amount
    it explicitly provides (design 004 §4.6 point 6); re-syncing
    updates them directly rather than merging with a lower-priority
    source, since this is currently the only writer of ETF
    `distributions` rows (the stock ex-dividend notice table doesn't
    cover ETFs). A conflict match means the source confirmed this
    ex_dividend_date again, so any earlier `reconciliation_status`
    flag (design 009 §4.4) is cleared."""
    row = conn.execute(
        """
        INSERT INTO distributions
            (id, security_id, ex_dividend_date, record_date, pay_date,
             amount_per_share, amount_status, source, fetched_at)
        VALUES
            (:id, :security_id, :ex_dividend_date, :record_date, :pay_date,
             :amount_per_share, :amount_status, :source, :fetched_at)
        ON CONFLICT (security_id, ex_dividend_date) DO UPDATE SET
            record_date = excluded.record_date,
            pay_date = excluded.pay_date,
            amount_per_share = excluded.amount_per_share,
            amount_status = excluded.amount_status,
            source = excluded.source,
            fetched_at = excluded.fetched_at,
            reconciliation_status = NULL,
            reconciliation_note = NULL,
            reconciliation_detected_at = NULL
        RETURNING id
        """,
        {
            **fields,
            "id": new_id(),
            "security_id": security_id,
            "source": source,
            "fetched_at": fetched_at,
        },
    ).fetchone()
    return row["id"]


def _etf_distribution_change(
    conn: sqlite3.Connection, security_id: str, fields: dict, source: str
) -> str:
    """Classify business changes; fetched_at only records source verification."""
    existing = conn.execute(
        "SELECT * FROM distributions WHERE security_id=? AND ex_dividend_date=?",
        (security_id, fields["ex_dividend_date"]),
    ).fetchone()
    if existing is None:
        return "inserted"
    unchanged = all(
        existing[key] == fields[key]
        for key in ("record_date", "pay_date", "amount_per_share", "amount_status")
    )
    cleared_review = all(
        existing[key] is None
        for key in (
            "reconciliation_status",
            "reconciliation_note",
            "reconciliation_detected_at",
        )
    )
    return (
        "unchanged"
        if unchanged and existing["source"] == source and cleared_review
        else "updated"
    )


def _flag_stale_etf_distributions(
    conn: sqlite3.Connection,
    security_id: str,
    source: str,
    year: int,
    returned_dates: set[str],
    detected_at: str,
) -> None:
    """Design 009 §4.4: TWSE `etfDiv`/TPEx `etfExDiv` have no stable
    event ID (verified live 2026-09-20), so when a previously-synced
    ex_dividend_date for this (security, year) unit no longer appears
    in a resync, there is no reliable way to tell whether it was
    corrected to a different date or genuinely dropped — flag it for
    manual review rather than guessing at a replacement. The unit is
    always queried one full year at a time, so a caller reaching this
    function has a response already trusted as complete for `year`;
    already-flagged rows are left alone rather than re-flagged.
    """
    stale_rows = conn.execute(
        """
        SELECT id, ex_dividend_date FROM distributions
        WHERE security_id = ? AND source = ?
          AND ex_dividend_date LIKE ?
          AND reconciliation_status IS NULL
        """,
        (security_id, source, f"{year}-%"),
    ).fetchall()
    for row in stale_rows:
        if row["ex_dividend_date"] in returned_dates:
            continue
        conn.execute(
            """
            UPDATE distributions SET
                reconciliation_status = 'needs_review',
                reconciliation_note = ?,
                reconciliation_detected_at = ?
            WHERE id = ?
            """,
            (
                f"{year} 年度重新同步時，此除息日未再出現於來源回應",
                detected_at,
                row["id"],
            ),
        )


def _sync_etf_distributions_for_market(conn, client, **kwargs) -> dict:
    """One market on its own, with any fetcher; see `_etf_distribution_steps`."""
    return host_pool.run(_etf_distribution_steps(conn, client, **kwargs))


def _etf_distribution_steps(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    *,
    market: str,
    source: str,
    fetch_and_normalize_year,
    years: int,
    reference_year: int | None,
    retry_run_id: str | None = None,
    security_ids: set[str] | None = None,
    year_list: list[int] | None = None,
    year_map: dict[str, set[int]] | None = None,
):
    """Shared per-(security, year) sync loop behind `sync_etf_distributions`
    (TWSE) and `sync_tpex_etf_distributions` (TPEx) — design 004 §4.6:
    syncs the last `years` calendar years (default 3: this year and
    the prior 2) of distribution events for every local ETF in
    `market`. One HTTP request per (security, year), each committed in
    its own transaction (§4.6 points 4/9): a failure only loses that
    one unit's progress and is counted, it does not abort the whole
    run or roll back units that already succeeded, and a rerun can
    pick up where a prior run left off. This does not touch
    `watchlist` — syncing never adds a tracked security (§4.6 point 3).

    `fetch_and_normalize_year(client, code, year)` hides the two
    sources' genuinely different request shapes (TWSE: GET with a
    plain year; TPEx: POST with a `YYYYMMDD` range) behind one
    per-year call so this loop doesn't need to know which market it's
    running for beyond the `WHERE market = ?` filter and the `source`
    label recorded on each row.

    `security_ids`/`year_list` narrow the same loop to held/watched
    ETFs and recent years (design 016 §4.2) — still one full year per
    request, so `_flag_stale_etf_distributions` stays valid, and still
    the same `source`, so the source lock keeps it from writing at the
    same time as a full-catalog run.

    Design 053: a generator. Up to its `yield` it plans the units and
    queues every request on the market's host; after it, it writes each
    unit in the old order as its response arrives, so another market or
    sync can fetch in between (`host_pool.start`).
    """
    delistings.refresh_status(conn)
    if year_list is None:
        reference_year = reference_year or int(today_taipei()[:4])
        year_list = list(range(reference_year - (years - 1), reference_year + 1))
    fetched_at = now_utc_iso()
    etfs = [
        etf
        for etf in conn.execute(
            "SELECT id, code, status FROM securities "
            "WHERE market=? AND security_type='etf'",
            (market,),
        )
        if security_ids is None or etf["id"] in security_ids
    ]
    synced, skipped_no_ex_date = 0, 0
    changes = dict.fromkeys(("inserted", "updated", "unchanged"), 0)
    failed_units = []
    url = (
        twse_adapter.ETF_DISTRIBUTION_URL
        if market == "TWSE"
        else "https://info.tpex.org.tw/api/etfExDiv"
    )
    with sync_history.run(conn, source, retry_run_id) as task:
        work = [
            (etf, year)
            for etf in etfs
            for year in year_list
            if year_map is None or year in year_map.get(etf["id"], set())
            if task.wanted(f"{etf['code']}:{year}")
        ]
        for etf, year in work:
            task.plan(
                f"{etf['code']}:{year}",
                security_id=etf["id"],
                stage="distribution_year",
                period=str(year),
            )
        pool = host_pool.current.get()
        responses = {
            (etf["id"], year): pool.submit(
                market,
                lambda code=etf["code"], year=year: sync._fetch_with_retry(
                    fetch_and_normalize_year, client, code, year
                ),
                delay=_ETF_DISTRIBUTION_DELAY_SECONDS,
                breaker=(
                    "etf-distributions",
                    sync._CIRCUIT_BREAKER_THRESHOLD,
                    sync._is_blocking_error,
                ),
            )
            for etf, year in work
            if etf["status"] != "delisted"
        }
        yield
        for etf, year in work:
            sync_progress.checkpoint()
            if etf["status"] == "delisted":
                task.record(
                    f"{etf['code']}:{year}",
                    delistings.SKIPPED,
                    reason=delistings.skip_reason(market),
                )
                continue
            key = f"{etf['code']}:{year}"
            try:
                events = host_pool.wait(responses[(etf["id"], year)]).result()
            except host_pool.Skipped:
                break  # the circuit breaker: the rest stay pending
            except Exception as exc:
                task.record(key, "parse_failed", url=url, exc=exc)
                failed_units.append(key)
                continue
            try:
                if any(event.get("code") != etf["code"] for event in events):
                    raise ValueError("配息商品代碼不符")
                valid = [event for event in events if event["ex_dividend_date"]]
                if len(valid) != len(events):
                    raise ValueError("配息事件缺少有效除息日")
                # Design 062: the unit is one requested year, which the
                # stale-row flagging below also assumes.
                if any(
                    not event["ex_dividend_date"].startswith(f"{year}-")
                    for event in valid
                ):
                    raise ValueError("除息日不在查詢年度")
                unit_changes = dict.fromkeys(changes, 0)
                with write_transaction(conn):
                    for event in valid:
                        change = _etf_distribution_change(
                            conn, etf["id"], event, source
                        )
                        upsert_etf_distribution(
                            conn, etf["id"], event, source=source, fetched_at=fetched_at
                        )
                        unit_changes[change] += 1
                    _flag_stale_etf_distributions(
                        conn,
                        etf["id"],
                        source,
                        year,
                        {event["ex_dividend_date"] for event in valid},
                        fetched_at,
                    )
                    task.record(
                        key,
                        "success" if valid else "empty",
                        count=len(valid),
                        fields=("events",),
                        url=url,
                    )
                synced += len(valid)
                for change, count in unit_changes.items():
                    changes[change] += count
            except Exception as exc:
                task.record(key, "parse_failed", url=url, exc=exc)
                failed_units.append(key)
    return {
        "synced": synced,
        **changes,
        "skipped_no_ex_date": skipped_no_ex_date,
        "failed_units": failed_units,
        "run_id": task.id,
    }


def _twse_distribution_year(client: httpx2.Client, code: str, year: int):
    payload = twse_adapter.fetch_etf_distributions(client, code, year, year)
    return twse_adapter.normalize_etf_distributions(payload)


def _tpex_distribution_year(client: httpx2.Client, code: str, year: int):
    # TPEx's `etfExDiv` takes a `YYYYMMDD` range per request rather than a
    # plain year (design 004 §4.2).
    rows = tpex_adapter.fetch_etf_distributions(
        client, code, f"{year}0101", f"{year}1231"
    )
    return tpex_adapter.normalize_etf_distributions(rows)


_DISTRIBUTION_MARKETS = {
    "TWSE": ("twse_etf_distribution_list", _twse_distribution_year),
    "TPEx": ("tpex_etf_distribution_list", _tpex_distribution_year),
}


def etf_distribution_steps(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    market: str,
    *,
    years: int = 3,
    reference_year: int | None = None,
    retry_run_id: str | None = None,
    security_ids: set[str] | None = None,
    year_list: list[int] | None = None,
    year_map: dict[str, set[int]] | None = None,
):
    """One market's distribution sync as `host_pool` steps, so both markets
    (or other syncs) can fetch at once (design 053)."""
    source, fetch = _DISTRIBUTION_MARKETS[market]
    return _etf_distribution_steps(
        conn,
        client,
        market=market,
        source=source,
        fetch_and_normalize_year=fetch,
        years=years,
        reference_year=reference_year,
        retry_run_id=retry_run_id,
        security_ids=security_ids,
        year_list=year_list,
        year_map=year_map,
    )


def sync_etf_distributions(conn: sqlite3.Connection, client: httpx2.Client, **kwargs):
    """TWSE ETF distribution list — independent of `sync_ex_dividend_notices`,
    which covers a different market segment (ETFs aren't in that
    table) via a different source. See `_etf_distribution_steps`."""
    return host_pool.run(etf_distribution_steps(conn, client, "TWSE", **kwargs))


def sync_tpex_etf_distributions(
    conn: sqlite3.Connection, client: httpx2.Client, **kwargs
):
    """TPEx counterpart to `sync_etf_distributions` — same per-
    (security, year) behaviour. See `_etf_distribution_steps`."""
    return host_pool.run(etf_distribution_steps(conn, client, "TPEx", **kwargs))


_REFRESH_IN_PROGRESS = "同步進行中，請稍後再試"


def _held_etf_scope(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """`{market: {security_id}}` for ETFs the user traded or watches."""
    scope: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        """
        SELECT id, market FROM securities
        WHERE security_type = 'etf'
          AND (id IN (SELECT security_id FROM live_trades)
               OR id IN (SELECT security_id FROM watchlist))
        """
    ):
        scope[row["market"]].add(row["id"])
    return scope


def _held_etf_history_start(conn: sqlite3.Connection) -> dict[str, int]:
    """`{security_id: year}` from which a traded ETF's distributions
    belong to the user: its first acquisition (any account, buy or
    opening, like `_stock_dividend_scope`), never before listing.
    Watched-only ETFs have no such year."""
    starts = {}
    for row in conn.execute(
        """
        SELECT s.id, s.listed_date, MIN(t.trade_date) AS first_acquired
        FROM securities s
        JOIN live_trades t ON t.security_id = s.id
        WHERE s.security_type = 'etf' AND t.event_type IN ('buy', 'opening')
        GROUP BY s.id
        """
    ):
        year = int(row["first_acquired"][:4])
        if row["listed_date"]:
            year = max(year, int(row["listed_date"][:4]))
        starts[row["id"]] = year
    return starts


def _pending_amount_rows(
    conn: sqlite3.Connection, security_ids: set[str], since: date
) -> list[sqlite3.Row]:
    """In-scope distributions from `since` on whose amount the source
    hasn't published yet."""
    if not security_ids:
        return []
    placeholders = ",".join("?" * len(security_ids))
    return conn.execute(
        f"""
        SELECT security_id, ex_dividend_date FROM distributions
        WHERE security_id IN ({placeholders})
          AND amount_status IN ('unknown', 'estimated')
          AND ex_dividend_date >= ?
        """,
        (*security_ids, since.isoformat()),
    ).fetchall()


def _refresh_years(today: date, pending: list[sqlite3.Row]) -> list[int]:
    """Design 016 §4.2: this year; next year too in December (late
    announcements for January ex-dates); plus any year still holding a
    recent distribution whose amount is unknown (e.g. last December's,
    refreshed in January)."""
    years = {today.year}
    if today.month == 12:
        years.add(today.year + 1)
    years.update(int(row["ex_dividend_date"][:4]) for row in pending)
    return sorted(years)


def refresh_held_etf_distributions(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    *,
    today: str | None = None,
    scope: dict[str, set[str]] | None = None,
) -> dict:
    """Re-syncs only held/watched ETFs' recent years (design 016) so an
    amount the 投信 just published reaches the income calendar in
    seconds rather than after a ~40-minute full-catalog run. Same
    sources, write rules and `source` names as `sync_etf_distributions`
    / `sync_tpex_etf_distributions`; a market whose full-catalog sync is
    running is reported, not waited on. An empty scope makes no request
    and records no run."""
    today_date = date.fromisoformat(today or today_taipei())
    try:
        since = today_date.replace(year=today_date.year - 1)
    except ValueError:  # 2/29 → a non-leap year
        since = today_date.replace(year=today_date.year - 1, day=28)
    scope = _held_etf_scope(conn) if scope is None else scope
    outcome: dict = {"errors": {}}
    ran = False
    coverage = sync_history.latest_units(conn)
    history_start = _held_etf_history_start(conn)
    with host_pool.shared():
        started = _start_held_markets(
            conn, client, scope, outcome, today_date, since, coverage, history_start
        )
        # Design 053: both markets are fetching by now; write each in turn.
        for name, security_ids, years, finish in started:
            try:
                result = finish()
            except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome["errors"][name] = sync._short_reason(exc)
                continue
            ran = True
            outcome[name] = {**result, "etfs": len(security_ids), "years": years}
    if ran:
        frequency_inference.recompute_all(conn)
    all_ids = set().union(*scope.values()) if scope else set()
    outcome["pending_amounts"] = len(_pending_amount_rows(conn, all_ids, since))
    return outcome


def _start_held_markets(
    conn, client, scope, outcome, today_date, since, coverage, history_start
):
    """Plan each market's years and queue its requests (design 016/050)."""
    started = []
    for name, market in (("twse", "TWSE"), ("tpex", "TPEx")):
        security_ids = scope.get(market, set())
        if not security_ids:
            outcome[name] = {
                "synced": 0,
                "inserted": 0,
                "updated": 0,
                "unchanged": 0,
                "skipped_no_ex_date": 0,
                "failed_units": [],
                "run_id": None,
                "etfs": 0,
                "years": [],
            }
            continue
        years = _refresh_years(
            today_date, _pending_amount_rows(conn, security_ids, since)
        )
        year_map = {sid: set(years) for sid in security_ids}
        source = f"{name}_etf_distribution_list"
        for sid in security_ids:
            # Design 050 §4.2.1: a traded ETF also reaches back to the year
            # it was first acquired; covered years are never asked again.
            start = min(today_date.year - 2, history_start.get(sid, today_date.year))
            for year in range(start, today_date.year):
                if not sync_history.years_covered(coverage, sid, market, [year]):
                    year_map[sid].add(year)
            # Missing amounts and failed old units still need backfill. An old
            # review warning alone should not trigger the same annual request
            # forever; keep its warning for explicit full-year reconciliation.
            for row in conn.execute(
                "SELECT ex_dividend_date FROM distributions WHERE security_id=? "
                "AND (amount_status IN ('unknown','estimated') OR "
                "(reconciliation_status='needs_review' AND ex_dividend_date>=?)) "
                "AND ex_dividend_date IS NOT NULL",
                (sid, since.isoformat()),
            ):
                year_map[sid].add(int(row[0][:4]))
            for (unit_sid, unit_source, period), unit in coverage.items():
                if (
                    unit_sid == sid
                    and unit_source == source
                    and period
                    and unit["status"] not in ("success", "empty", "skipped_delisted")
                ):
                    year_map[sid].add(int(period))
        years = sorted(set().union(*year_map.values()))
        try:
            finish = host_pool.start(
                etf_distribution_steps(
                    conn,
                    client,
                    market,
                    security_ids=security_ids,
                    year_list=years,
                    year_map=year_map,
                )
            )
        except sync_history.SyncInProgress:
            outcome["errors"][name] = _REFRESH_IN_PROGRESS
            continue
        except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
            outcome["errors"][name] = sync._short_reason(exc)
            continue
        started.append((name, security_ids, years, finish))
    return started
