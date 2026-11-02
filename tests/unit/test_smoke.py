from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app


def test_index_page_renders_successfully(tmp_path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Taiwan Stock" in response.text


def test_health_endpoint_returns_ok(tmp_path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_endpoint_reports_a_broken_migration_instead_of_the_app_not_starting(
    tmp_path,
):
    """A startup migration failure (here: a tampered checksum on an
    already-applied migration, DEVELOPMENT.md §2.2.4's immutability
    check) must not take the whole server down — /health should still
    answer, reporting the problem, rather than becoming unreachable
    along with every other route.
    """
    conn = get_connection(tmp_path)
    conn.execute(
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, checksum) VALUES (1, 'tampered')"
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "error"
