"""Shared test fixtures."""

from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from taiwan_stock import cli
from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.shared import pacing
from taiwan_stock.web.deps import get_http_client
from taiwan_stock.web.routes_sync_jobs import get_job_launcher

DEPLOYMENT_DIR = Path(__file__).resolve().parents[1] / "docs" / "deployment"


@pytest.fixture
def rollback_sql():
    """Reads a rollback script by file name. The public export ships no
    docs/, so these tests skip there; a missing file here still fails."""
    if not DEPLOYMENT_DIR.is_dir():
        pytest.skip("docs/deployment is not part of this checkout")
    return lambda name: (DEPLOYMENT_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_sync_workers(request, monkeypatch):
    """Ordinary transport tests execute the real worker with mocked HTTP.

    Process-lifecycle tests explicitly opt out and use actual OS processes.
    Never let a pre-existing API/CLI fixture accidentally contact live sources.
    """
    if request.node.get_closest_marker("real_workers"):
        return

    def cli_launch(data_dir, job_id):
        with cli.new_client() as client:
            sync_jobs.run_worker(data_dir, job_id, client=client)

    monkeypatch.setattr(sync_jobs, "launch_worker", cli_launch)
    original = TestClient.__init__

    def install(self, app, *args, **kwargs):
        def launcher(client=Depends(get_http_client)):
            return lambda data_dir, job_id: sync_jobs.run_worker(
                data_dir, job_id, client=client
            )

        app.dependency_overrides.setdefault(get_job_launcher, launcher)
        original(self, app, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", install)


@pytest.fixture(autouse=True)
def _local_test_host(monkeypatch):
    """Issue #87: the app only answers local host names, so test clients
    address it the way a browser on this machine does, not "testserver"."""
    original = TestClient.__init__

    def local(self, app, *args, base_url="http://127.0.0.1", **kwargs):
        original(self, app, *args, base_url=base_url, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", local)


@pytest.fixture(autouse=True)
def _no_source_pacing(monkeypatch):
    """Design 005 §4.4's sync throttle/retry backoff waits through
    `pacing.pause`; tests never talk to a real slow source, so skip only
    those waits. A test's own `time.sleep` still waits (design 066)."""
    monkeypatch.setattr(pacing, "pause", lambda _seconds: None)
