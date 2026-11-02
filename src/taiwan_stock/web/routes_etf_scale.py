"""Read local ETF size/holder dates; admit background refreshes (design 044 §4.2.4)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from taiwan_stock.catalog import etf_scale
from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.routes_sync_jobs import get_job_launcher

router = APIRouter()


@router.get("/api/v2/etf-scale")
def scale_status(
    requestKey: str | None = Query(None, max_length=128),
    conn=Depends(get_db),
):
    result = etf_scale.view(conn)
    if requestKey:
        row = conn.execute(
            "SELECT id FROM sync_jobs WHERE operation='etf-scale' AND request_key=?",
            (requestKey,),
        ).fetchone()
        result["requestedJobId"] = row[0] if row else None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


class Refresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestKey: str = Field(min_length=1, max_length=128)
    force: bool = Field(default=False, strict=True)


@router.post("/api/v2/etf-scale/refresh")
def refresh_scale(
    body: Refresh,
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
):
    try:
        job = sync_jobs.submit(
            conn,
            data_dir,
            "etf-scale",
            {"force": body.force},
            request_key=body.requestKey,
            launcher=launcher,
            admit=lambda: bool(etf_scale.due_sources(conn, force=body.force)),
        )
    except sync_jobs.Conflict as exc:
        return JSONResponse({"activeJobId": exc.job_id}, status_code=409)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse(
        {"jobId": job["id"] if job else None},
        status_code=202 if job else 200,
        headers={"Cache-Control": "no-store"},
    )
