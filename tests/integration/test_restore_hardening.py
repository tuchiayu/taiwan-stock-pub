"""Design 064: foreign keys, restoring a DB that isn't ready, sync conflicts."""

import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from taiwan_stock import cli
from taiwan_stock.infrastructure import backup, migrate, sync_jobs
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trades


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
        trades.create_trade(
            conn,
            {
                "broker_account_id": "a",
                "security_id": "TWSE:0050",
                "event_type": "buy",
                "trade_date": "2026-10-30",
                "quantity": 100,
                "unit_price": "100",
            },
        )
    return tmp_path


def trade_count(data_dir):
    with closing(get_connection(data_dir)) as conn:
        return conn.execute("SELECT count(*) FROM trades").fetchone()[0]


def pre_restore_backups(data_dir):
    return [b for b in backup.list_backups(data_dir) if b["kind"] == backup.PRE_RESTORE]


def test_a_backup_with_orphan_trades_is_not_restored(data_dir):
    saved = backup.create_backup(data_dir)
    with closing(sqlite3.connect(saved)) as raw:
        raw.execute("DELETE FROM broker_accounts")
        raw.commit()
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with closing(get_connection(data_dir)) as conn:
        conn.execute("UPDATE trades SET note='live'")

    with pytest.raises(backup.BackupError) as raised:
        backup.restore_backup(data_dir, saved.name)
    message = str(raised.value)
    assert "trades 有 1 筆找不到對應的 broker_accounts" in message
    assert "目前資料未變更" in message
    assert pre_restore_backups(data_dir) == []
    with closing(get_connection(data_dir)) as conn:
        assert conn.execute("SELECT note FROM trades").fetchone()[0] == "live"


def poison(data_dir, kind):
    with closing(get_connection(data_dir)) as conn:
        if kind == "missing":
            conn.execute("DELETE FROM schema_migrations WHERE version=1")
        elif kind == "checksum":
            conn.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=1")
        else:
            conn.execute("DROP TABLE schema_migrations")


@pytest.mark.parametrize("kind", ["missing", "checksum", "unreadable"])
def test_a_live_db_that_isnt_ready_can_still_be_restored(data_dir, kind):
    saved = backup.create_backup(data_dir)
    poison(data_dir, kind)

    safety = backup.restore_backup(data_dir, saved.name)

    with closing(get_connection(data_dir)) as conn:
        migrate.validate_runtime_schema(conn)
    assert trade_count(data_dir) == 1
    [label] = pre_restore_backups(data_dir)
    assert label["filename"] == safety
    assert label["note"] == f"還原 {saved.name} 前的資料（原資料庫結構未就緒）"


def test_the_cli_restores_while_the_web_is_down(data_dir, capsys):
    saved = backup.create_backup(data_dir)
    with TestClient(create_app(data_dir)) as client:
        poison(data_dir, "checksum")
        down = client.get("/")
    assert down.status_code == 503
    assert "taiwan-stock backup restore" in down.json()["detail"]

    assert cli.main(["backup", "list", "--data-dir", str(data_dir)]) == 0
    assert saved.name in capsys.readouterr().out

    assert cli.main(["backup", "restore", saved.name, "--data-dir", str(data_dir)]) == 0
    out = capsys.readouterr().out
    assert f"已還原 {saved.name}" in out
    assert "請重新啟動" in out
    with TestClient(create_app(data_dir)) as client:
        assert client.get("/").status_code == 200

    assert cli.main(["backup", "restore", "nope.db", "--data-dir", str(data_dir)]) == 1
    assert "錯誤：" in capsys.readouterr().err


def test_a_restore_during_a_sync_says_so(data_dir):
    saved = backup.create_backup(data_dir)
    with (
        closing(get_connection(data_dir)) as worker,
        sync_jobs.job_lease(data_dir, "j"),
    ):
        worker.execute(
            "INSERT INTO sync_jobs(id,operation,origin,request_key,parameters,"
            "status,submitted_at,owner_token) VALUES('j','prices','web','k','{}',"
            "'running','2026-11-01T00:00:00.000Z','t')"
        )
        with pytest.raises(backup.BackupError, match="背景同步正在執行中"):
            backup.restore_backup(data_dir, saved.name)
        with TestClient(create_app(data_dir)) as client:
            response = client.post(
                "/api/v1/backups/restore", json={"filename": saved.name}
            )
        assert response.status_code == 422
        assert response.json()["detail"] == "背景同步正在執行中，請待同步完成後再還原"
    assert pre_restore_backups(data_dir) == []
