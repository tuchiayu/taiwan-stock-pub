"""SQLite backup/restore (design 002 §4.13).

A separate SQLite coordination database prevents restores from overlapping
application connections. Backups and restores use SQLite's online backup API
so WAL contents are handled by SQLite.
"""

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import (
    DB_FILENAME,
    DatabaseInUseError,
    exclusive_database_access,
    get_runtime_connection,
)
from taiwan_stock.shared.dates import TAIPEI

# Microsecond precision, not whole seconds: two backups created less
# than a second apart (e.g. restore_backup's own pre-restore safety
# backup, taken moments after the backup a user just made) must never
# collide onto the same filename — a collision would silently
# overwrite one backup with another, and if the overwritten one is the
# very file a restore is reading from, corrupt the restore itself.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_PREFIX = "taiwan_stock-"

# Design 033: each backup gets a `<name>.json` sidecar saying why it was
# made. Automatic kinds keep only their newest AUTO_BACKUP_KEEP copies;
# manual backups and unlabeled ones from older versions are never pruned.
MANUAL, PRE_IMPORT, PRE_RESTORE = "manual", "pre_import", "pre_restore"
KIND_LABELS = {
    MANUAL: "手動備份",
    PRE_IMPORT: "匯入前自動備份",
    PRE_RESTORE: "還原前自動備份",
}
AUTO_BACKUP_KEEP = 10


class BackupError(Exception):
    pass


def _backups_dir(data_dir: Path) -> Path:
    path = data_dir / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unique_backup_path(data_dir: Path) -> Path:
    """Microsecond precision makes a collision astronomically unlikely
    for a human clicking a button, but "unlikely" isn't "impossible" —
    a colliding filename would silently overwrite an existing backup,
    so fall back to an explicit counter suffix rather than trust the
    clock alone.
    """
    directory = _backups_dir(data_dir)
    timestamp = datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)
    candidate = directory / f"{_PREFIX}{timestamp}.db"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{_PREFIX}{timestamp}-{counter}.db"
        counter += 1
    return candidate


def _backup_to_path(source: sqlite3.Connection, path: Path) -> None:
    dest = sqlite3.connect(path)
    try:
        source.backup(dest)
    except BaseException:
        dest.close()
        path.unlink(missing_ok=True)
        raise
    else:
        dest.close()


def create_backup(data_dir: Path, kind: str = MANUAL, note: str | None = None) -> Path:
    """Create a consistent hot backup using SQLite's online backup API."""
    backup_path = _unique_backup_path(data_dir)
    source = get_runtime_connection(data_dir)
    try:
        _backup_to_path(source, backup_path)
    finally:
        source.close()
    _write_label(backup_path, kind, note)
    _prune(data_dir, kind)
    return backup_path


def list_backups(data_dir: Path) -> list[dict]:
    backups = []
    for path in sorted(_backups_dir(data_dir).glob(f"{_PREFIX}*.db"), reverse=True):
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        label = _read_label(path)
        backups.append(
            {
                "filename": path.name,
                "size_bytes": stat.st_size,
                "modified_at": modified.isoformat(),
                # Design 051 §4.2.3: the site-wide Taipei minute format.
                "modified_at_taipei": modified.astimezone(TAIPEI).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "kind": label.get("kind"),
                "kind_label": KIND_LABELS.get(label.get("kind"), "未標示"),
                "note": label.get("note"),
            }
        )
    return backups


def delete_backup(data_dir: Path, filename: str) -> None:
    path = _resolve_backup_path(data_dir, filename)
    try:
        path.unlink()
    except OSError as exc:
        raise BackupError(f"無法刪除備份「{filename}」，請稍後再試：{exc}") from exc
    _label_path(path).unlink(missing_ok=True)


def _label_path(backup_path: Path) -> Path:
    return backup_path.with_suffix(".json")


def _write_label(backup_path: Path, kind: str, note: str | None) -> None:
    _label_path(backup_path).write_text(
        json.dumps({"kind": kind, "note": note}, ensure_ascii=False), encoding="utf-8"
    )


