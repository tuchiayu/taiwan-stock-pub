"""Design 081: cancelling a sync job from the Web."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def add_job(data_dir, job_id, status):
    with closing(get_connection(data_dir)) as conn:
        conn.execute(
            "INSERT INTO sync_jobs(id,operation,origin,request_key,parameters,"
            "status,submitted_at,owner_token) VALUES(?,'prices','web',?,'{}',?,?,?)",
            (job_id, f"key-{job_id}", status, sync_jobs.now_utc_iso(), "owner"),
        )
        if status in sync_jobs.ACTIVE:
            conn.execute(
                "INSERT INTO sync_job_resources VALUES(?,?)", (f"r-{job_id}", job_id)
            )


def stored(data_dir, job_id):
    with closing(get_connection(data_dir)) as conn:
        job = sync_jobs.get(conn, job_id)
        resources = conn.execute(
            "SELECT count(*) FROM sync_job_resources WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    return job, resources


def cancel_form(job_id):
    return f'action="/sync-jobs/{job_id}/cancel"'


def test_cancelling_a_queued_job_interrupts_it_at_once(client, tmp_path):
    add_job(tmp_path, "q", "queued")
    assert cancel_form("q") in client.get("/sync-jobs/q").text

    response = client.post("/api/v2/sync-jobs/q/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "interrupted"
    job, resources = stored(tmp_path, "q")
    assert job["status"] == "interrupted"
    assert job["error"] == "已在工作程序啟動前取消"
    assert resources == 0


def test_cancelling_a_running_job_waits_for_the_worker(client, tmp_path):
    add_job(tmp_path, "r", "running")
    # Holding the lease stands in for a live worker, so recovery leaves it.
    with sync_jobs.job_lease(tmp_path, "r"):
        assert cancel_form("r") in client.get("/sync-jobs/r").text

        response = client.post("/sync-jobs/r/cancel", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/sync-jobs/r"
        job, resources = stored(tmp_path, "r")
        assert (job["status"], job["cancel_requested"], resources) == ("running", 1, 1)
        page = client.get("/sync-jobs/r").text
        assert "取消中，等目前的請求結束後停止。" in page
        assert cancel_form("r") not in page
        # Cancelling again is harmless.
        again = client.post("/api/v2/sync-jobs/r/cancel")
        assert again.status_code == 200
        assert again.json()["cancel_requested"] == 1


def test_cancelling_a_finished_job_changes_nothing(client, tmp_path):
    add_job(tmp_path, "done", "success")
    assert cancel_form("done") not in client.get("/sync-jobs/done").text

    response = client.post("/api/v2/sync-jobs/done/cancel")

    assert response.status_code == 200
    job, _ = stored(tmp_path, "done")
    assert (job["status"], job["cancel_requested"]) == ("success", 0)
    form = client.post("/sync-jobs/done/cancel", follow_redirects=False)
    assert form.headers["location"] == "/sync-jobs/done"


@pytest.mark.parametrize(
    "path", ["/api/v2/sync-jobs/nope/cancel", "/sync-jobs/nope/cancel"]
)
def test_cancelling_an_unknown_job_is_404(client, path):
    assert client.post(path).status_code == 404


@pytest.mark.parametrize("path", ["/api/v2/sync-jobs/q/cancel", "/sync-jobs/q/cancel"])
def test_cancelling_from_another_origin_is_refused(client, tmp_path, path):
    add_job(tmp_path, "q", "queued")

    response = client.post(path, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    job, _ = stored(tmp_path, "q")
    assert (job["status"], job["cancel_requested"]) == ("queued", 0)
