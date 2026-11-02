"""Schema migration runner.

Design 002 §4.13 / DEVELOPMENT.md §2.2.4: migrations are plain numbered
`.sql` files applied inside one transaction each, tracked in
`schema_migrations` so re-running is a no-op. Migrations are bundled as
package data (see `pyproject.toml`) so they install with the wheel.
"""

import functools
import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d+)_.*\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def checksum(self) -> str:
        stat = self.path.stat()
        return _checksum(self.path, stat.st_mtime_ns, stat.st_size)


@functools.lru_cache(maxsize=256)
def _checksum(path: Path, mtime_ns: int, size: int) -> str:
    """Every request validates the schema (design 065 §4.2.4); keyed by file
    state so a rewritten migration is still noticed."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_migrations() -> list[Migration]:
    migrations = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = _FILENAME_RE.match(path.name)
        if not match:
            continue
        migrations.append(
            Migration(version=int(match.group(1)), name=path.stem, path=path)
        )
    return sorted(migrations, key=lambda m: m.version)


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )


def check_not_newer(applied: set[int] | dict, label: str = "資料庫") -> None:
    """An older program must not run against a newer schema (issue #115 #1)."""
    known = max((m.version for m in discover_migrations()), default=0)
    newest = max(applied, default=0)
    if newest > known:
        raise RuntimeError(
            f"{label}的結構版本為 {newest}，比目前程式支援的 {known} 還新，"
            "請更新程式後再開啟"
        )


def get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_schema_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


class DatabaseNotReadyError(RuntimeError):
    """Runtime access requires the complete, compatible published schema."""


def validate_runtime_schema(conn: sqlite3.Connection) -> None:
    """Read only: never initialize, migrate, recover, or repair a runtime DB."""
    try:
        applied = dict(conn.execute("SELECT version, checksum FROM schema_migrations"))
    except sqlite3.DatabaseError as exc:
        raise DatabaseNotReadyError("資料庫尚未完成初始化或結構無法讀取") from exc
    expected = {m.version: m.checksum for m in discover_migrations()}
    if applied != expected:
        raise DatabaseNotReadyError(
            "資料庫結構版本不完整、不相容或 migration checksum 不符；"
            "請停止其他程序，更新至相容版本後重新啟動，"
            "或執行 taiwan-stock backup restore <備份檔名> 還原備份"
        )


@contextmanager
def _migration_lock(conn: sqlite3.Connection):
    """A separate connection retains the lock across executescript commits."""
    filename = next(
        (row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), ""
    )
    if not filename:  # An in-memory database belongs to this connection only.
        yield
        return
    lock_path = Path(str(Path(filename).resolve()) + ".migration_lock.db")
    lock = sqlite3.connect(lock_path, timeout=30, isolation_level=None)
    try:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"無法取得 migration 鎖（等待上限 30 秒）：{lock_path}"
            ) from exc
        yield
    finally:
        lock.close()  # SQLite/OS also releases this lock after process death.


def apply_pending(conn: sqlite3.Connection) -> list[int]:
    """Serialize upgrades before reading versions; DDL retains per-file atomicity."""
    with _migration_lock(conn):
        return _apply_pending_locked(conn)


def _apply_pending_locked(conn: sqlite3.Connection) -> list[int]:
    """Apply every not-yet-applied migration in order, one transaction each.

    Returns the versions that were newly applied. Already-applied
    migrations whose file content still matches the recorded checksum
    are skipped silently; a changed checksum on an applied migration
    raises, since published migrations must never be edited in place
    (DEVELOPMENT.md §2.2.4 不可變性).
    """
    _ensure_schema_migrations_table(conn)
    applied = {
        row["version"]: row["checksum"]
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }
    check_not_newer(applied)

    newly_applied = []
    for migration in discover_migrations():
        if migration.version in applied:
            if applied[migration.version] != migration.checksum:
                raise RuntimeError(
                    f"migration {migration.version} ({migration.name}) has already "
                    "been applied but its file content changed since — published "
                    "migrations must not be edited; add a new migration instead"
                )
            continue

        sql = migration.path.read_text(encoding="utf-8")
        try:
            # executescript() commits any pending transaction before it
            # runs, so the transaction boundary has to be literal SQL
            # text inside the script itself rather than a prior
            # conn.execute("BEGIN") — otherwise executescript would
            # silently commit that BEGIN before applying the DDL.
            conn.executescript("BEGIN;\n" + sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (?, ?)",
                (migration.version, migration.checksum),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        newly_applied.append(migration.version)

    return newly_applied
