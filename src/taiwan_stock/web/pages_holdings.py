"""Holdings pages: `/` and the sale simulation (design 070).

Plain HTML `<form>` POSTs handled directly by these routes (redirect
after post) rather than JS `fetch()` calls to `/api/v1/...` — the
architecture diagram (design 002 §4.5) allows "本機 JavaScript" but
does not require it. The JSON API stays available under `/api/v1`.
"""

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import (
    corporate_action_reviews,
    corporate_actions,
    distribution_entitlement,
    replay,
    simulate,
)
from taiwan_stock.portfolio import trades as trades_repo
from taiwan_stock.portfolio.trading_rules import resolve_trading_rules
from taiwan_stock.shared.dates import today_taipei
from taiwan_stock.shared.version import get_version
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.presentation import ACTION_TYPE_LABELS, SYNC_MARKET_LABELS
from taiwan_stock.web.routes_sync_jobs import get_job_launcher, submit_form
from taiwan_stock.web.templates import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def homepage(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    prices_synced: int | None = None,
    price_sync_summary: str | None = None,
    sync_error: str | None = None,
) -> HTMLResponse:
    with read_transaction(conn):
        data_error = None
        position_result = None
        try:
            position_result = trades_repo.position_overview_result(conn)
            positions = position_result["items"]
            corporate_action_reviews.ReviewBook(conn).annotate(positions)
            unhandled_actions = (
                corporate_actions.list_unhandled_actions_for_held_securities(conn)
            )
        except replay.OversellError as exc:
            # A stored trades/corporate_actions combination has become
            # inconsistent (e.g. from data written before the §4.11 ratio
            # check existed) — show it as a data problem to fix rather than
            # letting the homepage itself 500 with no way to reach any
            # other page to correct it.
            positions = []
            unhandled_actions = []
            data_error = (
                "持股資料不一致，重播交易紀錄時發現超賣："
                f"{exc}。請檢查交易紀錄與股票異動頁面。"
            )
        history_warnings = (
            position_result["warnings"]
            if position_result is not None
            else distribution_entitlement.history_warnings(conn)
        )
        portfolio_summary = trades_repo.compute_portfolio_summary(
            positions, excluded_positions=history_warnings
        )
        portfolio_summary["contains_estimated_cost"] = any(
            p.get("cost_estimates") for p in positions
        )
        from taiwan_stock.web.holdings_view import arrange

        positions, holding_state = arrange(positions, request.query_params)
        response_kwargs = {
            "request": request,
            "name": "index.html",
            "context": {
                "version": get_version(),
                "positions": positions,
                "holding_state": holding_state,
                "portfolio_summary": portfolio_summary,
                "unhandled_actions": unhandled_actions,
                "action_type_labels": ACTION_TYPE_LABELS,
                "prices_synced": prices_synced,
                "price_sync_summary": price_sync_summary,
                "sync_error": sync_error,
                "data_error": data_error,
                "history_warnings": history_warnings,
            },
        }
    return templates.TemplateResponse(**response_kwargs)


def _price_sync_summary(outcome: dict, total: int) -> str:
    """Design 013 §4.6: which trading days each market ended up on, so
    a market still behind (or served from the OpenAPI fallback) is
    visible right after syncing."""
    parts = []
    for market, result in outcome["results"].items():
        dates = "、".join(f"{d[5:7]}/{d[8:10]}" for d in result["trade_dates"])
        part = f"{SYNC_MARKET_LABELS[market]} {dates or '無新資料'}"
        if result["source"] == "openapi_fallback":
            part += f"（依日期來源失敗，改用 OpenAPI：{result['fallback_reason']}）"
        if result.get("incomplete_dates"):
            part += "（部分商品未核對，請稍後重試）"
        parts.append(part)
    requested = any(
        r.get("fetched_dates") or r["source"] == "openapi_fallback"
        for r in outcome["results"].values()
    )
    suffix = (
        f"（寫入 {total} 筆）" if total or requested else "（已是最新，未重新下載）"
    )
    return "；".join(parts) + suffix


@router.post("/prices/sync")
def sync_prices_form(
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
    request_key: str = Form(""),
):
    return submit_form(conn, data_dir, "prices", launcher, "/", request_key or None)


@router.get("/sale-simulation", response_class=HTMLResponse)
def sale_simulation_page(
    request: Request,
    broker_account_id: str,
    security_id: str,
    quantity: int | None = None,
    price: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    with read_transaction(conn):
        security = conn.execute(
            "SELECT * FROM securities WHERE id = ?",
            (security_id,),
        ).fetchone()
        # Design 074: 404, not a 500 at `rules.fees`, for an unknown id.
        if security is None:
            raise HTTPException(404, "找不到商品")

        rule_date = today_taipei()
        rules = resolve_trading_rules(conn, security, rule_date)
        result = None
        error = None
        position = None
        if problem := distribution_entitlement.stored_problem(
            conn, broker_account_id, security_id
        ):
            error = simulate.PROBLEM_MESSAGES[problem["reason"]]
        else:
            try:
                position = trades_repo.compute_position(
                    conn, broker_account_id, security_id, as_of_date=rule_date
                )
            except replay.OversellError as exc:
                error = f"持股資料不一致，重播交易紀錄時發現超賣：{exc}"

        if position is not None and quantity is not None:
            fee_schedule = rules.fees
            trades = trades_repo.fetch_trades(conn, broker_account_id, security_id)
            actions = trades_repo.fetch_corporate_actions(conn, security_id)
            try:
                result = simulate.simulate_partial_sale(
                    trades,
                    actions,
                    quantity,
                    fee_schedule,
                    as_of_date=rule_date,
                    tick_schedule=rules.tick_schedule,
                    price=price,
                )
            except simulate.SimulationError as exc:
                error = str(exc)

        response_kwargs = {
            "request": request,
            "name": "sale_simulation.html",
            "context": {
                "broker_account_id": broker_account_id,
                "security_id": security_id,
                "security": security,
                "position": position,
                "quantity": quantity,
                "price": price or "",
                "result": result,
                "pricing_warnings": list(rules.warnings),
                "pricing_notices": list(rules.notices),
                "error": error,
                "cost_estimates": corporate_action_reviews.ReviewBook(conn).quality(
                    broker_account_id, security_id, position
                )
                if position is not None
                else [],
            },
        }
    return templates.TemplateResponse(**response_kwargs)
