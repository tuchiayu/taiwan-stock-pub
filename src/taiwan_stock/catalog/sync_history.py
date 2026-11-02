"""Durable per-source/per-product sync outcomes; no background scheduler."""

import json
import sqlite3
from contextlib import contextmanager

import httpx2

from taiwan_stock.shared import sync_progress
from taiwan_stock.shared.dates import now_utc_iso
from taiwan_stock.shared.ids import new_id


class SyncInProgress(Exception):
    pass


class SourceUnavailable(Exception):
    """The source answered with a maintenance page (design 048 §4.2.8):
    transient like a timeout, not a change that needs a code update."""


class ProductNotListed(KeyError):
    """The code is absent from an official list fetched in full (design 062).
    A `KeyError` so code that already catches one keeps working."""


def listed(mapping, code):
    """`mapping[code]` from a complete official list, else ProductNotListed."""
    if mapping is None or code not in mapping:
        raise ProductNotListed(code)
    return mapping[code]


# Design 062: the database itself failed (e.g. locked) — transient, retried.
DB_FAILED = "db_failed"
# Design 063: a share class whose TWD class has no outcome to copy yet —
# transient, so a retry copies it once the TWD class has synced.
BASE_PENDING = "base_pending"


# Unit outcomes fall into three classes (issue #135). Only a transient
# failure may recover on its own; "not listed" means the official source has
# no such product, so it is settled — never a failure, never retried. A
# source change (format mismatch, product page 404) needs a code update: it
# is retried but kept apart from transient failures. `mapping_failed` is the
# pre-#135 name for "not listed" and remains in older history.
NOT_LISTED = "not_listed"
SETTLED = frozenset(
    {
        "success",
        "empty",
        "unsupported",
        "skipped_delisted",
        "skipped_notice",
        NOT_LISTED,
        "mapping_failed",
    }
)
NOT_LISTED_STATUSES = frozenset({NOT_LISTED, "mapping_failed"})
# Design 055: the source said it is down for maintenance. Not a failure (the
# old values stay and nothing is wrong on our side) but not settled either,
# so a retry or the next sync picks it up once the site is back.
MAINTENANCE = "source_maintenance"
SOURCE_CHANGED = frozenset({"parse_failed"})
IN_PROGRESS = frozenset({"pending", "running"})


def is_failure(status):
    """Needs attention: a transient failure or a source change. A source
    under maintenance is neither (design 055), though it is still retried."""
    return status not in SETTLED and status not in IN_PROGRESS and status != MAINTENANCE


def is_maintenance(status):
    return status == MAINTENANCE


def is_transient_failure(status):
    return is_failure(status) and status not in SOURCE_CHANGED


def sql_in(statuses):
    return "(" + ",".join(f"'{s}'" for s in sorted(statuses)) + ")"


CHANGE_RUNS = 3
# Design 074: runs are never pruned, so only each source's latest are read.
# Retries cover some issuers only, hence more than CHANGE_RUNS.
RECENT_RUNS = 20


def suspected_changes(conn, sources):
    """Design 068: a source (per issuer for 投信 data) whose last CHANGE_RUNS
    finished runs could only fail to parse — retrying won't help."""
    sources = sorted(set(sources))
    if not sources:
        return []
    rows = conn.execute(
        "WITH recent AS (SELECT id,source,started_at,rowid AS seq,"
        "row_number() OVER (PARTITION BY source "
        "ORDER BY started_at DESC,rowid DESC) AS n "
        "FROM catalog_sync_runs WHERE status!='running' AND source IN "
        f"({','.join('?' * len(sources))})) "
        "SELECT r.id,r.source,coalesce(u.issuer,'') AS issuer,"
        "sum(u.status IN " + sql_in(SOURCE_CHANGED) + ") AS failed,"
        "sum(u.status IN ('success','empty')) AS succeeded "
        "FROM recent r JOIN catalog_sync_units u ON u.run_id=r.id "
        "WHERE r.n<=? "
        "GROUP BY r.id,r.source,coalesce(u.issuer,'') "
        "ORDER BY r.started_at DESC,r.seq DESC",
        [*sources, RECENT_RUNS],
    ).fetchall()
    groups = {}
    for row in rows:
        groups.setdefault((row["source"], row["issuer"]), []).append(row)
    suspected = []
    for (source, issuer), runs in groups.items():
        recent = runs[:CHANGE_RUNS]
        if len(recent) < CHANGE_RUNS or any(
            not run["failed"] or run["succeeded"] for run in recent
        ):
            continue
        reason = conn.execute(
            "SELECT reason FROM catalog_sync_units WHERE run_id=? AND "
            "coalesce(issuer,'')=? AND status IN " + sql_in(SOURCE_CHANGED) + " "
            "ORDER BY rowid LIMIT 1",
            (recent[0]["id"], issuer),
        ).fetchone()[0]
        suspected.append(
            {
                "source": source,
                "issuer": issuer or None,
                "run_id": recent[0]["id"],
                "reason": reason,
            }
        )
    return suspected


