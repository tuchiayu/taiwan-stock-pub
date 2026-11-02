"""Official termination evidence, effective dates and durable sync outcomes."""

from taiwan_stock.catalog import delisting_sources as sources
from taiwan_stock.catalog import sync_history
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared import pacing
from taiwan_stock.shared.dates import now_utc_iso, today_taipei

SOURCE = "security_delistings"
SKIPPED = "skipped_delisted"

# A run can match dozens of historical termination notices across many
# ETFs; once TWSE starts blocking, grinding through the rest at the same
# pace just burns the whole run on requests that will keep failing (see
# sync.py's identical reasoning for ETF distributions) — better to stop
# and let the remaining notices stay `pending` for `sync retry`.
_CIRCUIT_BREAKER_THRESHOLD = 5


def refresh_status(conn):
    """Apply already verified scheduled events even without another network sync."""
    conn.execute(
        """
        UPDATE securities SET status='delisted' WHERE status='active' AND EXISTS (
          SELECT 1 FROM security_delistings d WHERE d.market=securities.market
          AND d.code=securities.code AND d.security_type=securities.security_type
          AND d.effective_date<=? AND
          (securities.listed_date IS NULL OR d.effective_date>=securities.listed_date)
        )
        """,
        (today_taipei(),),
    )


def details(conn):
    refresh_status(conn)
    return {
        r["id"]: dict(r)
        for r in conn.execute("""
        SELECT s.id, s.status, s.market, min(d.effective_date) AS delisted_date
        FROM securities s LEFT JOIN security_delistings d ON
        d.market=s.market AND d.code=s.code AND d.security_type=s.security_type
        AND (s.listed_date IS NULL OR d.effective_date>=s.listed_date)
        GROUP BY s.id
        """)
    }


def skip_reason(market):
    return ("已下市" if market == "TWSE" else "已下櫃") + "，略過日常同步"


def store_event(conn, value):
    """A conflicting official date needs review; never silently replace evidence."""
    # A code may have traded in more than one market/lifecycle.
    row = conn.execute(
        """SELECT id,listed_date FROM securities
        WHERE market=? AND code=? AND security_type=?""",
        (value["market"], value["code"], value["security_type"]),
    ).fetchone()
    if row is None or (
        row["listed_date"] and value["effective_date"] < row["listed_date"]
    ):
        return False
    previous = conn.execute(
        """SELECT effective_date FROM security_delistings
        WHERE market=? AND code=? AND security_type=? AND
        (? IS NULL OR effective_date>=?)""",
        (
            value["market"],
            value["code"],
            value["security_type"],
            row["listed_date"],
            row["listed_date"],
        ),
    ).fetchall()
    if any(r[0] != value["effective_date"] for r in previous):
        raise ValueError("官方終止日期與既有證據不一致，保留原狀態，待人工核對更正")
    conn.execute(
        """
        INSERT INTO security_delistings
        (market,code,security_type,effective_date,last_trade_date,reason,
         source_url,fetched_at)
        VALUES (:market,:code,:security_type,:effective_date,:last_trade_date,
         :reason,:source_url,:fetched_at)
        ON CONFLICT(market,code,source_url) DO UPDATE SET
        fetched_at=excluded.fetched_at,
        last_trade_date=excluded.last_trade_date,reason=excluded.reason
        """,
        {**value, "fetched_at": now_utc_iso()},
    )
    return True


def sync_delistings(conn, client, *, retry_run_id=None):
    """Fetch complete indexes before applying evidence; each notice is auditable.

    Retry rechecks the source indexes too: a formerly incomplete index cannot
    become trusted just because an individual detail request succeeded.
    """
    synced, errors = 0, {}
    with sync_history.run(conn, SOURCE, retry_run_id) as task:
        for market, loader in (
            ("TWSE", sources.fetch_twse_stocks),
            ("TPEx", sources.fetch_tpex_stocks),
        ):
            key = market + ":stock"
            if not task.wanted(key):
                continue
            task.plan(key, stage="list")
            url = sources.TWSE_STOCKS if market == "TWSE" else sources.TPEX_STOCKS
            try:
                values = loader(client)
                with write_transaction(conn):
                    count = sum(store_event(conn, value) for value in values)
                    refresh_status(conn)
                    task.record(
                        key, "success", count=count, fields=("status",), url=url
                    )
                synced += count
            except Exception as exc:
                task.record(key, "parse_failed", exc=exc, url=url)
                errors[key] = sync_history.error_info(exc)[2]
        known = {
            (r["market"], r["code"]): r["id"]
            for r in conn.execute(
                "SELECT id,market,code FROM securities WHERE security_type='etf'"
            )
        }
        for market, loader in (
            ("TWSE", sources.fetch_twse_notices),
            ("TPEx", sources.fetch_tpex_notices),
        ):
            key = market + ":etf:index"
            # Index failures have no child units yet; a retry must discover them.
            retry_index = task.retry_keys is None or key in task.retry_keys
            if not retry_index and not any(
                k.startswith(market + ":etf:") for k in task.retry_keys
            ):
                continue
            task.plan(key, stage="list")
            url = sources.TWSE_ETFS if market == "TWSE" else sources.TPEX_ETFS
            try:
                notices = loader(client)
                task.record(key, "success", count=len(notices), url=url)
            except Exception as exc:
                task.record(key, "parse_failed", exc=exc, url=url)
                errors[key] = sync_history.error_info(exc)[2]
                continue
            candidates = []
            for params, subject in notices:
                sid = known.get((market, params["fund"]))
                if sid is None or not sources.termination_subject(subject):
                    continue
                notice_key = (
                    f"{market}:etf:{params['fund']}:{params['date']}:{params['seq']}"
                )
                if not retry_index and not task.wanted(notice_key):
                    continue
                candidates.append((notice_key, sid, params))
            for notice_key, sid, params in candidates:
                task.plan(notice_key, security_id=sid, stage="termination_notice")
            consecutive_blocking_failures = 0
            for notice_key, sid, params in candidates:
                if consecutive_blocking_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                    break
                try:
                    value = sources.fetch_notice(client, market, params)
                    with write_transaction(conn):
                        count = int(store_event(conn, value))
                        refresh_status(conn)
                        task.record(
                            notice_key,
                            "success",
                            count=count,
                            fields=("status",),
                            url=value["source_url"],
                        )
                    synced += count
                    consecutive_blocking_failures = 0
                except sources.NoticeDatePending as exc:
                    task.record(
                        notice_key, "skipped_notice", reason=str(exc), url=exc.url
                    )
                    consecutive_blocking_failures = 0
                except Exception as exc:
                    task.record(notice_key, "parse_failed", exc=exc)
                    errors[notice_key] = sync_history.error_info(exc)[2]
                    consecutive_blocking_failures = (
                        consecutive_blocking_failures + 1
                        if sources.is_blocking_error(exc)
                        else 0
                    )
                finally:
                    pacing.pause(sources.PAGE_DELAY_SECONDS)
    return {"synced": synced, "errors": errors, "run_id": task.id}
