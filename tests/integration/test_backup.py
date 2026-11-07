"""Integration tests for SQLite backup/restore (design 002 §4.13)."""

import io
import sqlite3
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


def _backup_dir_files(data_dir):
    directory = data_dir / "backups"
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def test_import_backup_lists_an_upload_under_a_new_name(data_dir):
    """Design 080 §4.2.2: any file name works; the original is kept as a note."""
    source = backup.create_backup(data_dir)
    content = source.read_bytes()
    name = "C:\\Users\\me\\Downloads\\" + source.stem + " (1).db"

    path = backup.import_backup(data_dir, io.BytesIO(content), name)

    assert path.name != source.name and path.name.startswith("taiwan_stock-")
    assert path.read_bytes() == content
    listed = {b["filename"]: b for b in backup.list_backups(data_dir)}
    assert listed[path.name]["kind_label"] == "上傳的備份"
    assert listed[path.name]["note"] == f"上傳：{source.stem} (1).db"
    assert not [n for n in _backup_dir_files(data_dir) if n.endswith(".part")]


def test_import_backup_shortens_and_defaults_the_original_name(data_dir):
    content = backup.create_backup(data_dir).read_bytes()

    long = backup.import_backup(data_dir, io.BytesIO(content), "x" * 300 + ".db")
    unnamed = backup.import_backup(data_dir, io.BytesIO(content), None)

    labels = {b["filename"]: b["note"] for b in backup.list_backups(data_dir)}
    assert labels[long.name] == "上傳：" + "x" * 100
    assert labels[unnamed.name] == "上傳：未命名檔案"


def _foreign_database(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE notes (body TEXT)")
    conn.commit()
    conn.close()
    return path.read_bytes()


def _newer_database(data_dir, path):
    path.write_bytes(backup.create_backup(data_dir).read_bytes())
    conn = sqlite3.connect(path)
    newest = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    columns = [r[1] for r in conn.execute("PRAGMA table_info(schema_migrations)")]
    values = {c: "x" for c in columns} | {"version": newest + 1}
    conn.execute(
        f"INSERT INTO schema_migrations ({', '.join(values)}) "
        f"VALUES ({', '.join('?' * len(values))})",
        list(values.values()),
    )
    conn.commit()
    conn.close()
    return path.read_bytes()


def _truncated_database(data_dir):
    content = backup.create_backup(data_dir).read_bytes()
    return content[: len(content) // 2]


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda d, t: b"", "不是 SQLite"),
        (lambda d, t: b"name,code\n2330,TSMC\n", "不是 SQLite"),
        (lambda d, t: _foreign_database(t / "other.db"), "不是本程式的備份"),
        (lambda d, t: _newer_database(d, t / "newer.db"), "還新"),
        (lambda d, t: _truncated_database(d), "完整性檢查失敗"),
    ],
    ids=["empty", "csv", "foreign", "newer", "truncated"],
)
def test_import_backup_rejects_bad_files_without_leaving_any(
    data_dir, tmp_path_factory, make, message
):
    content = make(data_dir, tmp_path_factory.mktemp("upload"))
    before = _backup_dir_files(data_dir)

    with pytest.raises(backup.BackupError, match=message):
        backup.import_backup(data_dir, io.BytesIO(content), "upload.db")

    assert _backup_dir_files(data_dir) == before


def test_import_backup_stops_at_the_size_limit(data_dir, monkeypatch):
    content = backup.create_backup(data_dir).read_bytes()
    before = _backup_dir_files(data_dir)
    monkeypatch.setattr(backup, "MAX_UPLOAD_BYTES", len(content) - 1)
    monkeypatch.setattr(backup, "_UPLOAD_CHUNK", 4096)

    with pytest.raises(backup.BackupError, match="上限"):
        backup.import_backup(data_dir, io.BytesIO(content), "big.db")

    assert _backup_dir_files(data_dir) == before


def test_uploaded_backups_are_not_pruned(data_dir):
    content = backup.create_backup(data_dir).read_bytes()
    uploads = [
        backup.import_backup(data_dir, io.BytesIO(content), f"{i}.db")
        for i in range(backup.AUTO_BACKUP_KEEP + 1)
    ]
    for _ in range(backup.AUTO_BACKUP_KEEP + 1):
        backup.create_backup(data_dir, backup.PRE_RESTORE)

    remaining = {b["filename"] for b in backup.list_backups(data_dir)}
    assert {p.name for p in uploads} <= remaining


def test_restoring_an_upload_still_checks_related_rows(data_dir):
    """Design 080: an upload passes through restore's own checks unchanged."""
    path = data_dir / "orphan.db"
    path.write_bytes(backup.create_backup(data_dir).read_bytes())
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO watchlist (security_id, updated_at) "
        "VALUES ('missing', '2026-11-07T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    uploaded = backup.import_backup(data_dir, io.BytesIO(path.read_bytes()), "o.db")

    with pytest.raises(backup.BackupError, match="關聯資料不完整"):
        backup.restore_backup(data_dir, uploaded.name)