def recover_interrupted(conn):
    """Called once at application startup, not per DB connection."""
    conn.execute(
        """
        UPDATE catalog_sync_units SET status='interrupted', finished_at=?
        WHERE run_id IN (SELECT id FROM catalog_sync_runs WHERE
        status='running') AND status='pending'
        """,
        (now_utc_iso(),),
    )
    conn.execute(
        """
        UPDATE catalog_sync_runs SET status='interrupted', finished_at=? WHERE
        status='running'
        """,
        (now_utc_iso(),),
    )


def error_info(exc):
    if isinstance(exc, httpx2.HTTPStatusError):
        code = exc.response.status_code
        # A missing page persists until the source or our adapter changes
        # (e.g. #129: a redesigned site 404s every old product URL).
        status = "parse_failed" if code == 404 else "http_failed"
        return status, type(exc).__name__, f"HTTP {code}"
    if isinstance(exc, httpx2.HTTPError):
        return "http_failed", type(exc).__name__, "連線失敗或逾時"
    if isinstance(exc, SourceUnavailable):
        return MAINTENANCE, type(exc).__name__, "來源網站維護中"
    # Design 062: only a code missing from a complete official list is "not
    # listed"; any other missing key means the response changed shape.
    if isinstance(exc, ProductNotListed):
        return NOT_LISTED, "ProductNotListed", "官方名單未列出此商品"
    if isinstance(exc, KeyError):
        return "parse_failed", "KeyError", f"來源格式不符（缺少欄位 {exc}）"[:160]
    if isinstance(exc, sqlite3.Error):
        return DB_FAILED, type(exc).__name__, "資料庫忙碌或無法寫入"
    # Avoid persisting arbitrary response text/URLs that can carry credentials.
    return (
        "parse_failed",
        type(exc).__name__,
        (
            str(exc)[:160]
            if isinstance(exc, ValueError) or type(exc).__name__ == "TrustAdapterError"
            else "來源格式或資料驗證失敗"
        ),
    )


# Shown to the user for a sync phase or source (design 051 §4.2.4).
SOURCE_LABELS = {
    "market-calendar": "TWSE 休市行事曆",
    "etf-scale": "ETF 規模／受益人數",
    "twse_etf_distribution_list": "TWSE ETF 配息",
    "tpex_etf_distribution_list": "TPEx ETF 配息",
    "investment_trust": "投信保管銀行／配息頻率",
    "etf_profile_basic": "ETF 官方基本資料",
    "security_delistings": "核對上市／上櫃狀態",
}


class Run:
    def __init__(self, conn, source, retry_of=None):
        self.conn, self.source, self.id = conn, source, new_id()
        progress = sync_progress.current.get()
        job_id = progress.job_id if progress else None
        if progress:
            progress.phase = SOURCE_LABELS.get(source, source)
            progress.update()
        if progress and retry_of is None:
            retry_job = conn.execute(
                "SELECT retry_of FROM sync_jobs WHERE id=?", (job_id,)
            ).fetchone()[0]
            if retry_job:
                prior = conn.execute(
                    "SELECT id FROM catalog_sync_runs WHERE job_id=? AND "
                    "source=? ORDER BY rowid DESC LIMIT 1",
                    (retry_job, source),
                ).fetchone()
                if prior:
                    retry_of = prior[0]
        self.retry_keys = None
        if retry_of:
            previous = conn.execute(
                "SELECT * FROM catalog_sync_runs WHERE id=?", (retry_of,)
            ).fetchone()
            if (
                not previous
                or previous["source"] != source
                or previous["status"] == "running"
            ):
                raise ValueError("不是可重試的同步紀錄")
            self.retry_keys = {
                r[0]
                for r in conn.execute(
                    "SELECT unit_key FROM catalog_sync_units WHERE run_id=? AND "
                    "status NOT IN " + sql_in(SETTLED),
                    (retry_of,),
                )
            }
        try:
            conn.execute(
                """
                INSERT INTO
                catalog_sync_runs(id,source,retry_of,started_at,status,job_id)
                VALUES(?,?,?,?,'running',?)
                """,
                (self.id, source, retry_of, now_utc_iso(), job_id),
            )
        except sqlite3.IntegrityError as exc:
            raise SyncInProgress("此來源同步中，請稍後再試") from exc

    def wanted(self, key):
        sync_progress.checkpoint()
        return self.retry_keys is None or key in self.retry_keys

    def plan(self, key, *, security_id=None, issuer=None, stage="product", period=None):
        sync_progress.checkpoint()
        self.conn.execute(
            """
            INSERT INTO
            catalog_sync_units(run_id,unit_key,security_id,issuer,stage,period,attempted_at,status)
            VALUES(?,?,?,?,?,?,?,'pending')
            """,
            (self.id, key, security_id, issuer, stage, period, now_utc_iso()),
        )

    def record(
        self,
        key,
        status,
        *,
        count=0,
        fields=(),
        url=None,
        exc=None,
        reason=None,
        field_results=None,
    ):
        error_type = None
        if exc:
            status, error_type, reason = error_info(exc)
        self.conn.execute(
            """
            UPDATE catalog_sync_units SET status=?, finished_at=?,
            row_count=?, fields=?, source_url=?, error_type=?, reason=?,
            field_results=? WHERE
            run_id=? AND unit_key=?
            """,
            (
                status,
                now_utc_iso(),
                count,
                json.dumps(list(fields)),
                url,
                error_type,
                reason,
                json.dumps(field_results, ensure_ascii=False)
                if field_results
                else None,
                self.id,
                key,
            ),
        )
        sync_progress.checkpoint()

    def finish(self, interrupted=False):
        counts = {
            r[0]: r[1]
            for r in self.conn.execute(
                """
                SELECT status,count(*) FROM catalog_sync_units WHERE run_id=?
                GROUP BY status
                """,
                (self.id,),
            )
        }
        # Units still pending here were never reached, which is incomplete too.
        failed = sum(n for s, n in counts.items() if s not in SETTLED)
        succeeded = counts.get("success", 0) + counts.get("empty", 0)
        state = (
            "interrupted"
            if interrupted
            else "partial"
            if failed and succeeded
            else "failed"
            if failed
            else "success"
        )
        self.conn.execute(
            "UPDATE catalog_sync_runs SET status=?, finished_at=? WHERE id=?",
            (state, now_utc_iso(), self.id),
        )


