"""Unit tests for data-directory resolution (design 002 D07).

The Windows-default and POSIX-default branches each construct a real
`pathlib.Path`, and Python 3.13 refuses to instantiate a concrete
`WindowsPath` on POSIX (or `PosixPath` on Windows) even via
`monkeypatch.setattr(os, "name", ...)` — so each branch can only be
exercised on its own native OS, not simulated cross-platform. The two
tests below are skipped on the non-matching host instead of faking a
foreign OS, since faking it would either fail with
`UnsupportedOperation` or silently test nothing.
"""

import sys
from pathlib import Path

import pytest

from taiwan_stock.infrastructure import db


def test_explicit_cli_path_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv(db.ENV_VAR, str(tmp_path / "from-env"))

    resolved = db.resolve_data_dir(str(tmp_path / "from-cli"))

    assert resolved == (tmp_path / "from-cli").resolve()


def test_env_var_wins_when_no_cli_path(monkeypatch, tmp_path):
    monkeypatch.setenv(db.ENV_VAR, str(tmp_path / "from-env"))

    resolved = db.resolve_data_dir(None)

    assert resolved == (tmp_path / "from-env").resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="constructs a real WindowsPath")
def test_windows_default_uses_local_app_data(monkeypatch):
    monkeypatch.delenv(db.ENV_VAR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\tester\AppData\Local")

    resolved = db.resolve_data_dir(None)

    assert resolved == Path(r"C:\Users\tester\AppData\Local\TaiwanStock\data")


@pytest.mark.skipif(sys.platform == "win32", reason="constructs a real PosixPath")
def test_posix_default_uses_xdg_data_home(monkeypatch):
    monkeypatch.delenv(db.ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "/home/tester/.local/share")

    resolved = db.resolve_data_dir(None)

    assert resolved == Path("/home/tester/.local/share/taiwan-stock")


def test_get_connection_creates_db_file_with_pragmas_enabled(tmp_path):
    conn = db.get_connection(tmp_path)
    try:
        assert (tmp_path / db.DB_FILENAME).exists()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
