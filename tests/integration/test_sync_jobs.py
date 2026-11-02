"""Ownership, atomic admission, durable results, and real process lifetimes."""

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import operations, sync_history
from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import (
    DatabaseInUseError,
    exclusive_database_access,
    get_connection,
)
from taiwan_stock.main import create_app
from taiwan_stock.web.deps import get_http_client


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as connection:
        migrate.apply_pending(connection)
        yield connection


@pytest.fixture
def live_launcher():
    gate = threading.Event()
    ready = threading.Event()
    threads = []

    def execute(conn, client, operation, options):
        with sync_history.run(conn, "twse_etf_distribution_list") as run:
            run.plan("x:2026", period="2026")
            ready.set()
            gate.wait(10)
            run.record("x:2026", "empty")
        return {"synced": 0}

    def launch(data_dir, job_id):
        worker = threading.Thread(
            target=sync_jobs.run_worker,
            args=(data_dir, job_id),
            kwargs={"executor": execute},
        )
        worker.start()
        threads.append(worker)

    launch.gate = gate
    launch.ready = ready
    yield launch
    gate.set()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()


def test_live_cli_lease_survives_web_startup_and_keeps_units(
    conn, tmp_path, live_launcher
):
    job = sync_jobs.submit(
        conn, tmp_path, "full-etf-distributions", origin="cli", launcher=live_launcher
    )
    assert live_launcher.ready.wait(5)
    with TestClient(create_app(tmp_path)) as web:
        status = web.get(job["status_url"]).json()
        assert status["status"] == "running"
        assert status["started_at"] == job["started_at"]
        assert "owner_token" not in status
        assert (
            conn.execute("SELECT status FROM catalog_sync_units").fetchone()[0]
            == "pending"
        )
        with pytest.raises(sync_jobs.Conflict) as conflict:
            sync_jobs.submit(conn, tmp_path, "held-etf-distributions")
        assert conflict.value.job_id == job["id"]
        for path in ("/", "/securities", "/corporate-actions", "/income-calendar"):
            page = web.get(path)
            assert page.status_code == 200
            assert "data-sync-operation=" in page.text
        page = web.get("/income-calendar")
        form = page.text[
            page.text.index('data-sync-operation="held-etf-distributions"') :
        ]
        assert "disabled" in form[: form.index("</button>")]
        assert job["id"] in page.text


def test_clock_and_pid_never_recover_an_owned_job(conn, tmp_path, live_launcher):
    job = sync_jobs.submit(
        conn, tmp_path, "full-etf-distributions", launcher=live_launcher
    )
    conn.execute(
        "UPDATE sync_jobs SET heartbeat_at='1900-01-01',owner_pid=-1 WHERE id=?",
        (job["id"],),
    )
    sync_jobs.recover(conn, tmp_path)
    assert sync_jobs.get(conn, job["id"])["status"] == "running"


def test_atomic_conflict_idempotency_and_option_mismatch(conn, tmp_path, live_launcher):
    first = sync_jobs.submit(
        conn,
        tmp_path,
        "full-etf-distributions",
        request_key="same",
        launcher=live_launcher,
    )
    second = sync_jobs.submit(
        conn,
        tmp_path,
        "full-etf-distributions",
        request_key="same",
        launcher=lambda *_: pytest.fail("duplicate worker"),
    )
    assert second["id"] == first["id"]
    with pytest.raises(ValueError):
        sync_jobs.submit(conn, tmp_path, "prices", request_key="same")
    assert conn.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM sync_job_resources").fetchone()[0] == 2


