"""Design 065: unknown ids, security headers, per-request overhead."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import bank_dictionary
from taiwan_stock.infrastructure import db, migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app

HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
}


@pytest.fixture
def data_dir(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        conn.execute(
            "INSERT INTO securities(id,code,name,market,security_type,status) "
            "VALUES('TWSE:0050','0050','元大台灣50','TWSE','etf','active')"
        )
    return tmp_path


@pytest.fixture
def client(data_dir):
    with TestClient(
        create_app(data_dir), base_url="http://127.0.0.1", raise_server_exceptions=False
    ) as client:
        yield client


def count(data_dir, table):
    with closing(get_connection(data_dir)) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_unknown_ids_are_404_and_write_nothing(client, data_dir):
    api = client.post("/api/v1/watchlist/NOPE")
    assert api.status_code == 404
    assert api.json()["detail"] == "找不到商品 NOPE"

    form = client.post("/watchlist/add", data={"security_id": "NOPE"})
    assert form.status_code == 404
    assert "找不到商品 NOPE" in form.text
    assert count(data_dir, "watchlist") == 0

    bank = sorted(bank_dictionary.display_names())[0]
    for path, detail in (
        ("/securities/NOPE/broker-accounts/a/dividend-bank", "找不到商品"),
        ("/securities/TWSE:0050/broker-accounts/NOPE/dividend-bank", "找不到券商帳戶"),
    ):
        response = client.post(path, data={"dividend_bank": bank})
        assert response.status_code == 404, path
        assert response.json()["detail"] == detail
    assert count(data_dir, "dividend_bank_preferences") == 0


def test_known_ids_are_still_written(client, data_dir):
    assert client.post("/api/v1/watchlist/TWSE:0050").status_code == 200
    bank = sorted(bank_dictionary.display_names())[0]
    response = client.post(
        "/securities/TWSE:0050/broker-accounts/a/dividend-bank",
        data={"dividend_bank": bank},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert count(data_dir, "watchlist") == 1
    assert count(data_dir, "dividend_bank_preferences") == 1


def test_every_response_forbids_framing(client, data_dir):
    responses = [
        client.get("/"),
        client.get("/api/v1/watchlist"),
        client.get("/static/app.css"),
        client.post("/api/v1/watchlist/NOPE"),
    ]
    with closing(get_connection(data_dir)) as conn:
        conn.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=1")
    responses.append(client.get("/"))
    assert responses[-1].status_code == 503
    for response in responses:
        for name, value in HEADERS.items():
            assert response.headers[name] == value, (response.url, name)


def test_a_page_reuses_its_connection_for_the_template(client, data_dir, monkeypatch):
    with closing(get_connection(data_dir)) as conn:
        conn.execute(
            "INSERT INTO sync_jobs(id,operation,origin,request_key,parameters,"
            "status,submitted_at,owner_token,phase) VALUES('j','prices','web','k',"
            "'{}','running','2026-11-01T00:00:00.000Z','t','測試同步階段')"
        )
    opened = []
    original = db.get_connection
    monkeypatch.setattr(
        db, "get_connection", lambda data_dir: opened.append(1) or original(data_dir)
    )
    page = client.get("/")
    assert page.status_code == 200
    assert "測試同步階段" in page.text
    # The readiness check and the route's own connection, nothing more.
    assert len(opened) == 2


def test_checksums_follow_the_file_and_are_not_reread(tmp_path, monkeypatch):
    path = tmp_path / "0001_x.sql"
    path.write_bytes(b"SELECT 1;")
    migration = migrate.Migration(1, "0001_x", path)
    first = migration.checksum
    reads = []
    original = type(path).read_bytes
    monkeypatch.setattr(
        type(path), "read_bytes", lambda self: reads.append(self) or original(self)
    )
    assert migration.checksum == first
    assert reads == []
    path.write_bytes(b"SELECT 22;")
    assert migration.checksum != first
    assert reads == [path]
