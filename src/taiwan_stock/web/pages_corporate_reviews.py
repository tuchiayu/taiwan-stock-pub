"""HTML forms for account-specific corporate-action review."""

import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import corporate_action_reviews as reviews
from taiwan_stock.portfolio import replay
from taiwan_stock.web.deps import get_db
from taiwan_stock.web.templates import templates

router = APIRouter()


def _render(
    request,
    conn,
    action_id,
    account_id,
    *,
    page=1,
    error=None,
    status_code=200,
    note="",
    decision="",
):
    with read_transaction(conn):
        try:
            context = reviews.preview(conn, action_id, account_id)
        except reviews.ReviewNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except replay.OversellError as exc:
            raise HTTPException(
                422, "持股資料不一致，請先至交易紀錄檢查超賣問題"
            ) from exc
        pagination = reviews.history_page_info(conn, action_id, account_id, page)
        history, _ = reviews.history_page(
            conn, action_id, account_id, pagination["page"]
        )
        response_kwargs = {
            "request": request,
            "name": "corporate_action_review.html",
            "context": {
                **context,
                "history": history,
                **pagination,
                "page_link": lambda target: (
                    f"/corporate-actions/{action_id}/review?"
                    + urlencode({"broker_account_id": account_id, "page": target})
                ),
                "error": error,
                "form_note": note,
                "form_decision": decision,
                "decision_labels": reviews.DECISION_LABELS,
                "state_labels": reviews.STATE_LABELS,
            },
            "status_code": status_code,
        }
    return templates.TemplateResponse(**response_kwargs)


@router.get("/corporate-actions/{action_id}/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    action_id: str,
    broker_account_id: str,
    page: int = Query(1, ge=1, le=1000000),
    conn=Depends(get_db),
):
    return _render(request, conn, action_id, broker_account_id, page=page)


def _submit(request, conn, action_id, account_id, decision, note, token):
    try:
        reviews.record(conn, action_id, account_id, decision, note, token)
    except reviews.ReviewNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except replay.OversellError as exc:
        raise HTTPException(422, "持股資料不一致，請先至交易紀錄檢查超賣問題") from exc
    except (reviews.ReviewValidationError, reviews.StaleReviewError) as exc:
        return _render(
            request,
            conn,
            action_id,
            account_id,
            error=str(exc),
            status_code=409 if isinstance(exc, reviews.StaleReviewError) else 422,
            note=note,
            decision=decision,
        )
    return RedirectResponse(
        f"/corporate-actions/{action_id}/review?"
        + urlencode({"broker_account_id": account_id, "saved": "1"}),
        status_code=303,
    )


@router.post("/corporate-actions/{action_id}/review")
def confirm_form(
    request: Request,
    action_id: str,
    broker_account_id: str = Form(""),
    decision: str = Form(""),
    note: str = Form(""),
    review_token: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    if decision == "reopened":
        return _render(
            request,
            conn,
            action_id,
            broker_account_id,
            error="請使用重新開啟表單並填寫原因",
            status_code=422,
            note=note,
        )
    return _submit(
        request, conn, action_id, broker_account_id, decision, note, review_token
    )


@router.post("/corporate-actions/{action_id}/reopen")
def reopen_form(
    request: Request,
    action_id: str,
    broker_account_id: str = Form(""),
    note: str = Form(""),
    review_token: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    return _submit(
        request, conn, action_id, broker_account_id, "reopened", note, review_token
    )