@contextmanager
def run(conn, source, retry_of=None):
    # Design 073: a caller's own transaction is the caller's to end; only one
    # the block left open is rolled back before recording the interruption.
    outer = conn.in_transaction
    task = Run(conn, source, retry_of)
    try:
        yield task
    except BaseException:
        if conn.in_transaction and not outer:
            conn.rollback()
        task.finish(interrupted=True)
        raise
    else:
        task.finish()


def latest_units(conn):
    """One batch query for latest product outcomes, keyed by (id, source, period)."""
    rows = conn.execute("""
        SELECT * FROM (
            SELECT u.*,r.source,
                row_number() OVER (
                    PARTITION BY u.security_id,r.source,u.period
                    ORDER BY u.attempted_at DESC,r.rowid DESC,u.rowid DESC
                ) AS recency
            FROM catalog_sync_units u JOIN catalog_sync_runs r ON r.id=u.run_id
            WHERE u.security_id IS NOT NULL
        ) WHERE recency=1
    """).fetchall()
    return {(r["security_id"], r["source"], r["period"]): dict(r) for r in rows}


def latest_successes(conn):
    """Like `latest_units`, but only `success`/`empty` outcomes — a later
    failed retry must not hide when the data was last confirmed."""
    rows = conn.execute("""
        SELECT * FROM (
            SELECT u.*,r.source,
                row_number() OVER (
                    PARTITION BY u.security_id,r.source,u.period
                    ORDER BY u.attempted_at DESC,r.rowid DESC,u.rowid DESC
                ) AS recency
            FROM catalog_sync_units u JOIN catalog_sync_runs r ON r.id=u.run_id
            WHERE u.security_id IS NOT NULL AND u.status IN ('success','empty')
        ) WHERE recency=1
    """).fetchall()
    return {(r["security_id"], r["source"], r["period"]): dict(r) for r in rows}


def distribution_source(market):
    return (
        "twse_etf_distribution_list"
        if market == "TWSE"
        else "tpex_etf_distribution_list"
    )


def years_covered(units, security_id, market, years) -> bool:
    """True only if every given year's ETF-distribution-list sync for
    this security ended `success`/`empty` — i.e. we can trust that no
    event in that year was missed, not just that the events we do have
    happen to look complete. Shared by `catalog.query.event_coverage`
    (near-term yield completeness) and `frequency_inference` (design
    005 §4.4 follow-up: a clean 12-month gap between two known events
    is only trustworthy as "annual" once both years it spans are
    confirmed, not just assumed, complete)."""
    source = distribution_source(market)
    for year in years:
        unit = units.get((security_id, source, str(year)))
        if not unit or unit["status"] not in ("success", "empty"):
            return False
    return True


def get_report(conn, run_id):
    row = conn.execute(
        "SELECT * FROM catalog_sync_runs WHERE id=?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    units = [
        dict(r)
        for r in conn.execute(
            """
            SELECT u.*,s.code,s.name,s.market FROM catalog_sync_units u LEFT
            JOIN securities s ON s.id=u.security_id WHERE run_id=? ORDER BY
            u.unit_key
            """,
            (run_id,),
        )
    ]
    counts = {}
    for unit in units:
        try:
            unit["field_outcomes"] = json.loads(unit.get("field_results") or "{}")
        except (TypeError, ValueError):
            unit["field_outcomes"] = {}
        counts[unit["status"]] = counts.get(unit["status"], 0) + 1
    return {**dict(row), "units": units, "counts": counts}
