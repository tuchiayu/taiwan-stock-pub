"""Durable job admission and recovery, coordinated by OS-released SQLite locks.

The control lease serializes admission/recovery only. Each executing worker
holds its own exclusive lease for its entire lifetime; neither a stale clock
nor PID reuse can grant another worker ownership of the same job.
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from taiwan_stock.catalog.sync_history import (
    IN_PROGRESS,
    MAINTENANCE,
    NOT_LISTED_STATUSES,
    SETTLED,
    SOURCE_CHANGED,
    SyncInProgress,
    sql_in,
)
from taiwan_stock.infrastructure.db import get_runtime_connection, write_transaction
from taiwan_stock.shared.dates import now_utc_iso
from taiwan_stock.shared.ids import new_id

ACTIVE = ("queued", "running")
# Recovery leaves a fresh queued job to its submitter for this long; the
# submitter waits at most WORKER_START_SECONDS for a live worker to claim it.
QUEUED_GRACE_SECONDS = 90
WORKER_START_SECONDS = 60
ARTIFACT_KEEP_DAYS = 30
RESOURCES = {
    "market-calendar": ("market-calendar",),
    "etf-scale": ("etf-scale",),
    "prices": ("prices:TWSE", "prices:TPEx"),
    "catalog": ("catalog", "delistings"),
    "stock-announcements": ("stock-dividends",),
    "held-etf-distributions": ("etf-distributions:TWSE", "etf-distributions:TPEx"),
    "full-etf-distributions": ("etf-distributions:TWSE", "etf-distributions:TPEx"),
    "investment-trust": ("investment-trust", "catalog"),
    # Design 052: not the distribution sources, so 領息月曆 stays usable;
    # the source lock keeps the two apart while distributions run.
    "etf-data": ("catalog", "delistings", "investment-trust"),
    "delistings": ("delistings",),
    "stock-dividends": ("stock-dividends",),
}
SOURCE_RESOURCES = {
    "twse_etf_distribution_list": "etf-distributions:TWSE",
    "tpex_etf_distribution_list": "etf-distributions:TPEx",
    "investment_trust": "investment-trust",
    "etf_profile_basic": "catalog",
    "security_delistings": "delistings",
}


def source_resources(source):
    if source == "investment_trust":
        return RESOURCES["investment-trust"]
    return (SOURCE_RESOURCES.get(source, source),)


class Conflict(SyncInProgress):
    def __init__(self, job_id=None):
        self.job_id = job_id
        super().__init__(
            "同步進行中，請稍後再試"
            if job_id
            else "此來源同步中；舊版同步紀錄需確認舊程序停止後執行 sync recover-legacy"
        )


@contextmanager
def lease(data_dir, name, *, timeout=0):
    conn = sqlite3.connect(Path(data_dir) / name, isolation_level=None, timeout=timeout)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        yield
    finally:
        conn.close()


def job_lease(data_dir, job_id):
    # IDs always originate in SQLite, never interpolate an unvalidated API path.
    return lease(data_dir, f".sync-job-{job_id}.db")


def get(conn, job_id):
    row = conn.execute("SELECT * FROM sync_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    for field in ("parameters", "plan", "result"):
        result[field] = json.loads(result[field]) if result[field] else None
    # Ownership is private even on the local API.
    result.pop("owner_token")
    result["server_now"] = now_utc_iso()
    result["status_url"] = f"/api/v2/sync-jobs/{job_id}"
    result["reports"] = [
        {"id": r["id"], "source": r["source"], "status": r["status"]}
        for r in conn.execute(
            "SELECT id,source,status FROM catalog_sync_runs WHERE job_id=?", (job_id,)
        )
    ]
    # `failed_units` counts only transient failures; these are shown apart.
    counts = conn.execute(
        "SELECT sum(status IN " + sql_in(SOURCE_CHANGED) + ") AS source_changed, "
        "sum(status IN " + sql_in(NOT_LISTED_STATUSES) + ") AS not_listed "
        "FROM catalog_sync_units WHERE run_id IN "
        "(SELECT id FROM catalog_sync_runs WHERE job_id=?)",
        (job_id,),
    ).fetchone()
    result["unit_outcomes"] = {
        "source_changed": counts["source_changed"] or 0,
        "not_listed": counts["not_listed"] or 0,
        # `[(issuer or source, count)]`, e.g. [("元大", 54)] (design 055).
        "maintenance": [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT COALESCE(u.issuer, r.source), count(*) "
                "FROM catalog_sync_units u JOIN catalog_sync_runs r "
                "ON r.id=u.run_id WHERE r.job_id=? AND u.status=? "
                "GROUP BY 1 ORDER BY 2 DESC",
                (job_id, MAINTENANCE),
            )
        ],
    }
    return result


def _interrupt(conn, job_id, reason, *, status="interrupted"):
    stamp = now_utc_iso()
    with write_transaction(conn):
        conn.execute(
            "UPDATE catalog_sync_units SET status='interrupted',finished_at=? "
            "WHERE status='pending' AND run_id IN "
            "(SELECT id FROM catalog_sync_runs WHERE job_id=? AND status='running')",
            (stamp, job_id),
        )
        conn.execute(
            "UPDATE catalog_sync_runs SET status='interrupted',finished_at=? "
            "WHERE job_id=? AND status='running'",
            (stamp, job_id),
        )
        conn.execute(
            "UPDATE sync_jobs SET status=?,finished_at=?,error=? "
            "WHERE id=? AND status IN ('queued','running')",
            (status, stamp, reason, job_id),
        )
        conn.execute("DELETE FROM sync_job_resources WHERE job_id=?", (job_id,))


def _grace_cutoff():
    moment = datetime.now(UTC) - timedelta(seconds=QUEUED_GRACE_SECONDS)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _recover(conn, data_dir):
    # A just-submitted job has no worker lease yet; its submitter is still
    # waiting for the worker outside the control lease (#115 #4). A cancelled
    # one needs no such protection (design 067): a worker claims a job only
    # if it is still queued under this same lease.
    for row in conn.execute(
        "SELECT id FROM sync_jobs WHERE status='running' OR "
        "(status='queued' AND (submitted_at<=? OR cancel_requested=1))",
        (_grace_cutoff(),),
    ).fetchall():
        try:
            with job_lease(data_dir, row["id"]):
                # Re-read under the lease. A completing worker may have won.
                job = get(conn, row["id"])
                if job["status"] in ACTIVE:
                    _interrupt(
                        conn,
                        job["id"],
                        "已在工作程序啟動前取消"
                        if job["status"] == "queued" and job["cancel_requested"]
                        else "執行程序已退出，可重試未完成的工作",
                    )
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise


def recover(conn, data_dir):
    with lease(data_dir, ".sync-control.db", timeout=10):
        _recover(conn, data_dir)


def recover_restored(conn):
    stamp = now_utc_iso()
    with write_transaction(conn):
        conn.execute(
            "UPDATE catalog_sync_units SET status='interrupted',finished_at=? "
            "WHERE status='pending' AND run_id IN "
            "(SELECT id FROM catalog_sync_runs WHERE status='running')",
            (stamp,),
        )
        conn.execute(
            "UPDATE catalog_sync_runs SET status='interrupted',finished_at=? "
            "WHERE status='running'",
            (stamp,),
        )
        conn.execute(
            "UPDATE sync_jobs SET status='interrupted',finished_at=?,error=? "
            "WHERE status IN ('queued','running')",
            (stamp, "自備份還原時已中斷未完成的工作"),
        )
        conn.execute("DELETE FROM sync_job_resources")


def snapshot(conn, data_dir, *, recover_jobs=True):
    """Page rendering passes recover_jobs=False: read only, no lease (#115 #3)."""
    if recover_jobs:
        recover(conn, data_dir)
    active = [
        get(conn, r[0])
        for r in conn.execute(
            "SELECT id FROM sync_jobs WHERE status IN ('queued','running') ORDER "
            "BY submitted_at"
        )
    ]
    reserved = {
        r[0] for r in conn.execute("SELECT resource_key FROM sync_job_resources")
    }
    legacy = [
        r[0]
        for r in conn.execute(
            "SELECT source FROM catalog_sync_runs WHERE status='running' AND "
            "job_id IS NULL"
        )
    ]
    reserved.update(key for source in legacy for key in source_resources(source))
    return {
        "jobs": active,
        "server_now": now_utc_iso(),
        "legacy_sources": legacy,
        "blocked_operations": [
            op for op, keys in RESOURCES.items() if reserved.intersection(keys)
        ],
    }


def validate(conn, operation, options):
    allowed = {
        "market-calendar": {"year", "force"},
        "etf-scale": {"force"},
        "full-etf-distributions": {"years", "reference_year"},
        "stock-dividends": {"years"},
        "investment-trust": {"issuer"},
        "retry": {"run_id"},
    }
    if operation not in RESOURCES and operation != "retry":
        raise ValueError("不支援的同步操作")
    if not isinstance(options, dict) or set(options) - allowed.get(operation, set()):
        raise ValueError("不支援的同步選項")
    if operation == "market-calendar":
        from taiwan_stock.catalog.market_calendar import supported

        year = options.get("year")
        if type(year) is not int or not supported(year):
            raise ValueError("不支援的休市資料年度")
    if type(options.get("force", False)) is not bool:
        raise ValueError("force 必須為布林值")
    for key in ("years", "reference_year"):
        if key in options and options[key] is not None:
            value = options[key]
            low, high = (1, 100) if key == "years" else (1912, 9999)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} 超出允許範圍")
    if "issuer" in options:
        from taiwan_stock.catalog.investment_trust_adapters import ISSUER_NAME_PREFIXES

        if (
            options["issuer"] is not None
            and options["issuer"] not in ISSUER_NAME_PREFIXES
        ):
            raise ValueError("不支援的投信")
    if operation == "retry":
        row = conn.execute(
            "SELECT * FROM catalog_sync_runs WHERE id=?", (options.get("run_id"),)
        ).fetchone()
        if (
            not row
            or row["status"] == "running"
            or row["source"] not in SOURCE_RESOURCES
        ):
            raise ValueError("不是可重試的來源紀錄")
        return source_resources(row["source"])
    return RESOURCES[operation]


