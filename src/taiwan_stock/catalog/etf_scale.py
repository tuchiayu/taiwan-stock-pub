"""Local ETF size and holder history, refreshed when a page finds it stale.

Design 044: SITCA monthly size and TDCC weekly holders, one unit each.
"""

from datetime import date, datetime, timedelta

import httpx2

from taiwan_stock.catalog import etf_scale_sources as sources
from taiwan_stock.catalog import sync_history
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared.dates import now_utc_iso, today_taipei

SOURCES = ("size", "holders")
LABELS = {"size": "規模", "holders": "受益人數"}
# Unchanged sources are asked again at most this often, success or not.
RETRY_SECONDS = 6 * 3600
FORCE_SECONDS = 30
HOLDERS_STALE_DAYS = 7


def after(stamp, seconds):
    return (
        (
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            + timedelta(seconds=seconds)
        )
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def previous_month(today):
    first = date.fromisoformat(today).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def stale(source, latest, today):
    if latest is None:
        return True
    if source == "holders":
        return date.fromisoformat(today) - date.fromisoformat(latest) >= timedelta(
            days=HOLDERS_STALE_DAYS
        )
    return latest < previous_month(today)


def coverage(conn):
    return {
        r["source"]: dict(r) for r in conn.execute("SELECT * FROM etf_scale_coverage")
    }


def due_sources(conn, *, force=False, now=None, today=None):
    now, today = now or now_utc_iso(), today or today_taipei()
    rows = coverage(conn)
    due = []
    for source in SOURCES:
        row = rows.get(source)
        if row is None:
            due.append(source)
        elif stale(source, row["latest_period"], today) and (
            now >= row["next_attempt_at"]
            or (force and now >= after(row["attempted_at"], FORCE_SECONDS))
        ):
            due.append(source)
    return due


def latest(conn):
    """Each source's newest period only; older periods never fill a gap."""
    sizes = {
        r["security_id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM etf_size_snapshots WHERE data_period="
            "(SELECT max(data_period) FROM etf_size_snapshots)"
        )
    }
    holders = {
        r["security_id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM etf_holder_snapshots WHERE data_date="
            "(SELECT max(data_date) FROM etf_holder_snapshots)"
        )
    }
    return {
        "size_period": next(iter(sizes.values()), {}).get("data_period"),
        "sizes": sizes,
        "holders_date": next(iter(holders.values()), {}).get("data_date"),
        "holders": holders,
    }


def display_dates(data):
    """Slash dates for each column; None when that source has no data yet."""
    period, day = (data or {}).get("size_period"), (data or {}).get("holders_date")
    return {
        "size": f"{period.replace('-', '/')} 月底" if period else None,
        "holders": day.replace("-", "/") if day else None,
    }


def dates_label(data):
    dates = display_dates(data)
    return " · ".join(
        f"{LABELS[key]} {dates[key]}" for key in ("size", "holders") if dates[key]
    )


def view(conn):
    now = now_utc_iso()
    rows = coverage(conn)
    data = latest(conn)
    failed = [s for s in SOURCES if rows.get(s, {}).get("status") == "failed"]
    active = conn.execute(
        "SELECT j.id FROM sync_jobs j JOIN sync_job_resources r ON r.job_id=j.id "
        "WHERE r.resource_key='etf-scale' AND j.status IN ('queued','running')"
    ).fetchone()
    parts = [dates_label(data) or "規模／受益人數尚未取得"]
    if failed:
        parts.append("、".join(LABELS[s] for s in failed) + "更新失敗，保留既有資料")
    return {
        "needsRefresh": bool(due_sources(conn, now=now)),
        "activeJobId": active[0] if active else None,
        "canRetry": any(
            now >= after(rows[s]["attempted_at"], FORCE_SECONDS) for s in failed
        ),
        "sizePeriod": data["size_period"],
        "holdersDate": data["holders_date"],
        "message": " · ".join(parts),
    }


def _security_ids(conn):
    """ETF code → id, preferring the active row when a code exists twice."""
    ids = {}
    for r in conn.execute(
        "SELECT id,code FROM securities WHERE security_type='etf' "
        "ORDER BY status='active', id"
    ):
        ids[r["code"]] = r["id"]
    return ids


def _save(conn, source, period, values, url, stamp):
    ids = _security_ids(conn)
    saved = unknown = 0
    for code, value in values.items():
        sid = ids.get(code)
        if sid is None:
            unknown += 1
            continue
        if source == "size":
            conn.execute(
                "INSERT INTO etf_size_snapshots VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(security_id,data_period) DO UPDATE SET "
                "net_assets_twd=excluded.net_assets_twd,nav_twd=excluded.nav_twd,"
                "units=excluded.units,source_url=excluded.source_url,"
                "fetched_at=excluded.fetched_at",
                (
                    sid,
                    period,
                    value["net_assets_twd"],
                    value["nav_twd"],
                    value["units"],
                    url,
                    stamp,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO etf_holder_snapshots VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(security_id,data_date) DO UPDATE SET "
                "holders=excluded.holders,units=excluded.units,"
                "source_url=excluded.source_url,fetched_at=excluded.fetched_at",
                (sid, period, value["holders"], value["units"], url, stamp),
            )
        saved += 1
    return saved, unknown


def _coverage(conn, source, status, stamp, *, period=None, message=None):
    conn.execute(
        "INSERT INTO etf_scale_coverage VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "latest_period=COALESCE(excluded.latest_period,latest_period),"
        "status=excluded.status,attempted_at=excluded.attempted_at,"
        "succeeded_at=COALESCE(excluded.succeeded_at,succeeded_at),"
        "next_attempt_at=excluded.next_attempt_at,message=excluded.message",
        (
            source,
            period,
            status,
            stamp,
            stamp if status == "success" else None,
            after(stamp, RETRY_SECONDS),
            message,
        ),
    )


def synchronize(conn, client, *, force=False):
    loaders = {"size": sources.fetch_sitca_sizes, "holders": sources.fetch_tdcc_holders}
    result = {"updated": 0, "failed_units": 0, "periods": {}}
    with sync_history.run(conn, "etf-scale") as run:
        # Each source is its own unit: one failing never blocks the other.
        for source in due_sources(conn, force=force):
            stamp = now_utc_iso()
            run.plan(source, stage="etf-scale")
            try:
                period, values, url, skipped = loaders[source](client)
                with write_transaction(conn):
                    saved, unknown = _save(conn, source, period, values, url, stamp)
                    _coverage(conn, source, "success", stamp, period=period)
                notes = [f"資料期別 {period}"]
                if unknown:
                    notes.append(f"商品名單未收錄 {unknown} 檔")
                if skipped:
                    notes.append(f"來源缺值 {skipped} 檔")
                run.record(
                    source, "success", count=saved, url=url, reason="；".join(notes)
                )
                result["updated"] += saved
                result["periods"][source] = period
            except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
                _coverage(
                    conn,
                    source,
                    "failed",
                    stamp,
                    message=sync_history.error_info(exc)[2],
                )
                run.record(source, "failed", exc=exc)
                result["failed_units"] += 1
    return result
