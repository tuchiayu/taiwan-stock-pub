"""Read local closure data; admit background refreshes through existing leases."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from taiwan_stock.catalog import market_calendar
from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.routes_sync_jobs import get_job_launcher

router = APIRouter()


@router.get("/api/v2/market-calendar")
def calendar_status(
    year: int = Query(ge=1, le=9999),
    requestKey: str | None = Query(None, max_length=128),
    conn=Depends(get_db),
):
    result = market_calendar.view(conn, year)
    if requestKey:
        row = conn.execute(
            "SELECT id FROM sync_jobs WHERE operation='market-calendar' "
            "AND request_key=?",
            (requestKey,),
        ).fetchone()
        result["requestedJobId"] = row[0] if row else None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


class Refresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int = Field(ge=1, le=9999, strict=True)
    requestKey: str = Field(min_length=1, max_length=128)
    force: bool = Field(default=False, strict=True)


@router.post("/api/v2/market-calendar/refresh")
def refresh_calendar(
    body: Refresh,
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
):
    if not market_calendar.supported(body.year):
        raise HTTPException(422, "不支援的休市資料年度")
    try:
        job = sync_jobs.submit(
            conn,
            data_dir,
            "market-calendar",
            {"year": body.year, "force": body.force},
            request_key=body.requestKey,
            launcher=launcher,
            admit=lambda: bool(
                market_calendar.due_sources(conn, body.year, force=body.force)
            ),
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
