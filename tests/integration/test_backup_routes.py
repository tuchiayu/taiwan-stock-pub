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


def test_settings_page_downloads_a_listed_backup(client, tmp_path):
    """Design 080 §4.2.1."""
    filename = client.post("/api/v1/backups").json()["filename"]

    page = client.get("/settings")
    assert f"/settings/backups/download?filename={filename}" in page.text
    response = client.get("/settings/backups/download", params={"filename": filename})

    assert response.status_code == 200
    assert response.content == (tmp_path / "backups" / filename).read_bytes()
    assert response.headers["content-type"] == "application/vnd.sqlite3"
    assert "attachment" in response.headers["content-disposition"]
    assert filename in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "filename", ["taiwan_stock-nope.db", "../taiwan_stock.db", "taiwan_stock.db"]
)
def test_download_refuses_unlisted_files(client, filename):
    response = client.get(
        "/settings/backups/download",
        params={"filename": filename},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "settings_error=" in response.headers["location"]


def _upload(client, content, name="taiwan_stock-old (1).db", **kwargs):
    return client.post(
        "/settings/backups/upload",
        files={"file": (name, content, "application/octet-stream")},
        follow_redirects=False,
        **kwargs,
    )


def test_backup_moves_to_a_new_computer_by_download_and_upload(tmp_path):
    """Design 080: download on the old data folder, upload and restore on a
    fresh one, then the restore notice summarizes what came back."""
    old, new = tmp_path / "old", tmp_path / "new"
    with TestClient(create_app(old)) as client:
        client.post("/api/v1/broker-accounts", json={"name": "舊電腦帳戶"})
        filename = client.post("/api/v1/backups").json()["filename"]
        content = client.get(
            "/settings/backups/download", params={"filename": filename}
        ).content

    with TestClient(create_app(new)) as client:
        assert client.get("/api/v1/broker-accounts").json()["items"] == []
        uploaded = _upload(client, content)
        assert uploaded.status_code == 303
        location = uploaded.headers["location"]
        assert "uploaded=" in location
        page = client.get(location).text
        assert "已上傳備份" in page and "上傳的備份" in page
        assert "上傳：taiwan_stock-old (1).db" in page
        new_name = [
            b["filename"]
            for b in client.get("/api/v1/backups").json()["items"]
            if b["kind"] == "uploaded"
        ][0]
        # Uploading only lists the file; the data is unchanged until restore.
        assert client.get("/api/v1/broker-accounts").json()["items"] == []

        restored = client.post(
            "/settings/backups/restore",
            data={"filename": new_name},
            follow_redirects=False,
        )
        assert "restored=" in restored.headers["location"]
        notice = client.get(restored.headers["location"]).text
        assert "目前資料：交易 0 筆、券商帳戶 1 個、自選 0 檔" in notice
        assert "公司行動確認 0 筆" in notice
        accounts = client.get("/api/v1/broker-accounts").json()["items"]
        assert [a["name"] for a in accounts] == ["舊電腦帳戶"]


def test_upload_reports_a_bad_file_and_keeps_nothing(client, tmp_path):
    before = sorted((tmp_path / "backups").glob("*"))

    response = _upload(client, b"date,code\n2026-11-07,0050\n", "trades.csv")

    assert response.status_code == 303
    assert "settings_error=" in response.headers["location"]
    page = client.get(response.headers["location"]).text
    assert "上傳失敗：這不是 SQLite 資料庫檔案" in page
    after = sorted((tmp_path / "backups").glob("*"))
    assert after == before


def test_upload_rejects_a_cross_origin_post(client):
    content = b"SQLite format 3\x00"

    response = _upload(client, content, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403


def test_settings_page_shows_the_data_folder_and_upload_form(client, tmp_path):
    page = client.get("/settings").text

    assert str(tmp_path) in page
    assert 'action="/settings/backups/upload"' in page
    assert 'enctype="multipart/form-data"' in page
    assert "換電腦：在舊電腦下載備份" in page


def test_transactions_page_says_csv_only_moves_trades(client):
    page = client.get("/transactions").text

    assert "CSV 只包含交易；換電腦請用設定頁的備份。" in page
