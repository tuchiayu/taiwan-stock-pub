"""Integration tests for SQLite backup/restore (design 002 §4.13)."""

import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import backup, migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app


@pytest.fixture
def data_dir(tmp_path):
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-1', 'TWSE', '2330', 'TSMC', 'stock', 'active')"
    )
    conn.close()
    return tmp_path


def test_create_backup_captures_current_data(data_dir):
    backup_path = backup.create_backup(data_dir)

    assert backup_path.exists()
    import sqlite3

    check = sqlite3.connect(backup_path)
    row = check.execute("SELECT name FROM securities WHERE id = 'sec-1'").fetchone()
    check.close()
    assert row == ("TSMC",)


def test_list_backups_sorted_newest_first(data_dir):
    first = backup.create_backup(data_dir)
    second = backup.create_backup(data_dir)

    backups = backup.list_backups(data_dir)

    filenames = [b["filename"] for b in backups]
    if first.name != second.name:
        assert filenames == [second.name, first.name]
    else:
        # Same-second timestamps can collide; just confirm it's listed.
        assert first.name in filenames


def test_restore_replaces_live_data_and_verified_by_new_connection(data_dir):
    backup_path = backup.create_backup(data_dir)

    conn = get_connection(data_dir)
    conn.execute("DELETE FROM securities WHERE id = 'sec-1'")
    conn.close()

    check_conn = get_connection(data_dir)
    remaining = check_conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0]
    check_conn.close()
    assert remaining == 0

    backup.restore_backup(data_dir, backup_path.name)

    restored = get_connection(data_dir)
    row = restored.execute("SELECT name FROM securities WHERE id = 'sec-1'").fetchone()
    restored.close()
    assert row["name"] == "TSMC"


def test_restore_preserves_design_004_columns(data_dir):
    """Design 004 §4.5's closing paragraph: the new columns (raw
    custodian institution, distribution source/fetched_at/record_date)
    "沿用既有 SQLite 整庫備份／還原" — no special-cased backup format is
    needed because `create_backup`/`restore_backup` copy the whole
    file, schema-agnostic. This is a regression test proving that,
    not just an assumption.
    """
    conn = get_connection(data_dir)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '00878', '國泰永續高股息', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id, custodian_institution_raw) "
        "VALUES ('e1', '臺灣集中保管結算所股份有限公司')"
    )
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, record_date, pay_date, "
        " amount_per_share, amount_status, source, fetched_at) "
        "VALUES ('d1', 'e1', '2026-08-18', '2026-08-24', '2026-09-11', "
        " '1.01', 'final', 'twse_etf_distribution_list', "
        " '2026-09-12T00:00:00.000Z')"
    )
    conn.close()
    backup_path = backup.create_backup(data_dir)

    conn = get_connection(data_dir)
    conn.execute("DELETE FROM distributions WHERE id = 'd1'")
    conn.execute("DELETE FROM etf_profiles WHERE security_id = 'e1'")
    conn.close()

    backup.restore_backup(data_dir, backup_path.name)

    restored = get_connection(data_dir)
    profile = restored.execute(
        "SELECT custodian_institution_raw FROM etf_profiles WHERE security_id = 'e1'"
    ).fetchone()
    distribution = restored.execute(
        "SELECT record_date, source, fetched_at FROM distributions WHERE id = 'd1'"
    ).fetchone()
    restored.close()
    assert profile["custodian_institution_raw"] == "臺灣集中保管結算所股份有限公司"
    assert distribution["record_date"] == "2026-08-24"
    assert distribution["source"] == "twse_etf_distribution_list"
    assert distribution["fetched_at"] == "2026-09-12T00:00:00.000Z"


def test_restore_takes_a_safety_backup_of_current_state_first(data_dir):
    import sqlite3

    backup_path = backup.create_backup(data_dir)
    conn = get_connection(data_dir)
    conn.execute("DELETE FROM securities WHERE id = 'sec-1'")
    conn.close()

    backup.restore_backup(data_dir, backup_path.name)

    safety_name = next(
        item["filename"]
        for item in backup.list_backups(data_dir)
        if item["filename"] != backup_path.name
    )
    safety = sqlite3.connect(data_dir / "backups" / safety_name)
    try:
        assert safety.execute("SELECT COUNT(*) FROM securities").fetchone()[0] == 0
        assert safety.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        safety.close()


