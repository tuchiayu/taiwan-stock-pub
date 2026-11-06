"""Transactions, CSV import/export and the broker-account form (design 070)."""

import sqlite3
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from taiwan_stock.portfolio import (
    broker_accounts,
    csv_trades,
    replay,
    trade_imports,
    trade_query,
)
from taiwan_stock.portfolio import trades as trades_repo
from taiwan_stock.shared.dates import today_taipei
from taiwan_stock.web import trade_forms
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.presentation import local_return, with_query
from taiwan_stock.web.templates import templates

router = APIRouter()

EVENT_TYPE_LABELS = {"buy": "買進", "sell": "賣出", "opening": "期初持股"}
IMPORT_FIELD_LABELS = {**trade_forms.LABELS, "is_day_trade": "當沖", "note": "備註"}


@router.get("/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    context = _transactions_context(conn, request.query_params)
    values = context["form_values"]
    code = request.query_params.get("security_code", "")
    account = request.query_params.get("new_account_id", "")
    if code:
        if conn.execute("SELECT 1 FROM securities WHERE code=?", (code,)).fetchone():
            values["security_code"] = code
        else:
            context["prefill_notice"] = "商品預填條件已失效，請重新選擇。"
    if account:
        if any(a["id"] == account for a in context["broker_accounts"]):
            values["broker_account_id"] = account
        else:
            context["prefill_notice"] = "帳戶預填條件已失效，請重新選擇。"
    if request.query_params.get("account_created") == "1":
        context["success"] = "已建立券商帳戶，可新增交易。"
    saved = request.query_params.get("saved")
    if saved:
        row = conn.execute(
            "SELECT s.code,s.name FROM trades t "
            "JOIN securities s ON s.id=t.security_id WHERE t.id=?",
            (saved,),
        ).fetchone()
        if row:
            context["success"] = f"已儲存交易：{row['code']} {row['name']}。"
    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context=context,
        status_code=422 if context["filter_errors"] else 200,
    )


@router.post("/broker-accounts")
def create_broker_account_form(
    request: Request,
    name: str = Form(""),
    dividend_bank_name: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    if not name.strip():
        return templates.TemplateResponse(
            request=request,
            name="transactions.html",
            context={
                **_transactions_context(conn),
                "error": "請填帳戶名稱。",
                "account_name": name,
                "field_errors": {"name": "請填帳戶名稱。"},
            },
            status_code=422,
        )
    broker_accounts.create_broker_account(
        conn, {"name": name.strip(), "dividend_bank_name": dividend_bank_name or None}
    )
    return RedirectResponse(url="/transactions?account_created=1", status_code=303)


@router.post("/transactions", response_class=HTMLResponse)
def create_trade_form(
    request: Request,
    broker_account_id: str = Form(""),
    security_code: str = Form(""),
    event_type: str = Form(""),
    trade_date: str = Form(""),
    quantity: str = Form(""),
    unit_price: str = Form(""),
    executed_amount: str = Form(""),
    commission: str = Form(""),
    transaction_tax: str = Form(""),
    other_fees: str = Form(""),
    is_day_trade: str = Form(""),
    note: str = Form(""),
    return_to: str = Form("/transactions"),
    enhanced: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    form_values = trade_forms.ungroup(
        {
            "broker_account_id": broker_account_id,
            "security_code": security_code,
            "event_type": event_type,
            "trade_date": trade_date,
            "quantity": quantity,
            "unit_price": unit_price,
            "executed_amount": executed_amount,
            "commission": commission,
            "transaction_tax": transaction_tax,
            "other_fees": other_fees,
            "is_day_trade": is_day_trade,
            "note": note,
        }
    )
    errors = trade_forms.validate(form_values)
    security = trade_forms.resolve_security(conn, security_code)
    if not security:
        errors["security_code"] = (
            f"找不到商品代碼「{security_code}」，請確認代碼是否正確"
        )
    if not conn.execute(
        "SELECT 1 FROM broker_accounts WHERE id=? AND status='active'",
        (broker_account_id,),
    ).fetchone():
        errors["broker_account_id"] = "請選擇有效的券商帳戶。"
    status = 422
    if not errors:
        try:
            fields = trade_forms.normalize_fields(form_values, enhanced == "1")
            fields["security_id"] = security["id"]
            trade_id = trades_repo.create_trade(conn, fields)
        except (replay.OversellError, trades_repo.TradeValidationError) as exc:
            errors = trade_forms.domain_error(str(exc))
            status = 409 if isinstance(exc, replay.OversellError) else 422
        else:
            target = local_return(return_to)
            return RedirectResponse(
                with_query(target, saved=trade_id),
                303,
            )
    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context={
            **_transactions_context(
                conn, dict(parse_qsl(urlsplit(local_return(return_to)).query))
            ),
            "error": "請修正交易欄位後再儲存。",
            "field_errors": errors,
            "form_values": form_values,
            "return_to": local_return(return_to),
        },
        status_code=status,
    )


@router.post("/transactions/import", response_class=HTMLResponse)
def import_trades_form(
    request: Request,
    file: UploadFile,
    conn: sqlite3.Connection = Depends(get_db),
    data_dir=Depends(get_data_dir),
):
    # Sync def, not async — see routes_portfolio.py's preview_trades_csv
    # for why: get_db's connection can't cross the thread boundary an
    # async endpoint's `await` would introduce.
    try:
        content = csv_trades.read_upload_text(file.file)
        preview_id, plan = trade_imports.create_preview(
            conn, data_dir, content, file.filename
        )
    except csv_trades.ImportError_ as exc:
        return _import_failure(request, conn, f"匯入失敗：{exc}")
    return _import_preview(request, preview_id, plan, file.filename)


@router.post("/transactions/import/confirm", response_class=HTMLResponse)
def confirm_import_form(
    request: Request,
    preview_id: str = Form(...),
    plan_digest: str = Form(...),
    include_rows: list[int] = Form([]),
    conn: sqlite3.Connection = Depends(get_db),
    data_dir=Depends(get_data_dir),
):
    try:
        result, backup_filename = trade_imports.confirm_preview(
            conn, data_dir, preview_id, plan_digest, include_rows
        )
    except trade_imports.PreviewNotFoundError as exc:
        return _import_failure(request, conn, str(exc), status_code=404)
    except csv_trades.StalePreviewError as exc:
        try:
            plan = trade_imports.refresh_preview(conn, data_dir, preview_id)
        except (trade_imports.PreviewNotFoundError, csv_trades.ImportError_) as err:
            return _import_failure(request, conn, f"匯入失敗：{err}")
        return _import_preview(request, preview_id, plan, None, notice=str(exc))
    except csv_trades.ImportError_ as exc:
        return _import_failure(request, conn, f"匯入失敗：{exc}")
    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context={
            **_transactions_context(conn),
            "import_success": (
                f"已新增 {result.created} 筆、修改 {result.updated} 筆交易；"
                f"匯入前已備份資料庫（{backup_filename}），可在設定頁還原"
                if backup_filename
                else "沒有勾選任何疑似重複的列，未寫入交易"
            ),
        },
    )