def _read_label(backup_path: Path) -> dict:
    try:
        label = json.loads(_label_path(backup_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return label if isinstance(label, dict) else {}


def _prune(data_dir: Path, kind: str, keep: frozenset[str] = frozenset()) -> None:
    """Drops automatic backups of `kind` beyond the newest few; `keep`
    protects a file still in use (the backup a restore just read)."""
    if kind == MANUAL:
        return
    paths = sorted(_backups_dir(data_dir).glob(f"{_PREFIX}*.db"), reverse=True)
    same_kind = [p for p in paths if _read_label(p).get("kind") == kind]
    for path in same_kind[AUTO_BACKUP_KEEP:]:
        if path.name not in keep:
            path.unlink(missing_ok=True)
            _label_path(path).unlink(missing_ok=True)


def _resolve_backup_path(data_dir: Path, filename: str) -> Path:
    if "/" in filename or "\\" in filename or ".." in filename:
        raise BackupError("無效的檔名")
    if not (filename.startswith(_PREFIX) and filename.endswith(".db")):
        raise BackupError("無效的檔名")

    path = _backups_dir(data_dir) / filename
    if not path.is_file():
        raise BackupError(f"找不到備份「{filename}」")
    return path


def database_size_bytes(data_dir: Path) -> int:
    path = data_dir / DB_FILENAME
    return path.stat().st_size if path.is_file() else 0


def vacuum(data_dir: Path) -> dict:
    """Reclaims space SQLite doesn't free on its own after deletes —
    e.g. untracking a security clears its `daily_prices`/
    `distributions`/`corporate_actions` rows (§4.7), but the `.db` file
    itself doesn't shrink until VACUUM rewrites it (§4.13).

    VACUUM needs roughly as much free disk space as the database's
    current size and holds an exclusive lock on it for the duration
    (longer for a bigger file); rejects execution when background sync
    jobs or legacy sync runs are active, and converts lock contention
    into `BackupError`.
    """
    size_before = database_size_bytes(data_dir)
    conn = get_runtime_connection(data_dir)
    try:
        state = sync_jobs.snapshot(conn, data_dir)
        if state["jobs"] or state["legacy_sources"]:
            raise BackupError("背景同步正在執行中，請待同步完成後再壓縮資料庫")
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            raise BackupError(f"資料庫正被其他作業鎖定，無法壓縮：{exc}") from exc
    finally:
        conn.close()
    size_after = database_size_bytes(data_dir)
    return {
        "size_before_bytes": size_before,
        "size_after_bytes": size_after,
        "reclaimed_bytes": size_before - size_after,
    }


_REQUIRED_TABLES = frozenset(
    {"schema_migrations", "trades", "securities", "broker_accounts"}
)


def _check_backup_schema(conn: sqlite3.Connection) -> None:
    """Only this app's databases, never newer than this program (#115)."""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if missing := _REQUIRED_TABLES - tables:
        raise BackupError(
            f"這不是本程式的備份（缺少資料表：{'、'.join(sorted(missing))}）"
        )
    versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    try:
        migrate.check_not_newer(versions, label="備份")
    except RuntimeError as exc:
        raise BackupError(str(exc)) from exc


def _foreign_key_problems(conn: sqlite3.Connection) -> str | None:
    """`integrity_check` doesn't look at foreign keys (design 064 §4.2.2)."""
    counts = {}
    for row in conn.execute("PRAGMA foreign_key_check"):
        counts[row[0], row[2]] = counts.get((row[0], row[2]), 0) + 1
    return (
        "；".join(
            f"{table} 有 {count} 筆找不到對應的 {parent}"
            for (table, parent), count in sorted(counts.items())
        )
        or None
    )


def _prepare_restore(backup_path: Path, prepared_path: Path) -> None:
    """Upgrade and check a disposable copy; the original backup stays as-is.

    Otherwise restoring a pre-jobs backup leaves the running Web without the
    job tables until restart. Nothing here touches the live DB.
    """
    source = sqlite3.connect(backup_path, isolation_level=None)
    prepared = sqlite3.connect(prepared_path, isolation_level=None)
    prepared.row_factory = sqlite3.Row
    try:
        source.backup(prepared)
        migrate.apply_pending(prepared)
        migrate.validate_runtime_schema(prepared)
        problems = _foreign_key_problems(prepared)
        if not problems:
            sync_jobs.recover_restored(prepared)
    except (sqlite3.DatabaseError, RuntimeError) as exc:
        raise BackupError(f"還原失敗：{exc}；目前資料未變更") from exc
    finally:
        prepared.close()
        source.close()
    if problems:
        raise BackupError(f"還原失敗：備份的關聯資料不完整：{problems}；目前資料未變更")


def _live_schema_ready(live: sqlite3.Connection) -> bool:
    """False for a live DB this program can't run but can still replace.

    Only a newer schema is refused (design 064 §4.2.3): updating the program
    fixes that, while restoring would overwrite newer data with older.
    """
    try:
        migrate.validate_runtime_schema(live)
        return True
    except migrate.DatabaseNotReadyError as exc:
        try:
            versions = {
                row[0] for row in live.execute("SELECT version FROM schema_migrations")
            }
        except sqlite3.DatabaseError:
            versions = set()
        try:
            migrate.check_not_newer(versions)
        except RuntimeError:
            raise exc from None
        return False


def _sync_running(db_path: Path) -> bool:
    """Read only, without a lease: called after the exclusive lease failed."""
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return False
    try:
        return bool(
            conn.execute(
                "SELECT EXISTS(SELECT 1 FROM sync_jobs WHERE status IN "
                "('queued','running')) OR EXISTS(SELECT 1 FROM catalog_sync_runs "
                "WHERE status='running' AND job_id IS NULL)"
            ).fetchone()[0]
        )
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def restore_backup(data_dir: Path, filename: str) -> str:
    """Restore through SQLite after all application connections have closed.

    The shared/exclusive lease is held in a separate rollback-journal DB, so
    web and CLI connections can run together but cannot overlap a restore.
    SQLite's backup API writes the live DB transactionally and manages WAL
    itself; raw file replacement would leave old WAL pages beside new data.
    Returns the pre-restore safety backup's filename.
    """
    backup_path = _resolve_backup_path(data_dir, filename)

    check_conn = sqlite3.connect(backup_path)
    try:
        try:
            result = check_conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"備份完整性檢查失敗：{exc}") from exc
        if result[0] != "ok":
            raise BackupError(f"備份完整性檢查失敗：{result[0]}")
        _check_backup_schema(check_conn)
    finally:
        check_conn.close()

    live_db_path = data_dir / DB_FILENAME
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=data_dir) as temp:
        prepared_path = Path(temp) / "prepared.db"
        _prepare_restore(backup_path, prepared_path)
        try:
            with exclusive_database_access(data_dir):
                if not live_db_path.is_file():
                    raise BackupError("找不到目前的資料庫")

                # This is the exact pre-restore state: no application
                # connection can write between this snapshot and the restore.
                safety_path = _unique_backup_path(data_dir)
                live = sqlite3.connect(live_db_path, isolation_level=None)
                try:
                    ready = _live_schema_ready(live)
                    try:
                        _backup_to_path(live, safety_path)
                    except sqlite3.DatabaseError as exc:
                        raise BackupError(
                            f"目前的資料庫檔案無法讀取，無法先保存安全備份：{exc}"
                        ) from exc
                finally:
                    live.close()
                note = f"還原 {filename} 前的資料"
                _write_label(
                    safety_path,
                    PRE_RESTORE,
                    note if ready else f"{note}（原資料庫結構未就緒）",
                )

                prepared = sqlite3.connect(prepared_path, isolation_level=None)
                target = sqlite3.connect(live_db_path, isolation_level=None)
                try:
                    prepared.backup(target)
                except sqlite3.DatabaseError as exc:
                    raise BackupError(
                        f"還原失敗；原資料已保存在 {safety_path.name}：{exc}"
                    ) from exc
                finally:
                    target.close()
                    prepared.close()

                verify = sqlite3.connect(live_db_path)
                try:
                    migrate.validate_runtime_schema(verify)
                    result = verify.execute("PRAGMA integrity_check").fetchone()
                    if result[0] != "ok":
                        raise BackupError(
                            f"還原後資料庫完整性檢查失敗：{result[0]}；"
                            f"原資料已保存在 {safety_path.name}"
                        )
                finally:
                    verify.close()
        except DatabaseInUseError as exc:
            if _sync_running(live_db_path):
                raise BackupError("背景同步正在執行中，請待同步完成後再還原") from exc
            raise BackupError(str(exc)) from exc
    _prune(data_dir, PRE_RESTORE, keep=frozenset({filename}))
    return safety_path.name
