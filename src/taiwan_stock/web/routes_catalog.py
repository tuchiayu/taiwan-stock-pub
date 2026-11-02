"""Search API and v1 synchronous adapters for the durable jobs worker.

Success/source-failure payloads stay compatible. New operation conflicts are
reported as 409 with the active job ID; v2 clients poll instead of waiting.
"""

import sqlite3

from fastapi import APIRouter, Depends, Query

from taiwan_stock.catalog import search
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.routes_sync_jobs import compatible_sync, get_job_launcher

router = APIRouter(prefix="/api/v1")


@router.get("/securities")
def list_securities(
    q: str | None = Query(default=None),
    security_type: str | None = Query(default=None),
    limit: int = Query(default=search.DEFAULT_LIMIT, ge=1, le=search.MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    rows = search.search_securities(
        conn, query=q, security_type=security_type, limit=limit, offset=offset
    )
    return {"items": [dict(row) for row in rows], "limit": limit, "offset": offset}


# v1 retains synchronous response shapes; execution belongs to the shared worker.
@router.post("/catalog-sync")
def trigger_catalog_sync(
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
):
    return compatible_sync(conn, data_dir, "catalog", launcher)


@router.post("/ex-dividend-sync")
def trigger_ex_dividend_sync(
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
):
    return compatible_sync(conn, data_dir, "stock-announcements", launcher)


@router.post("/held-etf-distributions-sync")
def trigger_held_etf_distributions_sync(
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
):
    return compatible_sync(conn, data_dir, "held-etf-distributions", launcher)


@router.post("/prices-sync")
def trigger_prices_sync(
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
):
    return compatible_sync(conn, data_dir, "prices", launcher)
