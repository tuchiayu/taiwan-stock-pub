"""Migration serialization must survive executescript and process boundaries."""

import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from taiwan_stock.infrastructure import migrate


def connect(path):
    c = sqlite3.connect(path, isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    return closing(c)


def test_two_connections_reread_versions_after_acquiring_lock(tmp_path, monkeypatch):
    path = tmp_path / "concurrent.db"
    held, release, attempted = (threading.Event() for _ in range(3))
    original = migrate.discover_migrations

    def discover():
        if threading.current_thread().name.endswith("_0"):
            held.set()
            assert release.wait(10)
        return original()

    monkeypatch.setattr(migrate, "discover_migrations", discover)

    def upgrade(second=False):
        with connect(path) as c:
            if second:
                attempted.set()
            return migrate.apply_pending(c)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(upgrade)
        try:
            assert held.wait(10)
            second = pool.submit(upgrade, True)
            assert attempted.wait(10)
            assert not second.done()
        finally:
            release.set()
        assert first.result(15) == list(range(1, 24))
        assert second.result(15) == []


PROCESS_SCRIPT = r"""
import json, pathlib, sqlite3, sys, time
from taiwan_stock.infrastructure import migrate
path, role, folder = sys.argv[1:]
folder = pathlib.Path(folder)
c = sqlite3.connect(path, isolation_level=None, timeout=10)
c.row_factory = sqlite3.Row
original = migrate.discover_migrations
def discover():
    (folder / (role + ".entered")).touch()
    if role == "first":
        deadline = time.monotonic() + 20
        while not (folder / "release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("parent did not release migration")
            time.sleep(.01)
    return original()
migrate.discover_migrations = discover
(folder / (role + ".started")).touch()
try:
    print(json.dumps(migrate.apply_pending(c)), flush=True)
finally:
    c.close()
"""


def wait_file(path):
    deadline = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"child did not create {path.name}")
        threading.Event().wait(0.01)


@pytest.mark.parametrize("existing_version", [0, 16, 17])
def test_independent_processes_upgrade_once(tmp_path, monkeypatch, existing_version):
    path = tmp_path / "shared.db"
    with connect(path) as c:
        if existing_version:
            versions = migrate.discover_migrations()
            with monkeypatch.context() as patch:
                patch.setattr(
                    migrate,
                    "discover_migrations",
                    lambda: [m for m in versions if m.version <= existing_version],
                )
                migrate.apply_pending(c)
    children = []
    try:
        for role in ["first", "second"]:
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        PROCESS_SCRIPT,
                        str(path),
                        role,
                        str(tmp_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            wait_file(
                tmp_path / (role + (".entered" if role == "first" else ".started"))
            )
        assert not (tmp_path / "second.entered").exists()
        (tmp_path / "release").touch()
        results = []
        for child in children:
            out, err = child.communicate(timeout=20)
            assert child.returncode == 0, err
            results.append(json.loads(out))
        assert results == [list(range(existing_version + 1, 24)), []]
    finally:
        (tmp_path / "release").touch()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
    with connect(path) as c:
        assert c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 23
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_process_death_releases_lock_and_other_databases_are_independent(tmp_path):
    path = tmp_path / "crash.db"
    child = subprocess.Popen(
        [sys.executable, "-c", PROCESS_SCRIPT, str(path), "first", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_file(tmp_path / "first.entered")
        with connect(tmp_path / "separate.db") as other:
            assert migrate.apply_pending(other) == list(range(1, 24))
    finally:
        child.kill()
        child.communicate(timeout=5)
    with connect(path) as c:
        assert migrate.apply_pending(c) == list(range(1, 24))


def test_failed_migration_releases_lock_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "failure.db"
    broken = tmp_path / "0001_broken.sql"
    broken.write_text(
        "CREATE TABLE partial(id); SELECT missing FROM missing;", encoding="utf-8"
    )
    with connect(path) as c:
        with monkeypatch.context() as patch:
            patch.setattr(
                migrate,
                "discover_migrations",
                lambda: [migrate.Migration(1, "broken", broken)],
            )
            with pytest.raises(sqlite3.OperationalError):
                migrate.apply_pending(c)
        assert not c.in_transaction
        assert (
            c.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone()
            is None
        )
        assert migrate.apply_pending(c) == list(range(1, 24))


def test_in_memory_databases_are_independent():
    for _ in range(2):
        with connect(":memory:") as c:
            assert migrate.apply_pending(c) == list(range(1, 24))