def launch_worker(data_dir, job_id):
    flags = {}
    if os.name == "nt":
        # DETACHED_PROCESS lets the Windows venv redirector's child allocate a
        # visible console. CREATE_NO_WINDOW also keeps that child windowless;
        # the new process group isolates it from the parent's console signals.
        flags["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        flags["start_new_session"] = True
    log_dir = Path(data_dir) / "sync-logs"
    log_dir.mkdir(exist_ok=True)
    with (log_dir / f"{job_id}.log").open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "taiwan_stock.sync_worker",
                str(Path(data_dir).resolve()),
                job_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            **flags,
        )
    # Reaping does not tie a worker's lifetime to its parent.
    threading.Thread(target=process.wait, daemon=True).start()
    return process


def submit(
    conn,
    data_dir,
    operation,
    options=None,
    *,
    origin="web",
    request_key=None,
    retry_of=None,
    launcher=None,
    admit=None,
):
    options = {} if options is None else options
    resources = validate(conn, operation, options)
    request_key = new_id() if request_key is None else request_key
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
        raise ValueError("request_key 長度必須為 1–128")
    with lease(data_dir, ".sync-control.db", timeout=10):
        _recover(conn, data_dir)
        previous = conn.execute(
            "SELECT id FROM sync_jobs WHERE request_key=?", (request_key,)
        ).fetchone()
        if previous:
            job = get(conn, previous[0])
            if (
                job["operation"] != operation
                or job["parameters"] != options
                or job["retry_of"] != retry_of
            ):
                raise ValueError("request_key 已用於不同的請求")
            return job
        # Recheck freshness under the same cross-process admission lease.
        if admit is not None and not admit():
            return None
        if retry_of:
            prior = get(conn, retry_of)
            if not prior or prior["status"] not in ("failed", "partial", "interrupted"):
                raise ValueError("不是可重試的任務")
            if prior["operation"] != operation or prior["parameters"] != options:
                raise ValueError("重試範圍必須與原任務相同")
        for resource in resources:
            row = conn.execute(
                "SELECT job_id FROM sync_job_resources WHERE resource_key=?",
                (resource,),
            ).fetchone()
            if row:
                raise Conflict(row[0])
        for row in conn.execute(
            "SELECT source FROM catalog_sync_runs WHERE status='running' AND "
            "job_id IS NULL"
        ):
            if set(source_resources(row[0])).intersection(resources):
                raise Conflict()
        job_id = new_id()
        plan = json.dumps(prior["plan"]) if retry_of and prior["plan"] else None
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO "
                "sync_jobs(id,operation,origin,request_key,parameters,plan,retry_of,"
                "status,submitted_at,owner_token) "
                "VALUES(?,?,?,?,?,?,?,'queued',?,?)",
                (
                    job_id,
                    operation,
                    origin,
                    request_key,
                    json.dumps(options),
                    plan,
                    retry_of,
                    now_utc_iso(),
                    new_id(),
                ),
            )
            conn.executemany(
                "INSERT INTO sync_job_resources VALUES(?,?)",
                [(r, job_id) for r in resources],
            )
        prune_artifacts(conn, data_dir)
        try:
            process = (launcher or launch_worker)(data_dir, job_id)
        except Exception:
            process = _LAUNCH_FAILED
    # Wait outside the control lease so other submissions and CLI waits are
    # not blocked; the queued grace period keeps recovery off this job.
    try:
        if process is _LAUNCH_FAILED:
            raise RuntimeError("launch failed")
        # Without a process handle liveness is unknown, so keep the short wait.
        limit = 10 if process is None else WORKER_START_SECONDS
        deadline = time.monotonic() + limit
        while get(conn, job_id)["status"] == "queued" and time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                break
            threading.Event().wait(0.025)
        if get(conn, job_id)["status"] == "queued":
            # Recovery keeps the lease while invalidating the owner. A late
            # worker cannot run after these resource reservations are freed.
            with job_lease(data_dir, job_id):
                _interrupt(
                    conn,
                    job_id,
                    "工作程序未能啟動，請檢查任務日誌並重試",
                    status="failed",
                )
    except Exception:
        try:
            with job_lease(data_dir, job_id):
                _interrupt(
                    conn,
                    job_id,
                    "工作程序啟動失敗，請檢查任務日誌並重試",
                    status="failed",
                )
        except sqlite3.OperationalError:
            pass  # A worker that already owns the lease remains authoritative.
    return get(conn, job_id)


