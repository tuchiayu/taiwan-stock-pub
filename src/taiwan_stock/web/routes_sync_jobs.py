"""Asynchronous jobs API and a reloadable HTML fallback."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from taiwan_stock.catalog.sync_history import SOURCE_LABELS, suspected_changes
from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.shared.ids import new_id
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.presentation import local_return, with_query
from taiwan_stock.web.templates import templates

router = APIRouter()

WEB_OPERATIONS = frozenset(
    {
        "prices",
        "catalog",
        "stock-announcements",
        "held-etf-distributions",
        "investment-trust",
        "etf-data",
    }
)
RETRY_STATUSES = frozenset({"partial", "failed", "interrupted"})


def result_summary(job):
    if not job:
        return ""
    outcomes = job.get("unit_outcomes") or {}
    extra = [
        f"{label} {outcomes[key]}"
        for key, label in (
            ("source_changed", "來源待更新"),
            ("not_listed", "來源未收錄"),
        )
        if outcomes.get(key)
    ]
    # Design 055: one plain sentence per source down for maintenance.
    extra += [
        f"{SOURCE_LABELS.get(name, name)}網站維護中，{count} 檔沿用舊資料，可稍後重試"
        for name, count in outcomes.get("maintenance") or []
    ]
    text = _operation_summary(job) if job["result"] else ""
    return " · ".join(part for part in (text, "、".join(extra)) if part)


def _etf_data_summary(result):
    """Design 052 §4.2.3: one clause per phase, a failed phase named."""
    parts = []
    for key, label, describe in (
        (
            "catalog",
            "商品名單",
            lambda r: _operation_summary({"operation": "catalog", "result": r}),
        ),
        (
            "etf_profiles",
            "ETF 資料",
            lambda r: _operation_summary(
                {"operation": "investment-trust", "result": r}
            ),
        ),
        ("distributions", "全市場配息", _market_distribution_summary),
    ):
        phase = result.get(key) or {}
        if phase.get("error"):
            parts.append(f"{label}失敗：{phase['error']}")
        elif phase:
            parts.append(describe(phase))
    return "；".join(parts)


def _market_distribution_summary(phase):
    markets = [phase.get(m) or {} for m in ("twse", "tpex")]
    failed = [
        f"{name}失敗：{m['error']}"
        for name, m in zip(("TWSE", "TPEx"), markets, strict=True)
        if m.get("error")
    ]
    synced = sum(m.get("synced", 0) for m in markets)
    inserted = sum(m.get("inserted", 0) for m in markets)
    updated = sum(m.get("updated", 0) for m in markets)
    text = f"全市場配息 {synced} 筆（新增 {inserted}、更新 {updated}）"
    return "、".join([text, *failed])


def _operation_summary(job):
    from taiwan_stock.web.pages_holdings import _price_sync_summary
    from taiwan_stock.web.pages_income import _held_etf_refresh_message

    result = job["result"]
    if job["operation"] == "market-calendar":
        return (
            f"休市資料 {result.get('updated', 0)} 筆；"
            f"待確認或失敗 {result.get('failed_units', 0)} 個來源"
        )
    if job["operation"] == "etf-scale":
        return (
            f"ETF 規模／受益人數 {result.get('updated', 0)} 筆；"
            f"失敗 {result.get('failed_units', 0)} 個來源"
        )
    if job["operation"] == "investment-trust":
        c = result.get("coverage", {})
        return (
            f"ETF {c.get('total', 0)} 檔；銀行有值 {c.get('bank_known', 0)}、"
            f"官方頻率 {c.get('frequency_official', 0)}、"
            f"不配息 {c.get('non_distributing', 0)}、"
            f"推測 {c.get('frequency_inferred', 0)}、"
            f"頻率待確認 {c.get('frequency_unknown', 0)}"
        )
    if job["operation"] == "held-etf-distributions":
        return _held_etf_refresh_message(result)
    if job["operation"] == "etf-data":
        return _etf_data_summary(result)
    if job["operation"] == "prices":
        return _price_sync_summary(
            {"results": {m: result[m] for m in ("twse", "tpex") if m in result}},
            result["total_synced"],
        )
    if job["operation"] == "catalog":
        total = sum(
            result[k]
            for k in ("stock_count", "etf_count", "tpex_stock_count", "tpex_etf_count")
        )
        return f"已同步 {total} 檔商品"
    if job["operation"] == "stock-announcements":
        count = sum(
            result.get("history", {})[m]["distributions"]
            for m in ("twse", "tpex")
            if m in result.get("history", {})
        )
        markets = [
            result.get("pay_dates", {})[m]
            for m in ("twse", "tpex")
            if m in result.get("pay_dates", {})
        ]
        updated = sum(r["updated"] for r in markets)
        mismatches = sum(len(r["amount_mismatches"]) for r in markets)
        text = (
            f"配息 {result['distributions']} 筆；歷史現金股利回補 {count} 筆；"
            f"補上發放日 {updated} 筆"
        )
        rejected = result.get("skipped_invalid_ratio", 0)
        conflicts = len(result.get("reconciliation_warnings", []))
        if rejected:
            text += f"；來源比例驗證失敗 {rejected} 筆，可重試失敗來源"
        if conflicts:
            text += f"；本地交易需核對 {conflicts} 組，請修正交易（重抓資料不會解除）"
        if mismatches:
            text += f"（金額與 MOPS 不一致 {mismatches} 筆，未修改）"
        return text
    return ""


def get_job_launcher():
    """Replaceable process boundary for isolated MockTransport tests."""
    return sync_jobs.launch_worker


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    options: dict = Field(default_factory=dict)
    request_key: str | None = None


@router.post("/api/v2/sync-jobs", status_code=202)
def submit_job(
    body: Submission,
    conn=Depends(get_db),
    data_dir: Path = Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
):
    try:
        if body.operation not in WEB_OPERATIONS:
            raise ValueError("此操作僅提供 CLI 入口")
        if body.operation == "investment-trust" and body.options:
            raise ValueError("Web ETF 資料更新不接受額外範圍選項")
        return sync_jobs.submit(
            conn,
            data_dir,
            body.operation,
            body.options,
            request_key=body.request_key,
            launcher=launcher,
        )
    except sync_jobs.Conflict as exc:
        raise HTTPException(
            409, detail={"message": str(exc), "active_job_id": exc.job_id}
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/api/v2/sync-jobs")
def active_jobs(
    active: bool = True,
    request_key: str | None = None,
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
):
    result = sync_jobs.snapshot(conn, data_dir)
    if request_key is not None:
        row = conn.execute(
            "SELECT id FROM sync_jobs WHERE request_key=?", (request_key,)
        ).fetchone()
        result["requested_job"] = sync_jobs.get(conn, row[0]) if row else None
    if not active:
        result["jobs"] = [
            sync_jobs.get(conn, r[0])
            for r in conn.execute(
                "SELECT id FROM sync_jobs ORDER BY submitted_at DESC LIMIT 100"
            )
        ]
    return result


@router.get("/api/v2/sync-jobs/{job_id}")
def job_status(job_id: str, conn=Depends(get_db), data_dir=Depends(get_data_dir)):
    sync_jobs.recover(conn, data_dir)
    job = sync_jobs.get(conn, job_id)
    if not job:
        raise HTTPException(404, "找不到同步任務")
    return {**job, "summary": result_summary(job)}


def cancel(job_id, conn, data_dir):
    """Design 081: the same path as the CLI's Ctrl+C. A queued job is
    interrupted at once (design 067); a running one stops at its next
    progress check."""
    job_status(job_id, conn, data_dir)
    sync_jobs.request_cancel(conn, job_id)
    return job_status(job_id, conn, data_dir)


@router.post("/api/v2/sync-jobs/{job_id}/cancel")
def cancel_job(job_id: str, conn=Depends(get_db), data_dir=Depends(get_data_dir)):
    return cancel(job_id, conn, data_dir)


@router.post("/sync-jobs/{job_id}/cancel")
def cancel_job_form(
    job_id: str, conn=Depends(get_db), data_dir=Depends(get_data_dir)
) -> RedirectResponse:
    cancel(job_id, conn, data_dir)
    return RedirectResponse(f"/sync-jobs/{job_id}", 303)


@router.get("/sync-jobs/{job_id}", response_class=HTMLResponse)
def job_page(
    request: Request, job_id: str, conn=Depends(get_db), data_dir=Depends(get_data_dir)
):
    job = job_status(job_id, conn, data_dir)
    return templates.TemplateResponse(
        request=request, name="sync_job.html", context=job_context(conn, job)
    )


def job_context(conn, job, error=None, active_job_id=None, request_key=None):
    sources = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT source FROM catalog_sync_runs WHERE job_id=?",
            (job["id"],),
        )
    ]
    return {
        "job": job,
        "suspected_changes": suspected_changes(conn, sources),
        "retry_key": request_key or new_id(),
        "retry_allowed": job["operation"] in WEB_OPERATIONS
        and job["status"] in RETRY_STATUSES,
        "cancel_allowed": job["status"] in sync_jobs.ACTIVE
        and not job["cancel_requested"],
        "retry_error": error,
        "active_job_id": active_job_id,
    }


@router.post("/sync-jobs/{job_id}/retry", response_class=HTMLResponse)
def retry_job_form(
    job_id: str,
    request: Request,
    request_key: str = Form(""),
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
):
    job = job_status(job_id, conn, data_dir)
    error, active_id = None, None
    if job["operation"] not in WEB_OPERATIONS:
        error = "此任務僅提供 CLI 重試，請使用下方指令。"
    elif not request_key:
        error = "缺少提交識別，請重新開啟任務頁再重試。"
    else:
        try:
            retried = sync_jobs.retry(
                conn, data_dir, job_id, request_key=request_key, launcher=launcher
            )
        except sync_jobs.Conflict as exc:
            error, active_id = "其他同步正在執行，請先查看執行進度。", exc.job_id
        except ValueError as exc:
            error = str(exc)
        else:
            return RedirectResponse(f"/sync-jobs/{retried['id']}", 303)
    return templates.TemplateResponse(
        request=request,
        name="sync_job.html",
        context=job_context(conn, job, error, active_id, request_key),
        status_code=409,
    )


def submit_form(conn, data_dir, operation, launcher, target, request_key=None):
    try:
        job = sync_jobs.submit(
            conn, data_dir, operation, request_key=request_key, launcher=launcher
        )
        params = {"job_id": job["id"]}
    except sync_jobs.Conflict as exc:
        params = {"job_id": exc.job_id} if exc.job_id else {"sync_error": str(exc)}
    # Only local page returns are accepted. Preserve month/sort/filter choices.
    return RedirectResponse(
        with_query(local_return(target, "/securities"), **params), 303
    )


def compatible_sync(conn, data_dir, operation, launcher):
    try:
        job = sync_jobs.submit(conn, data_dir, operation, launcher=launcher)
    except sync_jobs.Conflict as exc:
        raise HTTPException(
            409, detail={"message": str(exc), "active_job_id": exc.job_id}
        ) from exc
    job = sync_jobs.wait(conn, data_dir, job["id"])
    if job["status"] in ("failed", "interrupted"):
        raise HTTPException(502, detail=f"TWSE／TPEx 來源同步失敗：{job['error']}")
    result = job["result"]
    if (
        operation == "prices"
        and result["errors"]
        and not any(k in result for k in ("twse", "tpex"))
    ):
        detail = "；".join(f"{m}：{r}" for m, r in result["errors"].items())
        raise HTTPException(502, detail=f"TWSE／TPEx 來源同步失敗：{detail}")
    return result
