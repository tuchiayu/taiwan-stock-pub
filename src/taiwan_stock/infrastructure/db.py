"""SQLite connections, data-directory resolution, and restore coordination.

Normal connections hold a shared lease in a separate rollback-journal SQLite
file for their entire lifetime. A restore takes an exclusive lease before
changing the live database. This works across the web and CLI processes while
still allowing their ordinary connections to run concurrently.
"""

import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from taiwan_stock.shared.dates import business_date_scope

DB_FILENAME = "taiwan_stock.db"
ENV_VAR = "TSL_DATA_DIR"
_COORDINATION_FILENAME = ".connection_lease.db"


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """One short WAL snapshot; a nested reader never completes its caller."""
    with business_date_scope():
        if conn.in_transaction:
            yield
            return
        conn.execute("BEGIN DEFERRED")
        try:
            yield
        finally:
            conn.rollback()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Reserve the writer before reading; nested operations never commit a caller.

    Batch callers own the outer IMMEDIATE transaction. A savepoint makes a
    nested failure atomic even when the caller catches the exception.
    """
    nested = conn.in_transaction
    savepoint = "write_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        conn.execute(f"RELEASE {savepoint}" if nested else "COMMIT")
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        else:
            conn.rollback()
        raise


class DatabaseInUseError(RuntimeError):
    """Another application connection is open during a restore attempt."""


class ManagedConnection(sqlite3.Connection):
    """Release the coordination lease only after the SQLite file is closed."""

    _coordination_conn: sqlite3.Connection | None = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            lease = self._coordination_conn
            self._coordination_conn = None
            if lease is not None:
                lease.close()


def resolve_data_dir(cli_data_dir: str | Path | None = None) -> Path:
    if cli_data_dir is not None:
        return Path(cli_data_dir).expanduser().resolve()

    env_value = os.environ.get(ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()

    return _default_data_dir()


def _default_data_dir() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
        return base / "TaiwanStock" / "data"

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "taiwan-stock"


def _coordination_lease(
    data_dir: Path, *, exclusive: bool, timeout: float
) -> sqlite3.Connection:
    """Hold a rollback-journal read or exclusive transaction across processes.

    Merely executing BEGIN is deferred in SQLite. The schema SELECT forces a
    shared lock for normal connections, even when the coordination DB is empty.
    This file must stay in SQLite's default rollback journal mode: WAL readers
    would not block BEGIN EXCLUSIVE.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lease = sqlite3.connect(
        data_dir / _COORDINATION_FILENAME,
        isolation_level=None,
        timeout=timeout,
        check_same_thread=False,
    )
    try:
        lease.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN")
        if not exclusive:
            lease.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError as exc:
        lease.close()
        if exclusive and "locked" in str(exc).lower():
            raise DatabaseInUseError("資料庫仍有活動中的連線，請稍後再還原") from exc
        raise
    except BaseException:
        lease.close()
        raise
    return lease


@contextmanager
def exclusive_database_access(data_dir: Path) -> Iterator[None]:
    """Fail before changing the live DB if any web or CLI connection is open."""
    lease = _coordination_lease(data_dir, exclusive=True, timeout=0)
    try:
        yield
    finally:
        lease.close()


def get_connection(data_dir: Path) -> sqlite3.Connection:
    # FastAPI may hand one request's dependency between AnyIO threads. Both
    # connections allow that serial handoff; workers still open their own
    # connections, never concurrently share a request connection.
    lease = _coordination_lease(data_dir, exclusive=False, timeout=30)
    try:
        conn = sqlite3.connect(
            data_dir / DB_FILENAME,
            isolation_level=None,
            factory=ManagedConnection,
            check_same_thread=False,
        )
        conn._coordination_conn = lease
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.row_factory = sqlite3.Row
        except BaseException:
            conn.close()
            raise
        return conn
    except BaseException:
        lease.close()
        raise


def get_runtime_connection(data_dir: Path) -> sqlite3.Connection:
    from taiwan_stock.infrastructure.migrate import validate_runtime_schema

    conn = get_connection(data_dir)
    try:
        validate_runtime_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def connection_dependency(data_dir: Path) -> Callable[[], Iterator[sqlite3.Connection]]:
    """Open one connection per request, with a lease held until it closes.

    `request.state.db` lets the template context reuse it (design 065).
    """
    from fastapi import Request

    def _get_db(request: Request) -> Iterator[sqlite3.Connection]:
        conn = get_runtime_connection(data_dir)
        request.state.db = conn
        try:
            yield conn
        finally:
            request.state.db = None
            conn.close()

    return _get_db
