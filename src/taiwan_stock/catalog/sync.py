"""Upserts adapter-fetched catalog rows into SQLite.

Design 002 §4.7: syncing is idempotent and identity is (market, code)
— re-running just updates existing `securities`/`etf_profiles` rows,
it never duplicates them or touches a user's `watchlist`/`trades`.

`sync_stock_catalog`/`sync_ex_dividend_notices` take an `adapter`
module (default `twse_adapter`) so the same upsert logic serves both
TWSE and TPEx — pass `tpex_adapter` for the OTC market. `sync_etf_catalog`
has no such `adapter` parameter because TWSE's `t187ap47_L` and TPEx's
ETF list (design 004 §4.2, `sync_tpex_etf_list`) return genuinely
different shapes — TWSE's has rich basic-info fields, TPEx's has only
code+name — so they get separate functions rather than one shared
one that papers over the difference.
"""

import sqlite3
from types import ModuleType

import httpx2

from taiwan_stock.catalog import (
    delistings,
    sync_history,
    tpex_adapter,
    twse_adapter,
)
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared import pacing, sync_progress
from taiwan_stock.shared.ids import new_id

# Design 005 §4.4, revisited against production behavior: TWSE rejects
# (HTTP 428) bursts of requests once the rate gets too high. Measured
# live across several sessions on 2026-09-13: 0.2s pacing (~5 req/s)
# got blocked after ~35-75 requests; a steady 1 req/s looked safe in an
# isolated 60-request check but still got blocked after ~50 requests
# once run back-to-back with everything else tried that day (points at
# a longer, usage-accumulating window, not a short per-minute one). The
# 5s baseline that followed was validated by retrying that day's 659
# still-failing units end to end (a `sync retry` of the run that had
# hit the above two failures, not a fresh isolated sample): zero HTTP
# 428s across ~58 minutes.
#
# 2s/3s were never measured until issue #15 (2026-09-14): a 100-request
# sample at 3s came back clean, then — to match the 5s baseline's own
# scale rather than settle for a smaller sample — a full-catalog live
# run at 2s (all 271 TWSE ETFs x 3 years, 813 real requests, ~30
# minutes) also came back with zero HTTP 428s and nothing else
# blocking-class either. So 2s replaces 5s as the new baseline (roughly
# 60% shorter full-catalog run). If this starts tripping the circuit
# breaker (below) on a future run (traffic patterns can change), that's
# this value being too optimistic — 3s and then 5s are the last
# known-good fallbacks, in that order. Everything else here still gets
# a bounded number of retries with backoff before the unit is recorded
# as failed. Parse/validation errors are not retried — a bad response
# won't fix itself by asking again.
_RETRY_BACKOFF_SECONDS = (0.5, 2.0)

# If the source is rate-limiting us anyway (misjudged pace, a spike in
# other traffic sharing the same IP, a tighter limit some other day),
# retrying every single one of the remaining hundreds of units is
# pointless — live observation shows a block outlasts our retry
# backoff by roughly two orders of magnitude (minutes, not seconds).
# So once several requests in a row fail at the HTTP layer specifically
# (not a parse/mismatch error — that's a data problem, not a blocked
# source), stop attempting the rest of this run's units rather than
# grinding through them for no benefit; they stay `pending` and
# `taiwan-stock sync retry <run_id>` picks them up once the source
# recovers.
_CIRCUIT_BREAKER_THRESHOLD = 5


def _is_blocking_error(exc: Exception) -> bool:
    """403/428/429/5xx and connection-level failures look like the host
    itself pushing back (blocked, rate-limited, overloaded) — worth
    counting toward the circuit breaker. A 404 means this one product
    has no page there (seen live: 投信 issuer guessed from the name
    prefix, but that ETF isn't actually this issuer's, or the site
    just doesn't have every product under this URL shape) — a data
    problem with this one unit, not a sign every other request to the
    same host will fail too, so it must not trip the breaker and
    shouldn't give up on the rest of that host's units. A maintenance
    page (design 048 §4.2.8) is the whole host being down, so it counts."""
    if isinstance(exc, sync_history.SourceUnavailable):
        return True
    if not isinstance(exc, httpx2.HTTPError):
        return False
    if isinstance(exc, httpx2.HTTPStatusError) and exc.response.status_code == 404:
        return False
    return True


def _fetch_with_retry(fetch, *args, **kwargs):
    for delay in (*_RETRY_BACKOFF_SECONDS, None):
        sync_progress.checkpoint()
        try:
            return fetch(*args, **kwargs)
        except httpx2.HTTPError:
            if delay is None:
                raise
            pacing.pause(delay)