def test_legacy_running_source_is_preserved_and_blocks_admission(conn, tmp_path):
    run = sync_history.Run(conn, "twse_etf_distribution_list")
    run.plan("old")
    with TestClient(create_app(tmp_path)) as web:
        assert (
            conn.execute("SELECT status FROM catalog_sync_runs").fetchone()[0]
            == "running"
        )
        response = web.post(
            "/api/v2/sync-jobs", json={"operation": "held-etf-distributions"}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["active_job_id"] is None
        assert web.get("/api/v2/sync-jobs").json()["legacy_sources"] == [
            "twse_etf_distribution_list"
        ]


def test_spawn_failure_releases_every_resource_and_late_worker_cannot_execute(
    conn, tmp_path
):
    def fail(*_):
        raise OSError("spawn failed")

    job = sync_jobs.submit(conn, tmp_path, "catalog", launcher=fail)
    assert job["status"] == "failed"
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()
    sync_jobs.run_worker(
        tmp_path, job["id"], executor=lambda *_: pytest.fail("late worker executed")
    )
    assert sync_jobs.get(conn, job["id"])["status"] == "failed"


def test_composite_reservation_is_not_released_between_phases(conn, tmp_path):
    checks = []

    def execute(connection, client, operation, options):
        for phase in ("TWSE", "TPEx"):
            checks.append(phase)
            # Do not reenter the admission control lease while submitting;
            # resource ownership itself spans both phases.
            assert (
                connection.execute(
                    "SELECT count(*) FROM sync_job_resources WHERE job_id="
                    "(SELECT id FROM sync_jobs WHERE status='running')"
                ).fetchone()[0]
                == 2
            )
        return {}

    job = sync_jobs.submit(
        conn,
        tmp_path,
        "catalog",
        launcher=lambda d, j: sync_jobs.run_worker(d, j, executor=execute),
    )
    assert job["status"] == "success" and checks == ["TWSE", "TPEx"]
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()


def test_notice_skip_does_not_hide_real_error_and_only_error_is_retried(conn, tmp_path):
    attempts = []
    broken = True

    def execute(connection, client, operation, options):
        def source():
            errors = {}
            with sync_history.run(connection, "security_delistings") as run:
                for key in ("preliminary", "invalid-date"):
                    if not run.wanted(key):
                        continue
                    attempts.append(key)
                    run.plan(key)
                    if key == "preliminary":
                        run.record(key, "skipped_notice", reason="日期將另行公告")
                    elif broken:
                        run.record(key, "parse_failed", exc=ValueError("日期互相矛盾"))
                        errors[key] = "日期互相矛盾"
                    else:
                        run.record(key, "success", count=1)
            return {"errors": errors, "run_id": run.id}

        return operations.step(connection, "delistings", source)

    def launch(data_dir, job_id):
        sync_jobs.run_worker(data_dir, job_id, executor=execute)

    first = sync_jobs.submit(conn, tmp_path, "delistings", launcher=launch)
    assert first["status"] == "partial"
    # A format error needs a code fix: shown apart from transient failures.
    assert first["failed_units"] == 0
    assert sync_jobs.get(conn, first["id"])["unit_outcomes"] == {
        "source_changed": 1,
        "not_listed": 0,
        "maintenance": [],
    }
    assert first["processed"] == first["total"] == 2
    report = sync_history.get_report(conn, first["result"]["run_id"])
    assert report["status"] == "failed"
    assert report["counts"] == {"skipped_notice": 1, "parse_failed": 1}
    broken = False
    attempts.clear()
    second = sync_jobs.retry(conn, tmp_path, first["id"], launcher=launch)
    assert second["status"] == "success"
    assert second["failed_units"] == 0
    assert attempts == ["invalid-date"]


def test_maintenance_is_partial_not_failed_and_is_retried(conn, tmp_path):
    """Design 055: 來源維護中 keeps the job partial, counts no failure, says
    so in one sentence, and a retry tries exactly those units again."""
    from taiwan_stock.web.routes_sync_jobs import result_summary

    attempts = []
    down = True

    def execute(connection, client, operation, options):
        def source():
            with sync_history.run(connection, "investment_trust") as run:
                for key, issuer in (("國泰:00878", "國泰"), ("元大:0050", "元大"),
                                    ("元大:0056", "元大")):  # fmt: skip
                    if not run.wanted(key):
                        continue
                    attempts.append(key)
                    run.plan(key, issuer=issuer)
                    if issuer == "元大" and down:
                        run.record(
                            key, "parse_failed", exc=sync_history.SourceUnavailable()
                        )
                    else:
                        run.record(key, "success", count=1)
            return {"run_id": run.id}

        return operations.step(connection, "投信保管銀行／配息頻率", source)

    def launch(data_dir, job_id):
        sync_jobs.run_worker(data_dir, job_id, executor=execute)

    first = sync_jobs.submit(conn, tmp_path, "delistings", launcher=launch)
    job = sync_jobs.get(conn, first["id"])

    assert first["status"] == "partial"
    assert first["failed_units"] == 0
    assert job["unit_outcomes"]["maintenance"] == [("元大", 2)]
    assert result_summary(job).endswith("元大網站維護中，2 檔沿用舊資料，可稍後重試")

    down = False
    attempts.clear()
    second = sync_jobs.retry(conn, tmp_path, first["id"], launcher=launch)
    assert second["status"] == "success"
    assert attempts == ["元大:0050", "元大:0056"]


def test_summary_lists_source_changes_and_not_listed_apart_from_failures():
    from taiwan_stock.web.routes_sync_jobs import result_summary

    job = {
        "operation": "catalog",
        "result": {
            "stock_count": 1,
            "etf_count": 2,
            "tpex_stock_count": 0,
            "tpex_etf_count": 0,
        },
        "unit_outcomes": {"source_changed": 1, "not_listed": 3},
    }
    assert result_summary(job) == "已同步 3 檔商品 · 來源待更新 1、來源未收錄 3"
    job["unit_outcomes"] = {"source_changed": 0, "not_listed": 0}
    assert result_summary(job) == "已同步 3 檔商品"
    assert result_summary({**job, "result": None}) == ""


def test_retry_preserves_successful_steps_and_runs_failed_source_units_only(
    conn, tmp_path
):
    calls = []
    fail = True

    def execute(connection, client, operation, options):
        def source():
            with sync_history.run(connection, "twse_etf_distribution_list") as run:
                for key in ("ok:2026", "bad:2026"):
                    if run.wanted(key):
                        calls.append(key)
                        run.plan(key, period="2026")
                        run.record(
                            key,
                            "http_failed" if key == "bad:2026" and fail else "empty",
                        )
            return {}

        operations.step(connection, "complete", lambda: calls.append("complete") or {})
        return operations.step(connection, "source", source)

    def launcher(d, j):
        sync_jobs.run_worker(d, j, executor=execute)

    first = sync_jobs.submit(
        conn, tmp_path, "full-etf-distributions", launcher=launcher
    )
    assert first["status"] == "partial"
    fail = False
    second = sync_jobs.retry(conn, tmp_path, first["id"], launcher=launcher)
    assert second["status"] == "success"
    assert calls == ["complete", "ok:2026", "bad:2026", "bad:2026"]


def test_api_validation_not_found_and_origin_defense(tmp_path):
    with TestClient(create_app(tmp_path)) as web:
        for payload in (
            {"operation": "shell"},
            {"operation": "prices", "options": {"path": "x"}},
            {"operation": "full-etf-distributions", "options": {"years": True}},
        ):
            assert web.post("/api/v2/sync-jobs", json=payload).status_code == 422
        assert web.get("/api/v2/sync-jobs/../missing").status_code == 404
        assert (
            web.post(
                "/api/v2/sync-jobs",
                json={"operation": "prices"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )


def test_four_forms_keep_job_id_result_and_filter_without_js(tmp_path):
    app = create_app(tmp_path)

    def client_override():
        with httpx2.Client(
            transport=httpx2.MockTransport(lambda r: httpx2.Response(503))
        ) as client:
            yield client

    app.dependency_overrides[get_http_client] = client_override
    with TestClient(app) as web:
        for path, data in (
            ("/prices/sync", {}),
            (
                "/securities/sync",
                {"return_to": "/securities?security_type=etf&sort=bank"},
            ),
            (
                "/securities/profiles/sync",
                {"return_to": "/securities?security_type=etf&sort=bank"},
            ),
            ("/corporate-actions/sync", {}),
            ("/income-calendar/refresh-etf", {"month": "2026-10", "filter": "all"}),
        ):
            response = web.post(path, data=data, follow_redirects=False)
            assert response.status_code == 303
            parsed = urlparse(response.headers["location"])
            query = parse_qs(parsed.query)
            job_id = query["job_id"][0]
            assert web.get(f"/sync-jobs/{job_id}").status_code == 200
            if "month" in data:
                assert query["month"] == ["2026-10"]
            if "return_to" in data:
                assert query["sort"] == ["bank"]


@pytest.mark.real_workers
def test_detached_worker_survives_parent_kill_then_recovers_real_worker_death(
    conn, tmp_path
):
    """The same launch flags as production, with offline work injected in child."""
    child = """
import sys, threading
from pathlib import Path
from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.catalog import sync_history
def work(conn, client, operation, options):
    with sync_history.run(conn, "twse_etf_distribution_list") as run:
        run.plan("offline:2026", period="2026")
        Path(sys.argv[1], "worker-ready").write_text(str(__import__("os").getpid()))
        threading.Event().wait(60)
        run.record("offline:2026", "empty")
    return {}
sync_jobs.run_worker(Path(sys.argv[1]), sys.argv[2], executor=work)
"""
    parent = """
import sys, subprocess, threading
from pathlib import Path
from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.infrastructure.db import get_connection
original = subprocess.Popen
def offline(args, **kwargs):
    return original([args[0], "-c", sys.argv[2], *args[-2:]], **kwargs)
subprocess.Popen = offline
conn = get_connection(Path(sys.argv[1]))
job = sync_jobs.submit(conn, Path(sys.argv[1]), "full-etf-distributions")
Path(sys.argv[1], "parent-ready").write_text(job["id"])
threading.Event().wait(60)
"""
    parent_process = subprocess.Popen(
        [sys.executable, "-c", parent, str(tmp_path), child],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    worker_pid = None
    try:
        deadline = __import__("time").monotonic() + 15
        while (
            not all(
                (tmp_path / name).exists() for name in ("parent-ready", "worker-ready")
            )
            and __import__("time").monotonic() < deadline
        ):
            assert parent_process.poll() is None
            threading.Event().wait(0.05)
        assert (tmp_path / "parent-ready").exists()
        assert (tmp_path / "worker-ready").exists()
        worker_pid = int((tmp_path / "worker-ready").read_text())
        job_id = (tmp_path / "parent-ready").read_text()
        parent_process.kill()
        parent_process.wait(timeout=5)
        # Restarting Web must preserve the worker and its pending unit.
        with TestClient(create_app(tmp_path)) as web:
            assert web.get(f"/api/v2/sync-jobs/{job_id}").json()["status"] == "running"
            conn.close()
            with pytest.raises(DatabaseInUseError):
                with exclusive_database_access(tmp_path):
                    pass
        conn = get_connection(tmp_path)
        import os
        import signal

        os.kill(worker_pid, signal.SIGTERM)
        deadline = __import__("time").monotonic() + 10
        while __import__("time").monotonic() < deadline:
            sync_jobs.recover(conn, tmp_path)
            if sync_jobs.get(conn, job_id)["status"] == "interrupted":
                break
            threading.Event().wait(0.05)
        assert sync_jobs.get(conn, job_id)["status"] == "interrupted"
        assert (
            conn.execute("SELECT status FROM catalog_sync_units").fetchone()[0]
            == "interrupted"
        )
        assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()
        worker_pid = None
    finally:
        if parent_process.poll() is None:
            parent_process.kill()
        parent_process.wait(timeout=5)
        if parent_process.stderr:
            parent_process.stderr.close()
        if worker_pid is None and (tmp_path / "worker-ready").exists():
            worker_pid = int((tmp_path / "worker-ready").read_text())
        if worker_pid:
            import os
            import signal

            try:
                os.kill(worker_pid, signal.SIGTERM)
            except OSError:
                pass
        conn.close()


def test_simultaneous_requests_have_one_winner(conn, tmp_path, live_launcher):
    def request(key):
        with closing(get_connection(tmp_path)) as connection:
            try:
                return sync_jobs.submit(
                    connection,
                    tmp_path,
                    "full-etf-distributions",
                    request_key=key,
                    launcher=live_launcher,
                )["id"]
            except sync_jobs.Conflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, ("one", "two")))
    assert results.count("conflict") == 1
    assert conn.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 1


def test_nonconflicting_jobs_run_concurrently_and_data_dirs_are_isolated(
    conn, tmp_path
):
    gate = threading.Event()
    threads = []

    def work(*_):
        gate.wait(10)
        return {}

    def launcher(directory, job_id):
        thread = threading.Thread(
            target=sync_jobs.run_worker,
            args=(directory, job_id),
            kwargs={"executor": work},
        )
        threads.append(thread)
        thread.start()

    other = tmp_path / "other"
    other.mkdir()
    try:
        first = sync_jobs.submit(conn, tmp_path, "prices", launcher=launcher)
        second = sync_jobs.submit(conn, tmp_path, "catalog", launcher=launcher)
        with closing(get_connection(other)) as other_conn:
            migrate.apply_pending(other_conn)
            third = sync_jobs.submit(other_conn, other, "prices", launcher=launcher)
            assert third["status"] == "running"
        assert first["status"] == second["status"] == "running"
        assert len(sync_jobs.snapshot(conn, tmp_path)["jobs"]) == 2
    finally:
        gate.set()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()


def test_explicit_cancellation_finishes_as_interrupted(conn, tmp_path, live_launcher):
    job = sync_jobs.submit(
        conn, tmp_path, "full-etf-distributions", launcher=live_launcher
    )
    sync_jobs.request_cancel(conn, job["id"])
    # The injected worker checks cancellation at its next unit boundary.
    live_launcher.gate.set()
    finished = sync_jobs.wait(conn, tmp_path, job["id"])
    assert finished["status"] == "interrupted"
    assert (
        conn.execute("SELECT status FROM catalog_sync_runs").fetchone()[0]
        == "interrupted"
    )
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()


def test_submitter_dies_before_handshake_and_late_worker_is_fenced(conn, tmp_path):
    def parent_died(*_):
        raise SystemExit()

    with pytest.raises(SystemExit):
        sync_jobs.submit(conn, tmp_path, "prices", launcher=parent_died)
    job_id = conn.execute("SELECT id FROM sync_jobs").fetchone()[0]
    # A fresh queued job belongs to its submitter during the grace period.
    sync_jobs.recover(conn, tmp_path)
    assert sync_jobs.get(conn, job_id)["status"] == "queued"
    conn.execute(
        "UPDATE sync_jobs SET submitted_at='2000-01-01T00:00:00.000Z' WHERE id=?",
        (job_id,),
    )
    sync_jobs.recover(conn, tmp_path)
    assert sync_jobs.get(conn, job_id)["status"] == "interrupted"
    sync_jobs.run_worker(
        tmp_path, job_id, executor=lambda *_: pytest.fail("late worker was not fenced")
    )
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()


def test_legacy_recovery_requires_every_connection_to_be_closed(tmp_path):
    from taiwan_stock import cli

    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    run = sync_history.Run(connection, "investment_trust")
    run.plan("legacy")
    command = ["sync", "recover-legacy", "--data-dir", str(tmp_path)]
    try:
        assert cli.main(command) == 1
        assert (
            connection.execute("SELECT status FROM catalog_sync_runs").fetchone()[0]
            == "running"
        )
    finally:
        connection.close()
    assert cli.main(command) == 0
    with closing(get_connection(tmp_path)) as connection:
        assert (
            connection.execute("SELECT status FROM catalog_sync_units").fetchone()[0]
            == "interrupted"
        )


def test_cli_job_status_and_retry_validation(conn, tmp_path, capsys):
    import json

    from taiwan_stock import cli

    job = sync_jobs.submit(
        conn,
        tmp_path,
        "prices",
        launcher=lambda d, j: sync_jobs.run_worker(
            d, j, executor=lambda *_: {"done": True}
        ),
    )
    assert cli.main(["sync", "job-status", job["id"], "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"
    assert cli.main(["sync", "retry-job", job["id"], "--data-dir", str(tmp_path)]) == 1
    assert "不是可重試的任務" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["sync", "etf-distributions", "--full", "--held-only"]
        )


def test_restoring_pre_jobs_backup_upgrades_a_copy_and_keeps_original(
    tmp_path, monkeypatch
):
    import hashlib

    from taiwan_stock.infrastructure import backup

    versions = migrate.discover_migrations()
    with closing(get_connection(tmp_path)) as connection:
        with monkeypatch.context() as context:
            context.setattr(
                migrate,
                "discover_migrations",
                lambda: [m for m in versions if m.version <= 11],
            )
            migrate.apply_pending(connection)
            old_backup = backup.create_backup(tmp_path)
    original_hash = hashlib.sha256(old_backup.read_bytes()).hexdigest()
    with closing(get_connection(tmp_path)) as connection:
        migrate.apply_pending(connection)
    assert hashlib.sha256(old_backup.read_bytes()).hexdigest() == original_hash
    with TestClient(create_app(tmp_path)) as web:
        restored = web.post(
            "/api/v1/backups/restore", json={"filename": old_backup.name}
        )
        assert restored.status_code == 200
        assert hashlib.sha256(old_backup.read_bytes()).hexdigest() == original_hash
        assert web.get("/api/v2/sync-jobs").json()["jobs"] == []
        assert web.get("/settings").status_code == 200


def test_cli_ctrl_c_during_worker_handshake_cancels_queued_job(
    conn, tmp_path, monkeypatch
):
    from taiwan_stock import cli

    def interrupted(*_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(sync_jobs, "launch_worker", interrupted)
    start = time.monotonic()
    assert cli.main(["sync", "delistings", "--data-dir", str(tmp_path)]) == 130
    # Design 067: no 90-second queued grace period for a cancelled job.
    assert time.monotonic() - start < 10
    assert conn.execute("SELECT status FROM sync_jobs").fetchone()[0] == "interrupted"
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()


def cancelled_queued_job(conn, tmp_path):
    def died(*_):
        raise SystemExit()

    with pytest.raises(SystemExit):
        sync_jobs.submit(conn, tmp_path, "prices", launcher=died)
    job_id = conn.execute("SELECT id FROM sync_jobs").fetchone()[0]
    sync_jobs.request_cancel(conn, job_id)
    return job_id


def test_a_cancelled_queued_job_is_interrupted_at_once(conn, tmp_path):
    job_id = cancelled_queued_job(conn, tmp_path)
    sync_jobs.recover(conn, tmp_path)
    job = sync_jobs.get(conn, job_id)
    assert job["status"] == "interrupted"
    assert job["error"] == "已在工作程序啟動前取消"
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()
    # A late worker finds nothing to claim.
    sync_jobs.run_worker(tmp_path, job_id, executor=lambda *_: pytest.fail("ran"))
    assert sync_jobs.get(conn, job_id)["status"] == "interrupted"


def test_a_cancelled_job_a_worker_holds_is_left_to_the_worker(conn, tmp_path):
    job_id = cancelled_queued_job(conn, tmp_path)
    with sync_jobs.job_lease(tmp_path, job_id):
        sync_jobs.recover(conn, tmp_path)
        assert sync_jobs.get(conn, job_id)["status"] == "queued"


@pytest.mark.real_workers
@pytest.mark.skipif(sys.platform != "win32", reason="Windows console creation")
def test_windows_worker_has_no_console_window(conn, tmp_path, monkeypatch):
    # Inspect the actual interpreter after the venv redirector has launched it.
    # Checking Popen's flags alone misses a console created by that redirector.
    child = """
import ctypes, sys
from pathlib import Path
from taiwan_stock.infrastructure import sync_jobs
def work(conn, client, operation, options):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GetConsoleWindow.restype = ctypes.c_void_p
    user.IsWindowVisible.argtypes = [ctypes.c_void_p]
    window = kernel.GetConsoleWindow()
    print("worker-console-check", flush=True)
    return {"console_window": bool(window),
            "visible": bool(user.IsWindowVisible(window)), "prefix": sys.prefix}
sync_jobs.run_worker(Path(sys.argv[1]), sys.argv[2], executor=work)
"""
    original = subprocess.Popen

    def offline(args, **kwargs):
        return original([args[0], "-c", child, *args[-2:]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", offline)
    job = sync_jobs.submit(conn, tmp_path, "prices")
    finished = sync_jobs.wait(conn, tmp_path, job["id"])
    assert finished["status"] == "success"
    assert finished["result"]["console_window"] is False
    assert finished["result"]["visible"] is False
    assert finished["result"]["prefix"] == sys.prefix
    assert (
        "worker-console-check"
        in (tmp_path / "sync-logs" / f"{job['id']}.log").read_text()
    )


@pytest.mark.real_workers
def test_real_worker_module_uses_resolved_data_dir_without_live_requests(
    conn, tmp_path
):
    # There are no ETFs: this exercises the actual -m entry/launch/handshake,
    # and must finish without contacting any source or requiring a Web server.
    job = sync_jobs.submit(conn, tmp_path, "full-etf-distributions")
    finished = sync_jobs.wait(conn, tmp_path, job["id"])
    assert finished["status"] == "success"
    assert finished["result"]["twse"]["synced"] == 0
    assert finished["result"]["tpex"]["synced"] == 0
    assert len(finished["reports"]) == 2


def test_worker_exiting_before_handshake_fails_without_holding_resources(
    conn, tmp_path
):
    class Exited:
        def poll(self):
            return 1

    job = sync_jobs.submit(conn, tmp_path, "prices", launcher=lambda *_: Exited())
    assert job["status"] == "failed"
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()


def test_request_connection_and_coordination_lease_allow_thread_handoff(tmp_path):
    # FastAPI's synchronous dependency entry, endpoint, and exit can each run
    # on a different AnyIO thread, even though they use this connection serially.
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)

    def request_body():
        assert connection.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 0
        connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(request_body).result(timeout=5)
    # Both the main connection and its separate coordination lease must close.
    with exclusive_database_access(tmp_path):
        pass


def test_etf_profile_update_locks_catalog_and_issuer_resources(
    conn, tmp_path, live_launcher
):
    job = sync_jobs.submit(conn, tmp_path, "investment-trust", launcher=live_launcher)
    assert live_launcher.ready.wait(5)
    for operation in ("investment-trust", "catalog"):
        with pytest.raises(sync_jobs.Conflict) as exc:
            sync_jobs.submit(conn, tmp_path, operation)
        assert exc.value.job_id == job["id"]
    assert {
        r[0] for r in conn.execute("SELECT resource_key FROM sync_job_resources")
    } == {"catalog", "investment-trust"}


def test_etf_profile_web_form_origin_and_api_scope(tmp_path):
    with TestClient(create_app(tmp_path)) as web:
        assert (
            web.post(
                "/securities/profiles/sync", headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            web.post(
                "/api/v2/sync-jobs",
                json={"operation": "investment-trust", "options": {"issuer": "野村"}},
            ).status_code
            == 422
        )
