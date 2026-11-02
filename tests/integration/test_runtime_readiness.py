"""A failed startup or later incompatible upgrade must not permit business I/O."""

import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from taiwan_stock import cli
from taiwan_stock.infrastructure import backup, migrate, sync_jobs
from taiwan_stock.infrastructure.db import (
    exclusive_database_access,
    get_connection,
    get_runtime_connection,
)
from taiwan_stock.main import create_app


def poison(conn, kind):
    if kind == "newer":
        conn.execute("INSERT INTO schema_migrations(version,checksum) VALUES(9999,'x')")
    elif kind == "missing":
        conn.execute("DELETE FROM schema_migrations WHERE version=1")
    else:
        conn.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=1")


@pytest.mark.parametrize("kind", ["newer", "missing", "checksum"])
def test_runtime_check_is_readonly_and_releases_lease(tmp_path, kind):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        poison(conn, kind)
        before = conn.total_changes
        with pytest.raises(migrate.DatabaseNotReadyError):
            migrate.validate_runtime_schema(conn)
        assert conn.total_changes == before
    with pytest.raises(migrate.DatabaseNotReadyError):
        get_runtime_connection(tmp_path)
    with exclusive_database_access(tmp_path):
        pass


def test_validator_does_not_create_schema_table():
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(migrate.DatabaseNotReadyError):
            migrate.validate_runtime_schema(conn)
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []


@pytest.mark.parametrize("after_start", [False, True])
def test_all_business_routes_fail_closed(tmp_path, after_start):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        if not after_start:
            poison(conn, "newer")
    with TestClient(create_app(tmp_path)) as client:
        if after_start:
            with closing(get_connection(tmp_path)) as conn:
                poison(conn, "newer")
        for method, path in [
            ("GET", "/"),
            ("GET", "/settings"),
            ("GET", "/api/v2/sync-jobs"),
            ("POST", "/api/v1/broker-accounts"),
            ("POST", "/api/v1/backups"),
            ("POST", "/api/v1/backups/restore"),
            ("POST", "/api/v1/maintenance/vacuum"),
        ]:
            response = client.request(method, path, json={"name": "blocked"})
            assert response.status_code == 503, (path, response.text)
        assert client.get("/health").status_code == 503
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/", headers={"Host": "evil.invalid"}).status_code == 400
        assert (
            client.post("/", headers={"Origin": "https://evil.invalid"}).status_code
            == 403
        )
    with closing(get_connection(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM broker_accounts").fetchone()[0] == 0


def test_worker_and_maintenance_refuse_before_writing(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
    saved = backup.create_backup(tmp_path)
    with closing(get_connection(tmp_path)) as conn:
        poison(conn, "newer")
        before = list(conn.iterdump())
    for work in [
        lambda: sync_jobs.run_worker(tmp_path, "missing-job"),
        lambda: backup.create_backup(tmp_path),
        lambda: backup.vacuum(tmp_path),
        lambda: backup.restore_backup(tmp_path, saved.name),
    ]:
        with pytest.raises(migrate.DatabaseNotReadyError):
            work()
    with closing(get_connection(tmp_path)) as conn:
        assert list(conn.iterdump()) == before


def test_cli_initialization_failure_closes_connection(tmp_path, capsys):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        poison(conn, "newer")
    with pytest.raises(RuntimeError):
        cli._open_conn(tmp_path)
    with exclusive_database_access(tmp_path):
        pass