def upsert_security(conn: sqlite3.Connection, fields: dict) -> str:
    # `listed_date` defaults to whatever's already stored when a caller
    # doesn't know it (design 005 §4.5 follow-up: TPEx's ETF list source
    # has no such field at all) — never clobber a previously-learned
    # listing date with an unrelated sync's blank guess.
    fields = {"listed_date": None, **fields}
    row = conn.execute(
        """
        INSERT INTO securities
            (id, market, code, name, security_type, industry,
             transfer_agent_name, transfer_agent_phone, status, listed_date)
        VALUES (:id, :market, :code, :name, :security_type, :industry,
                :transfer_agent_name, :transfer_agent_phone, :status,
                :listed_date)
        ON CONFLICT (market, code) DO UPDATE SET
            name = excluded.name,
            security_type = excluded.security_type,
            industry = excluded.industry,
            transfer_agent_name = excluded.transfer_agent_name,
            transfer_agent_phone = excluded.transfer_agent_phone,
            status = CASE WHEN securities.status='delisted' THEN 'delisted'
                ELSE excluded.status END,
            listed_date = COALESCE(excluded.listed_date, securities.listed_date)
        RETURNING id
        """,
        {**fields, "id": new_id()},
    ).fetchone()
    return row["id"]


def upsert_etf_profile(
    conn: sqlite3.Connection, security_id: str, fields: dict
) -> None:
    """Writes only the columns the TWSE basic-info sync legitimately
    owns (raw custodian institution for reference, fee summary,
    benchmark). `custodian_bank` and `distribution_frequency` are owned
    by the per-投信 adapters and user overrides (design 004 §4.5.1) —
    excluding them from the insert column list and the ON CONFLICT
    update means re-running this sync can never clobber values those
    other sources set, fixing the overwrite bug design 004 §2.1
    describes. `fields` may still carry `custodian_bank`/
    `distribution_frequency` keys (adapters set them to `None` for
    clarity); they are simply not referenced by this SQL.
    """
    conn.execute(
        """
        INSERT INTO etf_profiles
            (security_id, custodian_institution_raw, fee_summary, benchmark,
             fund_tax_id, fund_type)
        VALUES
            (:security_id, :custodian_institution_raw, :fee_summary, :benchmark,
             :fund_tax_id, :fund_type)
        ON CONFLICT (security_id) DO UPDATE SET
            custodian_institution_raw = COALESCE(
                excluded.custodian_institution_raw,
                etf_profiles.custodian_institution_raw),
            fee_summary = COALESCE(excluded.fee_summary, etf_profiles.fee_summary),
            benchmark = COALESCE(excluded.benchmark, etf_profiles.benchmark),
            fund_tax_id = COALESCE(excluded.fund_tax_id, etf_profiles.fund_tax_id),
            fund_type = COALESCE(excluded.fund_type, etf_profiles.fund_type)
        """,
        {"fund_tax_id": None, "fund_type": None, **fields, "security_id": security_id},
    )


def sync_stock_catalog(
    conn: sqlite3.Connection, client: httpx2.Client, adapter: ModuleType = twse_adapter
) -> int:
    rows = adapter.fetch_stock_basic_info(client)
    with write_transaction(conn):
        for row in rows:
            upsert_security(conn, adapter.normalize_stock(row))
        delistings.refresh_status(conn)
    return len(rows)


def sync_etf_catalog(conn: sqlite3.Connection, client: httpx2.Client) -> int:
    rows = twse_adapter.fetch_etf_basic_info(client)
    with write_transaction(conn):
        for row in rows:
            security_fields, profile_fields = twse_adapter.normalize_etf(row)
            security_id = upsert_security(conn, security_fields)
            upsert_etf_profile(conn, security_id, profile_fields)
        delistings.refresh_status(conn)
    return len(rows)


def sync_tpex_etf_list(conn: sqlite3.Connection, client: httpx2.Client) -> int:
    """Design 004 §4.2/§4.5.3: TPEx's ETF list only has code+name, so
    (unlike `sync_etf_catalog`'s TWSE rows) the `etf_profiles` row
    created here starts with every field `None` — it exists purely so
    a later per-投信 adapter (Phase E) has a row to update, not because
    this source has anything to fill in yet.
    """
    entries = tpex_adapter.fetch_etf_list(client)
    security_rows = [
        row
        for row in (tpex_adapter.normalize_etf_list_entry(entry) for entry in entries)
        if row is not None
    ]
    with write_transaction(conn):
        for security_fields in security_rows:
            security_id = upsert_security(conn, security_fields)
            upsert_etf_profile(
                conn,
                security_id,
                {
                    "custodian_institution_raw": None,
                    "custodian_bank": None,
                    "distribution_frequency": None,
                    "fee_summary": None,
                    "benchmark": None,
                },
            )
        delistings.refresh_status(conn)
    return len(security_rows)