def test_restore_rejects_path_traversal_filename(data_dir):
    with pytest.raises(backup.BackupError, match="無效的檔名"):
        backup.restore_backup(data_dir, "../../etc/passwd")


def test_restore_rejects_unknown_filename(data_dir):
    with pytest.raises(backup.BackupError, match="找不到備份"):
        backup.restore_backup(data_dir, "taiwan_stock-20990101T000000Z.db")


def test_restore_rejects_a_corrupt_backup_file(data_dir):
    bad_path = data_dir / "backups" / "taiwan_stock-corrupt.db"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_bytes(b"not a real sqlite file")

    with pytest.raises(backup.BackupError, match="完整性檢查失敗"):
        backup.restore_backup(data_dir, bad_path.name)


def test_database_size_bytes_matches_the_file_on_disk(data_dir):
    from taiwan_stock.infrastructure.db import DB_FILENAME

    expected = (data_dir / DB_FILENAME).stat().st_size
    assert backup.database_size_bytes(data_dir) == expected


def test_database_size_bytes_is_zero_when_the_file_does_not_exist_yet(tmp_path):
    assert backup.database_size_bytes(tmp_path) == 0


def test_vacuum_reclaims_space_left_by_deleted_rows(data_dir):
    conn = get_connection(data_dir)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "SELECT 'sec-bulk-' || value, 'TWSE', value, 'x' || value, 'stock', 'active' "
        "FROM (WITH RECURSIVE seq(value) AS "
        "(SELECT 100000 UNION ALL SELECT value + 1 FROM seq WHERE value < 105000) "
        "SELECT value FROM seq)"
    )
    conn.execute("DELETE FROM securities WHERE id != 'sec-1'")
    conn.close()

    result = backup.vacuum(data_dir)

    assert result["size_before_bytes"] >= result["size_after_bytes"]
    assert result["reclaimed_bytes"] == (
        result["size_before_bytes"] - result["size_after_bytes"]
    )
    assert result["reclaimed_bytes"] > 0


def test_vacuum_preserves_data(data_dir):
    backup.vacuum(data_dir)

    conn = get_connection(data_dir)
    row = conn.execute("SELECT name FROM securities WHERE id = 'sec-1'").fetchone()
    conn.close()
    assert row["name"] == "TSMC"