_LAUNCH_FAILED = object()


def prune_artifacts(conn, data_dir, *, keep_days=ARTIFACT_KEEP_DAYS):
    """Drop lease files and logs of jobs finished long ago (#115 #6)."""
    moment = datetime.now(UTC) - timedelta(days=keep_days)
    cutoff = moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = conn.execute(
        "SELECT id FROM sync_jobs WHERE status NOT IN ('queued','running') "
        "AND finished_at IS NOT NULL AND finished_at<?",
        (cutoff,),
    ).fetchall()
    for row in rows:
        for path in (
            Path(data_dir) / f".sync-job-{row['id']}.db",
            Path(data_dir) / "sync-logs" / f"{row['id']}.log",
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # Still open elsewhere; a later submission retries.


def retry(conn, data_dir, job_id, **kwargs):
    job = get(conn, job_id)
    if not job:
        raise ValueError("找不到任務")
    return submit(
        conn, data_dir, job["operation"], job["parameters"], retry_of=job_id, **kwargs
    )


def wait(conn, data_dir, job_id, *, on_progress=None):
    last = None
    while True:
        recover(conn, data_dir)
        job = get(conn, job_id)
        if (
            on_progress
            and (state := (job["phase"], job["processed"], job["status"])) != last
        ):
            on_progress(job)
            last = state
        if job["status"] not in ACTIVE:
            return job
        threading.Event().wait(0.25)


def request_cancel(conn, job_id):
    conn.execute(
        "UPDATE sync_jobs SET cancel_requested=1 WHERE id=? AND status IN "
        "('queued','running')",
        (job_id,),
    )


class Progress:
    def __init__(self, conn, job_id, owner_token):
        self.conn, self.job_id, self.owner_token = conn, job_id, owner_token
        self.phase = "執行中"
        # Design 054 §4.2.3: extra live text after the phase, e.g. 已取得 X/Y.
        self.detail = None
        self.last_update = 0
        self.last_phase = None

    def update(self, *, force=False):
        from taiwan_stock.shared.sync_progress import Cancelled

        job = self.conn.execute(
            "SELECT * FROM sync_jobs WHERE id=?", (self.job_id,)
        ).fetchone()
        if (
            job["owner_token"] != self.owner_token
            or job["status"] != "running"
            or job["cancel_requested"]
        ):
            raise Cancelled()
        stamp = time.monotonic()
        phase = f"{self.phase} · {self.detail}" if self.detail else self.phase
        if not force and phase == self.last_phase and stamp - self.last_update < 0.25:
            return
        self.last_update, self.last_phase = stamp, phase
        counts = self.conn.execute(
            "SELECT count(*) AS total, sum(status!='pending') AS processed, "
            "sum(status NOT IN "
            # Design 055: maintenance is told apart, never counted as failed.
            + sql_in(SETTLED | SOURCE_CHANGED | IN_PROGRESS | {MAINTENANCE})
            + ") AS failed "
            "FROM catalog_sync_units WHERE run_id IN (SELECT id FROM "
            "catalog_sync_runs WHERE job_id=?)",
            (self.job_id,),
        ).fetchone()
        # Unknown phase size stays NULL; later phases may discover more units.
        self.conn.execute(
            "UPDATE sync_jobs SET "
            "phase=?,processed=?,total=?,failed_units=?,heartbeat_at=? WHERE "
            "id=? AND owner_token=?",
            (
                phase,
                counts["processed"] or 0,
                counts["total"] or None,
                counts["failed"] or 0,
                now_utc_iso(),
                self.job_id,
                self.owner_token,
            ),
        )


FINISH_ATTEMPTS = 3
FINISH_RETRY_SECONDS = 1


def _finish_job(conn, job_id, token, status, payload, error):
    """Record the outcome, retrying a locked database a few times (design
    062) so the job doesn't sit in "running" until the next recovery."""
    for attempt in range(FINISH_ATTEMPTS):
        try:
            with write_transaction(conn):
                conn.execute(
                    "UPDATE sync_jobs SET "
                    "status=?,finished_at=?,result=?,error=? WHERE id=? AND "
                    "owner_token=? AND status='running'",
                    (status, now_utc_iso(), payload, error, job_id, token),
                )
                conn.execute("DELETE FROM sync_job_resources WHERE job_id=?", (job_id,))
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt + 1 == FINISH_ATTEMPTS:
                traceback.print_exc()
                raise
            threading.Event().wait(FINISH_RETRY_SECONDS)


def run_worker(data_dir, job_id, *, client=None, executor=None):
    from taiwan_stock.catalog.operations import execute
    from taiwan_stock.shared import sync_progress
    from taiwan_stock.shared.http import new_client

    with closing(get_runtime_connection(Path(data_dir))) as conn:
        job = get(conn, job_id)
        if job is None:
            return
        try:
            with job_lease(data_dir, job_id):
                row = conn.execute(
                    "SELECT * FROM sync_jobs WHERE id=?", (job_id,)
                ).fetchone()
                if row["status"] != "queued" or {
                    r[0]
                    for r in conn.execute(
                        "SELECT resource_key FROM sync_job_resources WHERE job_id=?",
                        (job_id,),
                    )
                } != set(
                    validate(conn, row["operation"], json.loads(row["parameters"]))
                ):
                    return
                token = row["owner_token"]
                conn.execute(
                    "UPDATE sync_jobs SET "
                    "status='running',started_at=?,owner_pid=?,heartbeat_at=? "
                    "WHERE id=? AND status='queued' AND owner_token=?",
                    (now_utc_iso(), os.getpid(), now_utc_iso(), job_id, token),
                )
                progress = Progress(conn, job_id, token)
                context_token = sync_progress.current.set(progress)
                try:
                    if client is None:
                        with new_client() as actual_client:
                            result = (executor or execute)(
                                conn, actual_client, job["operation"], job["parameters"]
                            )
                    else:
                        result = (executor or execute)(
                            conn, client, job["operation"], job["parameters"]
                        )
                    progress.update(force=True)
                    failed = conn.execute(
                        "SELECT 1 FROM sync_job_steps WHERE job_id=? AND "
                        "status!='success'",
                        (job_id,),
                    ).fetchone()
                    status, error = ("partial" if failed else "success"), None
                except sync_progress.Cancelled:
                    if conn.in_transaction:
                        conn.rollback()
                    _interrupt(conn, job_id, "同步已中止，可重試未完成的工作")
                    return
                except Exception as exc:
                    from taiwan_stock.catalog.sync_history import error_info

                    # Design 062: stderr is the job's sync-logs file.
                    traceback.print_exc()
                    if conn.in_transaction:
                        conn.rollback()
                    result, status, error = None, "failed", error_info(exc)[2]
                finally:
                    sync_progress.current.reset(context_token)
                try:
                    payload = json.dumps(result, ensure_ascii=False, default=str)
                except (TypeError, ValueError) as exc:
                    payload, status = None, "failed"
                    error = f"同步結果無法儲存：{exc}"
                _finish_job(conn, job_id, token, status, payload, error)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
