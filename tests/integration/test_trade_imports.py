"""Preview storage between the CSV import's preview and confirm (design 033)."""

import os
import time

import pytest

from taiwan_stock.infrastructure import backup, migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import trade_imports

CONTENT = (
    "broker_account_name,security_code,market,event_type,trade_date,quantity,"
    "unit_price\n測試券商,2330,TWSE,buy,2024-01-01,1000,50\n"
)


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    connection.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2330', 'TWSE', '2330', 'TSMC', 'stock', 'active')"
    )
    connection.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acct-1', '測試券商', 'active')"
    )
    yield connection
    connection.close()


def _expire(path):
    stamp = time.time() - trade_imports.PREVIEW_TTL_SECONDS - 60
    os.utime(path, (stamp, stamp))


def test_expired_previews_are_refused_and_purged(conn, tmp_path):
    old_id, plan = trade_imports.create_preview(conn, tmp_path, CONTENT)
    old_path = tmp_path / "imports" / f"{old_id}.csv"
    _expire(old_path)

    with pytest.raises(trade_imports.PreviewNotFoundError):
        trade_imports.confirm_preview(conn, tmp_path, old_id, plan.digest, ())
    assert not old_path.exists()

    stale_id, _ = trade_imports.create_preview(conn, tmp_path, CONTENT)
    _expire(tmp_path / "imports" / f"{stale_id}.csv")
    fresh_id, _ = trade_imports.create_preview(conn, tmp_path, CONTENT)

    assert [p.stem for p in (tmp_path / "imports").glob("*.csv")] == [fresh_id]
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_backup_failure_writes_nothing_and_keeps_the_preview(
    conn, tmp_path, monkeypatch
):
    preview_id, plan = trade_imports.create_preview(conn, tmp_path, CONTENT)

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(trade_imports.backup, "create_backup", fail)
    with pytest.raises(OSError, match="disk full"):
        trade_imports.confirm_preview(conn, tmp_path, preview_id, plan.digest, ())

    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert (tmp_path / "imports" / f"{preview_id}.csv").exists()


def test_confirm_labels_the_backup_with_the_upload(conn, tmp_path):
    preview_id, plan = trade_imports.create_preview(
        conn, tmp_path, CONTENT, "trades.csv"
    )

    _, backup_name = trade_imports.confirm_preview(
        conn, tmp_path, preview_id, plan.digest, ()
    )

    listed = {b["filename"]: b for b in backup.list_backups(tmp_path)}
    assert listed[backup_name]["kind"] == backup.PRE_IMPORT
    assert listed[backup_name]["note"] == "匯入 trades.csv 前（將新增 1、修改 0）"
    assert not list((tmp_path / "imports").iterdir())
