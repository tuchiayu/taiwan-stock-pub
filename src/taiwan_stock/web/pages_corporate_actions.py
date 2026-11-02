"""The corporate actions page and its forms (design 070)."""

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from taiwan_stock.catalog import (
    query as catalog_query,
)
from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import (
    corporate_action_reviews,
    corporate_actions,
)
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.presentation import ACTION_TYPE_LABELS
from taiwan_stock.web.routes_sync_jobs import get_job_launcher, submit_form
from taiwan_stock.web.templates import templates

router = APIRouter()


@router.get("/corporate-actions", response_class=HTMLResponse)
def corporate_actions_page(
    request: Request,
    synced: str | None = None,
    sync_error: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="corporate_actions.html",
        context={
            **_corporate_actions_context(conn),
            "synced": synced,
            "sync_error": sync_error,
        },
    )


@router.post("/corporate-actions/sync")
def sync_ex_dividend_form(
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
    request_key: str = Form(""),
):
    return submit_form(
        conn,
        data_dir,
        "stock-announcements",
        launcher,
        "/corporate-actions",
        request_key or None,
    )


@router.post("/corporate-actions", response_class=HTMLResponse)
def create_corporate_action_form(
    request: Request,
    security_code: str = Form(...),
    action_type: str = Form(...),
    ex_date: str = Form(...),
    from_shares: str = Form(""),
    to_shares: str = Form(""),
    per_thousand_shares: str = Form(""),
    note: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    form_values = {
        "security_code": security_code,
        "action_type": action_type,
        "ex_date": ex_date,
        "from_shares": from_shares,
        "to_shares": to_shares,
        "per_thousand_shares": per_thousand_shares,
        "note": note,
    }
    security = catalog_query.resolve_security_by_code(conn, security_code)
    if security is None:
        return templates.TemplateResponse(
            request=request,
            name="corporate_actions.html",
            context={
                **_corporate_actions_context(conn),
                "error": f"找不到商品代碼「{security_code}」，請確認代碼是否正確",
                "form_values": form_values,
            },
            status_code=422,
        )

    ratio_numerator = ratio_denominator = None
    try:
        if action_type == "split":
            ratio_numerator, ratio_denominator = corporate_actions.compute_split_ratio(
                int(from_shares), int(to_shares)
            )
        elif action_type == "stock_dividend":
            ratio_numerator, ratio_denominator = (
                corporate_actions.compute_stock_dividend_ratio(int(per_thousand_shares))
            )
    except ValueError:
        return templates.TemplateResponse(
            request=request,
            name="corporate_actions.html",
            context={
                **_corporate_actions_context(conn),
                "error": "股數與配股數請填入正整數",
                "form_values": form_values,
            },
            status_code=422,
        )

    try:
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": security["id"],
                "action_type": action_type,
                "ratio_numerator": ratio_numerator,
                "ratio_denominator": ratio_denominator,
                "ex_date": ex_date,
                "note": note or None,
            },
        )
    except corporate_actions.DuplicateActionError as exc:
        return templates.TemplateResponse(
            request=request,
            name="corporate_actions.html",
            context={
                **_corporate_actions_context(conn),
                "error": str(exc),
                "form_values": form_values,
            },
            status_code=409,
        )
    except corporate_actions.CorporateActionValidationError as exc:
        return templates.TemplateResponse(
            request=request,
            name="corporate_actions.html",
            context={
                **_corporate_actions_context(conn),
                "error": str(exc),
                "form_values": form_values,
            },
            status_code=422,
        )

    return RedirectResponse(url="/corporate-actions", status_code=303)


def _corporate_actions_context(conn: sqlite3.Connection) -> dict:
    from taiwan_stock.portfolio.action_revisions import list_revisions

    with read_transaction(conn):
        securities = conn.execute(
            "SELECT code, name FROM securities ORDER BY code, (status = 'active') DESC"
        ).fetchall()
        recent = conn.execute(
            """
            SELECT ca.*, s.code AS security_code, s.name AS security_name
            FROM corporate_actions ca
            JOIN securities s ON s.id = ca.security_id
            ORDER BY ca.ex_date DESC
            """
        ).fetchall()
        distributions = conn.execute(
            """
            SELECT d.*, s.code AS security_code, s.name AS security_name
            FROM distributions d
            JOIN securities s ON s.id = d.security_id
            ORDER BY d.ex_dividend_date DESC
            LIMIT 100
            """
        ).fetchall()
        book = corporate_action_reviews.ReviewBook(conn)
        accounts = conn.execute(
            "SELECT DISTINCT t.broker_account_id,t.security_id,b.name "
            "FROM live_trades t JOIN broker_accounts b ON b.id=t.broker_account_id"
        ).fetchall()
        review_accounts = {}
        for action in recent:
            if action["action_type"] not in corporate_action_reviews.MANUAL_TYPES:
                continue
            review_accounts[action["id"]] = [
                {
                    "id": account["broker_account_id"],
                    "name": account["name"],
                    "state": corporate_action_reviews.STATE_LABELS[
                        book.state(action["id"], account["broker_account_id"])
                    ],
                }
                for account in accounts
                if account["security_id"] == action["security_id"]
            ]
        return {
            "revisions": list_revisions(conn),
            "securities": securities,
            "actions": recent,
            "review_accounts": review_accounts,
            "distributions": distributions,
            "action_type_labels": ACTION_TYPE_LABELS,
            "error": None,
            "form_values": {},
        }
