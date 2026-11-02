"""Local market calendar cache. Never writes to distributions or holdings."""

import json
from datetime import date, datetime, timedelta

import httpx2

from taiwan_stock.catalog import market_calendar_adapter as adapter
from taiwan_stock.catalog import sync_history
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared.dates import now_utc_iso, today_taipei

SOURCES = ("annual", "notices")
MIN_YEAR = 2021


def supported(year):
    return MIN_YEAR <= year <= int(today_taipei()[:4]) + 1


def after(stamp, seconds):
    return (
        (
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            + timedelta(seconds=seconds)
        )
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def latest_failure(conn, year):
    return conn.execute(
        "SELECT submitted_at,finished_at FROM sync_jobs "
        "WHERE operation='market-calendar' AND status IN ('failed','interrupted') "
        "AND json_extract(parameters,'$.year')=? ORDER BY submitted_at DESC LIMIT 1",
        (year,),
    ).fetchone()


def due_sources(conn, year, *, force=False, now=None):
    if not supported(year):
        return []
    now = now or now_utc_iso()
    failed = latest_failure(conn, year)
    if failed and now < after(
        failed["finished_at"] or failed["submitted_at"], 30 if force else 300
    ):
        return []
    due = []
    for source in SOURCES:
        row = conn.execute(
            "SELECT * FROM market_calendar_coverage WHERE source=? AND year=?",
            (source, year),
        ).fetchone()
        if row is None or (
            now >= row["next_attempt_at"]
            or (force and now >= after(row["attempted_at"], 30))
        ):
            due.append(source)
    return due


def view(conn, year):
    now = now_utc_iso()
    due = due_sources(conn, year, now=now)
    events = {}
    grouped = {}
    for row in conn.execute(
        "SELECT * FROM market_calendar_events WHERE year=? ORDER BY source,date",
        (year,),
    ):
        grouped.setdefault(row["date"], []).append(dict(row))
    for day, rows in grouped.items():
        notice_review = any(
            r["source"] == "notices" and r["status"] == "needs_review" for r in rows
        )
        confirmed = [
            r
            for r in rows
            if r["status"] != "needs_review"
            and not (r["source"] == "notices" and notice_review)
        ]
        chosen = next((r for r in confirmed if r["source"] == "notices"), None)
        chosen = chosen or next(iter(confirmed), None)
        review = any(r["status"] == "needs_review" for r in rows)
        events[day] = {
            "status": chosen["status"] if chosen else "needs_review",
            "name": chosen["name"] if chosen else "休市資訊待確認",
            "title": chosen["title"] if chosen else "休市資訊待確認",
            "url": chosen["source_url"] if chosen else rows[0]["source_url"],
            "needsReview": review,
        }
    result = {
        "year": year,
        "events": events,
        "needsRefresh": bool(due),
        "supported": supported(year),
        "activeJobId": None,
        "canRetry": False,
    }
    active = conn.execute(
        "SELECT j.id FROM sync_jobs j JOIN sync_job_resources r ON r.job_id=j.id "
        "WHERE r.resource_key='market-calendar' AND j.status IN ('queued','running')"
    ).fetchone()
    if active:
        result["activeJobId"] = active[0]
    for source in SOURCES:
        row = conn.execute(
            "SELECT * FROM market_calendar_coverage WHERE source=? AND year=?",
            (source, year),
        ).fetchone()
        label = "年度表" if source == "annual" else "臨時公告"
        state = row["status"] if row else "missing"
        failed = latest_failure(conn, year)
        failed_at = (
            (failed["finished_at"] or failed["submitted_at"]) if failed else None
        )
        if failed_at and (not row or row["attempted_at"] < failed["submitted_at"]):
            state = "failed"
            result["canRetry"] |= now >= after(failed_at, 30)
        if not supported(year):
            state, message = "unsupported", "此年度超出休市資料範圍，僅標示週末"
        else:
            message = {
                "missing": "尚未取得",
                "success": "已更新",
                "empty": "該年度資料尚未取得"
                if source == "annual"
                else "未查得明確公告",
                "partial": "有資料待確認",
                "failed": "更新失敗，保留既有資料",
            }[state]
            if state == "success" and source in due:
                message = "資料待更新"
        prefix = "annual" if source == "annual" else "notice"
        result[prefix + "Status"] = state
        result[prefix + "UpdatedAt"] = row["succeeded_at"] if row else None
        result[prefix + "Message"] = label + "：" + message
        if row and state in ("failed", "partial"):
            result["canRetry"] |= now >= after(row["attempted_at"], 30)
    return result


def day_info(day, event=None):
    weekend = date.fromisoformat(day).weekday() >= 5
    event = event or {}
    state = event.get("status")
    closed = state == "closed" or (weekend and state != "open")
    return {
        "closed": closed,
        "weekend": weekend,
        "name": event.get("name", ""),
        "url": event.get("url", ""),
        "review": event.get("needsReview", False),
        "description": (
            ("週末" if weekend and state != "open" else "")
            + ("，" if weekend and state != "open" and event else "")
            + event.get("name", "")
        ),
    }


def _save(conn, source, year, events, snapshot, reasons, stamp):
    previous = {
        (r["event_key"], r["date"]): dict(r)
        for r in conn.execute(
            "SELECT * FROM market_calendar_events WHERE source=? AND year=?",
            (source, year),
        )
    }
    seen = set()
    for item in events:
        key = (item["key"], item["date"])
        seen.add(key)
        old = previous.get(key)
        if source == "notices" and old and old["fingerprint"] != item["fingerprint"]:
            item["status"] = "needs_review"
            reasons.append("已有臨時公告內容變動，需確認")
        elif old and old["status"] == "needs_review" and source == "notices":
            item["status"] = "needs_review"
            reasons.append("臨時公告變動待確認")
        conn.execute(
            "INSERT INTO market_calendar_events "
            "(source,year,event_key,date,status,name,title,source_url,"
            "published_at,fingerprint,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source,year,event_key,date) DO UPDATE SET "
            "previous_snapshot=CASE WHEN fingerprint<>excluded.fingerprint THEN "
            "json_object('status',status,'title',title,'fingerprint',fingerprint,"
            "'updatedAt',updated_at) ELSE previous_snapshot END,"
            "status=excluded.status,name=excluded.name,title=excluded.title,"
            "source_url=excluded.source_url,published_at=excluded.published_at,"
            "fingerprint=excluded.fingerprint,updated_at=excluded.updated_at",
            (
                source,
                year,
                item["key"],
                item["date"],
                item["status"],
                item["name"],
                item["title"],
                item["url"],
                item["published"],
                item["fingerprint"],
                stamp,
            ),
        )
    for key in previous.keys() - seen:
        if source == "annual" and events and not reasons:
            conn.execute(
                "DELETE FROM market_calendar_events WHERE source=? AND year=? "
                "AND event_key=? AND date=?",
                (source, year, *key),
            )
        elif source == "notices":
            conn.execute(
                "UPDATE market_calendar_events SET status='needs_review',updated_at=? "
                "WHERE source=? AND year=? AND event_key=? AND date=?",
                (stamp, source, year, *key),
            )
            reasons.append("先前臨時公告未見於完整清單，需確認")
    if source == "annual" and not events and previous:
        reasons.append("年度來源暫無資料，保留既有資料")
    # Conflicting notices cannot silently override each other.
    conflicts = conn.execute(
        "SELECT event_key FROM market_calendar_events WHERE source=? AND year=? "
        "GROUP BY event_key HAVING count(DISTINCT date)>1",
        (source, year),
    ).fetchall()
    for row in conflicts:
        conn.execute(
            "UPDATE market_calendar_events SET status='needs_review' "
            "WHERE source=? AND year=? AND event_key=?",
            (source, year, row[0]),
        )
        reasons.append("公告日期變動待確認")
    status = "partial" if reasons else "success" if events else "empty"
    current = int(today_taipei()[:4])
    ttl = 30 * 86400 if year < current else 1800 if source == "notices" else 86400
    if status == "partial":
        ttl = 1800
    elif status == "empty" and source == "annual":
        ttl = 86400
    _coverage(
        conn,
        source,
        year,
        status,
        stamp,
        ttl,
        "; ".join(sorted(set(reasons))),
        snapshot,
    )
    return status


def _coverage(conn, source, year, status, stamp, ttl, reason, snapshot=None):
    success = stamp if status in ("success", "empty") else None
    conn.execute(
        "INSERT INTO market_calendar_coverage VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source,year) DO UPDATE SET status=excluded.status,"
        "attempted_at=excluded.attempted_at,"
        "succeeded_at=COALESCE(excluded.succeeded_at,succeeded_at),"
        "next_attempt_at=excluded.next_attempt_at,reason=excluded.reason,"
        "snapshot=COALESCE(excluded.snapshot,snapshot)",
        (
            source,
            year,
            status,
            stamp,
            success,
            after(stamp, ttl),
            reason,
            json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
        ),
    )


def synchronize(conn, client, year, *, force=False):
    result = {"updated": 0, "failed_units": 0}
    with sync_history.run(conn, "market-calendar") as run:
        for source in due_sources(conn, year, force=force):
            stamp = now_utc_iso()
            key = f"{source}:{year}"
            run.plan(key, stage="calendar", period=str(year))
            try:
                loader = adapter.annual if source == "annual" else adapter.notices
                events, snapshot, reasons = loader(client, year)
                with write_transaction(conn):
                    status = _save(conn, source, year, events, snapshot, reasons, stamp)
                run.record(key, status, count=len(events), reason="; ".join(reasons))
                result["updated"] += len(events)
                result["failed_units"] += status == "partial"
            except (
                httpx2.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
            ) as exc:
                _coverage(
                    conn,
                    source,
                    year,
                    "failed",
                    stamp,
                    300,
                    sync_history.error_info(exc)[2],
                )
                run.record(key, "failed", exc=exc)
                result["failed_units"] += 1
    return result
