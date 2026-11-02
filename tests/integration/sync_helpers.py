"""Follow the durable job link returned by HTML synchronization forms."""

from urllib.parse import parse_qs, urlparse


def load_job(client, response):
    job_id = parse_qs(urlparse(response.headers["location"]).query)["job_id"][0]
    status = client.get(f"/api/v2/sync-jobs/{job_id}")
    assert status.status_code == 200
    return status.json()
