"""Tests for `/api/v1/backups` (create/list/restore)."""

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def test_create_and_list_backups(client):
    created = client.post("/api/v1/backups")
    assert created.status_code == 201
    filename = created.json()["filename"]

    listed = client.get("/api/v1/backups")
    assert listed.status_code == 200
    names = [b["filename"] for b in listed.json()["items"]]
    assert filename in names


def test_restore_round_trip_via_api(client):
    client.post("/api/v1/broker-accounts", json={"name": "測試"})
    created = client.post("/api/v1/backups")
    filename = created.json()["filename"]

    account_created_before = client.get("/api/v1/broker-accounts").json()["items"]
    assert len(account_created_before) == 1

    client.post("/api/v1/broker-accounts", json={"name": "第二個帳戶"})
    assert len(client.get("/api/v1/broker-accounts").json()["items"]) == 2

    restore_response = client.post(
        "/api/v1/backups/restore", json={"filename": filename}
    )
    assert restore_response.status_code == 200

    after = client.get("/api/v1/broker-accounts").json()["items"]
    assert len(after) == 1  # back to the state at backup time


def test_restore_unknown_backup_returns_422(client):
    response = client.post(
        "/api/v1/backups/restore", json={"filename": "taiwan_stock-nope.db"}
    )

    assert response.status_code == 422


def test_restore_rejects_path_traversal(client):
    response = client.post(
        "/api/v1/backups/restore", json={"filename": "../../../etc/passwd"}
    )

    assert response.status_code == 422


def test_database_size_and_vacuum_via_api(client):
    client.post("/api/v1/broker-accounts", json={"name": "測試"})

    size_response = client.get("/api/v1/database-size")
    assert size_response.status_code == 200
    assert size_response.json()["size_bytes"] > 0

    vacuum_response = client.post("/api/v1/vacuum")
    assert vacuum_response.status_code == 200
    body = vacuum_response.json()
    assert set(body) == {"size_before_bytes", "size_after_bytes", "reclaimed_bytes"}

    # Data survives VACUUM.
    accounts = client.get("/api/v1/broker-accounts").json()["items"]
    assert len(accounts) == 1


def test_vacuum_conflict_when_sync_running_and_restore_recovers_running_sync(tmp_path):
    from taiwan_stock.infrastructure.db import get_connection

    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO catalog_sync_runs (id, source, started_at, status) "
            "VALUES ('run-1', 'investment_trust', '2026-10-01T00:00:00Z', 'running')"
        )
        conn.execute(
            "INSERT INTO catalog_sync_units "
            "(run_id, unit_key, stage, attempted_at, status) "
            "VALUES ('run-1', '元大:0050', 'product', "
            "'2026-10-01T00:00:00Z', 'pending')"
        )
        conn.commit()
        conn.close()

        # Vacuum is rejected while a sync run is active.
        api_vacuum = client.post("/api/v1/vacuum")
        assert api_vacuum.status_code == 409
        assert "同步正在執行中" in api_vacuum.json()["detail"]

        form_vacuum = client.post("/settings/vacuum", follow_redirects=False)
        assert form_vacuum.status_code == 303
        assert "vacuum_error=" in form_vacuum.headers["location"]

        # Create an online backup while the sync run is still marked 'running'.
        filename = client.post("/api/v1/backups").json()["filename"]

        # Restoring that backup marks the interrupted run & units as 'interrupted'.
        restore_resp = client.post(
            "/api/v1/backups/restore", json={"filename": filename}
        )
        assert restore_resp.status_code == 200

        conn = get_connection(tmp_path)
        run_status = conn.execute(
            "SELECT status FROM catalog_sync_runs WHERE id = 'run-1'"
        ).fetchone()["status"]
        unit_status = conn.execute(
            "SELECT status FROM catalog_sync_units WHERE run_id = 'run-1'"
        ).fetchone()["status"]
        conn.close()
        assert run_status == "interrupted"
        assert unit_status == "interrupted"

        # Now vacuum succeeds because no sync run is stuck in 'running'.
        assert client.post("/api/v1/vacuum").status_code == 200