@router.post("/transactions/import/cancel")
def cancel_import_form(
    preview_id: str = Form(""), data_dir=Depends(get_data_dir)
) -> RedirectResponse:
    trade_imports.discard_preview(data_dir, preview_id)
    return RedirectResponse(url="/transactions", status_code=303)


def _import_preview(request, preview_id, plan, filename, notice=None):
    return templates.TemplateResponse(
        request=request,
        name="transactions_import_preview.html",
        context={
            "preview_id": preview_id,
            "plan": plan,
            "filename": filename,
            "notice": notice,
            "field_labels": IMPORT_FIELD_LABELS,
            "event_type_labels": EVENT_TYPE_LABELS,
        },
        status_code=409 if notice else 200,
    )


def _import_failure(request, conn, message, status_code=422):
    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context={**_transactions_context(conn), "error": message},
        status_code=status_code,
    )


@router.get("/transactions/export", response_class=PlainTextResponse)
def export_trades_form(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    filters, errors = trade_query.parse_filters(conn, request.query_params)
    if errors:
        return templates.TemplateResponse(
            request=request,
            name="transactions.html",
            context=_transactions_context(conn, request.query_params),
            status_code=422,
        )
    return PlainTextResponse(
        csv_trades.export_trades_csv(conn, filters),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


def _transactions_context(conn: sqlite3.Connection, params=None) -> dict:
    params = params or {}
    filters, errors = trade_query.parse_filters(conn, params)
    data = trade_query.browse(
        conn,
        filters if not errors else {},
        params.get("page", 1),
        params.get("page_size", 50),
    )
    if errors:
        data.update(transactions=[], total_results=0, total_pages=1, page=1)
    state = {**filters, "page": data["page"], "page_size": data["page_size"]}
    target = with_query("/transactions", **state)
    return {
        **data,
        "filters": filters,
        "filter_errors": errors,
        "filter_accounts": conn.execute(
            "SELECT * FROM broker_accounts ORDER BY name"
        ).fetchall(),
        "broker_accounts": broker_accounts.list_broker_accounts(conn),
        "securities": conn.execute(
            "SELECT id, market, code, name, security_type, status FROM securities "
            "ORDER BY code, (status = 'active') DESC"
        ).fetchall(),
        "event_type_labels": EVENT_TYPE_LABELS,
        "return_to": local_return(params.get("return_to"), target),
        "page_link": lambda page: with_query(target, page=page),
        "new_trade_url": with_query(target, new=1) + "#new-trade",
        "filtered_export_url": with_query("/transactions/export", **filters),
        "edit_url": lambda trade_id: with_query(
            f"/transactions/{trade_id}/edit", return_to=target + "#history"
        ),
        "error": None,
        "field_errors": {},
        "import_success": None,
        "success": None,
        "prefill_notice": None,
        "account_name": "",
        "form_values": {"event_type": "buy", "trade_date": today_taipei()},
    }