def upsert_distribution(
    conn: sqlite3.Connection, security_id: str, fields: dict
) -> str:
    row = conn.execute(
        """
        INSERT INTO distributions
            (id, security_id, ex_dividend_date, amount_per_share, amount_status)
        VALUES (:id, :security_id, :ex_dividend_date, :amount_per_share, :amount_status)
        ON CONFLICT (security_id, ex_dividend_date) DO UPDATE SET
            amount_per_share = excluded.amount_per_share,
            amount_status = excluded.amount_status
        RETURNING id
        """,
        {**fields, "id": new_id(), "security_id": security_id},
    ).fetchone()
    return row["id"]


def upsert_corporate_action(
    conn: sqlite3.Connection, security_id: str, fields: dict
) -> str:
    from taiwan_stock.portfolio import action_revisions

    with write_transaction(conn):
        before_actions = conn.execute(
            "SELECT * FROM corporate_actions WHERE security_id=?", (security_id,)
        ).fetchall()
        before = next(
            (
                a
                for a in before_actions
                if a["ex_date"] == fields["ex_date"]
                and a["action_type"] == fields["action_type"]
            ),
            None,
        )
        action_id = _upsert_corporate_action(conn, security_id, fields)
        if before is not None and (
            before["ratio_numerator"],
            before["ratio_denominator"],
        ) != (fields["ratio_numerator"], fields["ratio_denominator"]):
            after_actions = conn.execute(
                "SELECT * FROM corporate_actions WHERE security_id=?", (security_id,)
            ).fetchall()
            after = next(a for a in after_actions if a["id"] == action_id)
            impacts = action_revisions.replay_impacts(
                conn, security_id, before_actions, after_actions
            )
            action_revisions.record(
                conn,
                before,
                after,
                impacts,
                source=fields.get("source", "official"),
                source_url=fields.get("source_url"),
            )
        return action_id


def _upsert_corporate_action(
    conn: sqlite3.Connection, security_id: str, fields: dict
) -> str:
    """Official data stays authoritative — corporate actions cannot be
    edited, so a wrong manual placeholder can only be corrected here — but
    is validated like manual input, and an overwritten manual ratio is
    kept in `note` (issue #115 #2). Raises CorporateActionValidationError.
    """
    from taiwan_stock.portfolio.corporate_actions import (
        AUTO_APPLIED_TYPES,
        validate_auto_applied_ratio,
    )

    if fields["action_type"] in AUTO_APPLIED_TYPES:
        validate_auto_applied_ratio(fields)
    current = conn.execute(
        "SELECT ratio_numerator, ratio_denominator, source, note "
        "FROM corporate_actions "
        "WHERE security_id = ? AND ex_date = ? AND action_type = ?",
        (security_id, fields["ex_date"], fields["action_type"]),
    ).fetchone()
    note = current["note"] if current else None
    if (
        current is not None
        and current["source"] == "manual"
        and (current["ratio_numerator"], current["ratio_denominator"])
        != (fields["ratio_numerator"], fields["ratio_denominator"])
    ):
        overwritten = (
            "官方資料已更新比例（原手動 "
            f"{current['ratio_numerator']}/{current['ratio_denominator']}）"
        )
        note = f"{note}；{overwritten}" if note else overwritten
    row = conn.execute(
        """
        INSERT INTO corporate_actions
            (id, security_id, action_type, ratio_numerator,
             ratio_denominator, ex_date, source)
        VALUES
            (:id, :security_id, :action_type, :ratio_numerator,
             :ratio_denominator, :ex_date, 'official')
        ON CONFLICT (security_id, ex_date, action_type) DO UPDATE SET
            ratio_numerator = excluded.ratio_numerator,
            ratio_denominator = excluded.ratio_denominator,
            source = 'official',
            note = :note
        RETURNING id
        """,
        {**fields, "id": new_id(), "security_id": security_id, "note": note},
    ).fetchone()
    return row["id"]


def _short_reason(exc: Exception) -> str:
    # httpx's messages carry a full URL and a docs link — too long for a
    # flash message; the status or error type is what's actionable.
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx2.HTTPError):
        return type(exc).__name__
    return (str(exc).splitlines() or [type(exc).__name__])[0][:120]
