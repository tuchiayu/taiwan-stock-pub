"""The income calendar and its held-ETF refresh (design 070)."""

import sqlite3
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from taiwan_stock.catalog import (
    market_calendar,
)
from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import (
    distribution_entitlement,
    income_calendar,
    replay,
)
from taiwan_stock.portfolio.income_scope import ScopeOptions
from taiwan_stock.portfolio.yields import add_months
from taiwan_stock.shared.dates import today_taipei
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.presentation import SYNC_MARKET_LABELS
from taiwan_stock.web.routes_sync_jobs import get_job_launcher, submit_form
from taiwan_stock.web.templates import templates

router = APIRouter()


def _held_etf_refresh_message(outcome: dict) -> str:
    counts = "、".join(
        f"{label} {outcome[market]['etfs']} 檔"
        for market, label in SYNC_MARKET_LABELS.items()
        if market in outcome and outcome[market]["etfs"]
    )
    if not counts and not outcome["errors"]:
        return "沒有持有或追蹤中的 ETF，未更新"
    markets = [outcome[market] for market in SYNC_MARKET_LABELS if market in outcome]
    synced = sum(result["synced"] for result in markets)
    changes = ""
    # Older persisted jobs only have the processed count, not a change breakdown.
    if all(
        all(key in result for key in ("inserted", "updated", "unchanged"))
        for result in markets
    ):
        changes = (
            "（"
            + "、".join(
                f"{label} {sum(result[key] for result in markets)} 筆"
                for key, label in (
                    ("inserted", "新增"),
                    ("updated", "更新"),
                    ("unchanged", "未變更"),
                )
            )
            + "）"
        )
    message = (
        f"已更新持有 ETF 配息：{counts or '無'}，核對 {synced} 筆{changes}；"
        f"仍待公布金額 {outcome['pending_amounts']} 筆"
    )
    failures = [
        f"{label} {len(outcome[market]['failed_units'])} 個單位失敗，"
        f"可執行 taiwan-stock sync retry {outcome[market]['run_id']}"
        for market, label in SYNC_MARKET_LABELS.items()
        if market in outcome and outcome[market]["failed_units"]
    ] + [
        f"{SYNC_MARKET_LABELS[market]}：{reason}"
        for market, reason in outcome["errors"].items()
    ]
    if failures:
        message += "（" + "；".join(failures) + "）"
    return message


@router.post("/income-calendar/refresh-etf")
def refresh_held_etf_form(
    month: str = Form(""),
    filter_value: str = Form("all", alias="filter"),
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
    request_key: str = Form(""),
):
    params = {}
    if month:
        params["month"] = month
    if filter_value != "all":
        params["filter"] = filter_value
    return submit_form(
        conn,
        data_dir,
        "held-etf-distributions",
        launcher,
        "/income-calendar?" + urlencode(params),
        request_key or None,
    )


@router.get("/income-calendar", response_class=HTMLResponse)
def income_calendar_page(
    request: Request,
    month: str | None = None,
    preview: bool = False,
    view: str = "month",
    filter_value: str = Query("all", alias="filter"),
    return_month: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    # Design 009 §4.5: the old 12-month historical-frequency preview
    # entry point is gone — redirect it to the announced-only calendar
    # with a notice, rather than silently reinterpreting `preview=true`.
    with read_transaction(conn):
        if preview:
            return RedirectResponse(
                url="/income-calendar?view=all&notice=preview-removed", status_code=303
            )
        today = today_taipei()
        selected_month = month or today[:7]
        options = ScopeOptions(conn)
        history = distribution_entitlement.HistoryBook(conn)
        scope = options.all
        filter_error = None
        try:
            scope = options.select(filter_value)
        except ValueError as exc:
            filter_error = str(exc)
        try:
            if len(selected_month) != 7:
                raise ValueError("月份格式必須是 YYYY-MM")
            try:
                first = date.fromisoformat(selected_month + "-01")
            except ValueError as exc:
                raise ValueError("月份格式必須是 YYYY-MM") from exc
            if return_month:
                try:
                    if len(return_month) != 7:
                        raise ValueError
                    date.fromisoformat(return_month + "-01")
                except ValueError as exc:
                    raise ValueError("返回月份格式必須是 YYYY-MM") from exc
            if filter_error:
                raise ValueError(filter_error)
            entries = income_calendar.calendar_view(
                conn,
                month=month if view == "all" else None,
                filter_value=scope.value,
                options=options,
                history=history,
            )
            error = None
        except (ValueError, replay.OversellError) as exc:
            entries = []
            error = str(exc)
            selected_month = today[:7]
            first = date.fromisoformat(selected_month + "-01")
        monthly = view != "all"
        overview = income_calendar.monthly_overview(entries, selected_month)
        market = market_calendar.view(conn, first.year)
        for day in overview["days"]:
            if day["date"]:
                day["market"] = market_calendar.day_info(
                    day["date"], market["events"].get(day["date"])
                )
        previous_month = (
            add_months(first, -1).isoformat()[:7]
            if first.year > 1 or first.month > 1
            else None
        )
        next_month = (
            add_months(first, 1).isoformat()[:7]
            if first.year < 9999 or first.month < 12
            else None
        )

        def calendar_url(**params):
            params = {"filter": scope.value, **params}
            if params["filter"] == "all":
                del params["filter"]
            query = urlencode({k: v for k, v in params.items() if v})
            return "/income-calendar" + (f"?{query}" if query else "")

        form_state = (
            {"month": selected_month}
            if monthly
            else {"view": "all", "month": month, "return_month": return_month}
        )
        response_kwargs = {
            "request": request,
            "name": "income_calendar.html",
            "status_code": 422 if filter_error else 200,
            "context": {
                "entries": entries,
                "today": today,
                "month": selected_month,
                "month_label": f"{first.year} 年 {first.month} 月",
                "previous_month": previous_month,
                "next_month": next_month,
                "scope": scope,
                "scope_options": options,
                "filter_form_state": form_state,
                "previous_url": calendar_url(month=previous_month),
                "next_url": calendar_url(month=next_month),
                "today_url": calendar_url(),
                "announcements_url": calendar_url(
                    view="all", return_month=selected_month
                ),
                "calendar_url": calendar_url(month=return_month or month),
                "clear_filter_url": calendar_url(**form_state, filter="all"),
                "monthly": monthly,
                "overview": overview,
                "market_calendar": market,
                "unscheduled": [e for e in entries if not e["pay_date"]]
                if monthly
                else [],
                "notice": request.query_params.get("notice"),
                "etf_refresh": request.query_params.get("etf_refresh"),
                "etf_refresh_failed": bool(
                    request.query_params.get("etf_refresh_failed")
                ),
                "error": error,
                "history_warnings": income_calendar.scoped_warnings(
                    conn, options, scope.value, history
                ),
            },
        }
    return templates.TemplateResponse(**response_kwargs)
