"""Shared FastAPI dependency placeholders.

Routers depend on `get_db` by reference; the app assembly in
`main.py` overrides it via `app.dependency_overrides[get_db] = ...`
once the real data directory is known (FastAPI's standard pattern for
a dependency that needs runtime configuration). Tests override it the
same way, pointed at a temporary database.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx2

from taiwan_stock.shared.http import new_client


def get_db() -> Iterator[sqlite3.Connection]:  # pragma: no cover - always overridden
    raise NotImplementedError("get_db must be overridden via app.dependency_overrides")


def get_data_dir() -> Path:  # pragma: no cover - always overridden
    raise NotImplementedError(
        "get_data_dir must be overridden via app.dependency_overrides"
    )


def get_http_client() -> Iterator[httpx2.Client]:
    """Default override: a real client. Tests override this with one
    backed by `httpx2.MockTransport` so catalog sync never touches the
    network during the test suite (design 002 §5.1 CI 計算／UI 測試以
    合成資料...為基礎，不以即時網路...為必要 fixture).
    """
    with new_client() as client:
        yield client