def test_restore_rejects_active_cli_process_before_changing_live_data(data_dir):
    """A separate CLI connection blocks restore, including while it is idle."""
    saved = backup.create_backup(data_dir)
    conn = get_connection(data_dir)
    conn.execute("DELETE FROM securities WHERE id = 'sec-1'")
    conn.close()
    backups_before = len(backup.list_backups(data_dir))
    ready = data_dir / "cli-ready"
    child_code = """
import sys
from pathlib import Path
from taiwan_stock.infrastructure.db import get_connection
conn = get_connection(Path(sys.argv[1]))
Path(sys.argv[2]).write_text("ready")
sys.stdin.readline()
conn.close()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(data_dir), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not ready.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert ready.exists(), child.stderr.read() if child.poll() is not None else ""

        with TestClient(create_app(data_dir)) as client:
            response = client.post(
                "/api/v1/backups/restore", json={"filename": saved.name}
            )
        assert response.status_code == 422
        assert "活動中的連線" in response.json()["detail"]
        with pytest.raises(backup.BackupError, match="活動中的連線"):
            backup.restore_backup(data_dir, saved.name)
        assert len(backup.list_backups(data_dir)) == backups_before

        check = get_connection(data_dir)
        try:
            assert check.execute("SELECT COUNT(*) FROM securities").fetchone()[0] == 0
            assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            check.close()
    finally:
        if child.stdin:
            child.stdin.write("\n")
            child.stdin.flush()
        try:
            child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
        assert child.returncode == 0

    backup.restore_backup(data_dir, saved.name)
    restored = get_connection(data_dir)
    try:
        assert restored.execute("SELECT COUNT(*) FROM securities").fetchone()[0] == 1
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()
    assert len(backup.list_backups(data_dir)) == backups_before + 1


def test_backups_are_listed_with_kind_note_and_taipei_time(data_dir):
    manual = backup.create_backup(data_dir)
    automatic = backup.create_backup(data_dir, backup.PRE_IMPORT, "匯入 a.csv 前")
    legacy = backup.create_backup(data_dir)
    legacy.with_suffix(".json").unlink()

    listed = {b["filename"]: b for b in backup.list_backups(data_dir)}

    assert listed[manual.name]["kind_label"] == "手動備份"
    assert listed[automatic.name]["kind_label"] == "匯入前自動備份"
    assert listed[automatic.name]["note"] == "匯入 a.csv 前"
    assert listed[legacy.name]["kind_label"] == "未標示"
    assert len(listed[manual.name]["modified_at_taipei"]) == len("2026-10-23 10:00")


def test_only_the_newest_automatic_backups_of_each_kind_are_kept(data_dir):
    manuals = [backup.create_backup(data_dir) for _ in range(3)]
    restores = [
        backup.create_backup(data_dir, backup.PRE_RESTORE)
        for _ in range(backup.AUTO_BACKUP_KEEP)
    ]
    imports = [
        backup.create_backup(data_dir, backup.PRE_IMPORT)
        for _ in range(backup.AUTO_BACKUP_KEEP + 2)
    ]

    remaining = {b["filename"] for b in backup.list_backups(data_dir)}

    assert {p.name for p in manuals + restores} <= remaining
    assert {p.name for p in imports[2:]} <= remaining
    assert not {p.name for p in imports[:2]} & remaining
    assert not imports[0].with_suffix(".json").exists()


def test_restore_labels_its_safety_backup_and_keeps_the_restored_file(data_dir):
    oldest = backup.create_backup(data_dir, backup.PRE_RESTORE)
    for _ in range(backup.AUTO_BACKUP_KEEP):
        backup.create_backup(data_dir, backup.PRE_RESTORE)
    assert oldest.name not in {b["filename"] for b in backup.list_backups(data_dir)}

    target = backup.create_backup(data_dir, backup.PRE_RESTORE)
    for _ in range(backup.AUTO_BACKUP_KEEP - 1):
        backup.create_backup(data_dir, backup.PRE_RESTORE)
    backup.restore_backup(data_dir, target.name)

    listed = backup.list_backups(data_dir)
    assert listed[0]["kind"] == backup.PRE_RESTORE
    assert listed[0]["note"] == f"還原 {target.name} 前的資料"
    assert target.exists()


def test_delete_backup_removes_the_file_and_its_label(data_dir):
    path = backup.create_backup(data_dir)

    backup.delete_backup(data_dir, path.name)

    assert not path.exists() and not path.with_suffix(".json").exists()
    with pytest.raises(backup.BackupError, match="無效的檔名"):
        backup.delete_backup(data_dir, "../taiwan_stock.db")
    with pytest.raises(backup.BackupError, match="找不到備份"):
        backup.delete_backup(data_dir, path.name)


def test_settings_page_lists_and_deletes_backups(data_dir):
    path = backup.create_backup(data_dir, backup.PRE_IMPORT, "匯入 trades.csv 前")
    with TestClient(create_app(data_dir)) as client:
        page = client.get("/settings")
        assert "匯入前自動備份" in page.text and "匯入 trades.csv 前" in page.text

        response = client.post(
            "/settings/backups/delete",
            data={"filename": path.name},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "已刪除備份" in client.get(response.headers["location"]).text
        assert not path.exists()

        failed = client.post(
            "/settings/backups/delete",
            data={"filename": path.name},
            follow_redirects=False,
        )
        assert "settings_error" in failed.headers["location"]
