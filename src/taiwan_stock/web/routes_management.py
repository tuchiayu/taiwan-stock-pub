"""Account, preferences, receipt and transaction editing interfaces."""

import sqlite3
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse

from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import (
    broker_accounts,
    income_calendar,
    settings,
    trades,
)
from taiwan_stock.portfolio.income_scope import api_filter
from taiwan_stock.web import trade_forms
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.pages_settings import settings_context
from taiwan_stock.web.presentation import local_return, with_query
from taiwan_stock.web.templates import templates

router = APIRouter()


def encode(value):
    return jsonable_encoder(value, custom_encoder={Decimal: str})


@router.get("/api/v1/settings")
def get_settings(conn: sqlite3.Connection = Depends(get_db)):
    return settings.read_settings(conn)


@router.patch("/api/v1/settings")
def update_settings(body: dict, conn: sqlite3.Connection = Depends(get_db)):
    try:
        return settings.save_settings(conn, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/settings/preferences")
def preferences_form(
    request: Request,
    commission_rate: str = Form(""),
    commission_discount: str = Form(""),
    min_commission: str = Form(""),
    tax_rate_stock: str = Form(""),
    tax_rate_etf: str = Form(""),
    form_format: str = Form(""),
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
):
    values = {
        "commission_rate": commission_rate,
        "commission_discount": commission_discount,
        "min_commission": min_commission,
        "tax_rate_stock": tax_rate_stock,
        "tax_rate_etf": tax_rate_etf,
    }
    cleaned, errors = {}, {}
    for key, raw in values.items():
        try:
            value = Decimal(raw)
            if form_format == "display":
                value /= (
                    10
                    if key == "commission_discount"
                    else 1
                    if key == "min_commission"
                    else 100
                )
            limit = value <= 1 if key == "commission_discount" else value < 1
            if (
                not value.is_finite()
                or value < 0
                or (key != "min_commission" and not limit)
            ):
                raise ValueError
            cleaned[key] = str(value)
        except (InvalidOperation, ValueError):
            errors[key] = "請填入有效範圍內的非負數值。"
    if errors:
        displayed = values
        if form_format != "display":
            displayed = dict(settings_context(conn, data_dir)["display_preferences"])
            for key, raw in values.items():
                try:
                    displayed[key] = str(
                        Decimal(raw)
                        * (
                            10
                            if key == "commission_discount"
                            else 1
                            if key == "min_commission"
                            else 100
                        )
                    )
                except InvalidOperation:
                    displayed[key] = raw
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                **settings_context(conn, data_dir),
                "display_preferences": displayed,
                "settings_error": "請修正費用設定後再儲存。",
                "field_errors": errors,
                "preferences_failed": True,
            },
            status_code=422,
        )
    settings.save_settings(conn, cleaned)
    return RedirectResponse(
        "/settings?saved=preferences#preferences"
        if form_format == "display"
        else "/settings",
        303,
    )


def edit_account(conn, account_id, fields):
    current = conn.execute(
        "SELECT * FROM broker_accounts WHERE id=?", (account_id,)
    ).fetchone()
    if current is None:
        raise HTTPException(404, "找不到券商帳戶")
    values = dict(current)
    allowed = {"name", "dividend_bank_name", "remittance_fee_note", "status"}
    values.update({k: v for k, v in fields.items() if k in allowed})
    if isinstance(values["name"], str):
        values["name"] = values["name"].strip()
    if not values["name"] or values["status"] not in ("active", "disabled"):
        raise HTTPException(422, "帳戶名稱或狀態無效")
    conn.execute(
        "UPDATE broker_accounts SET name=:name, "
        "dividend_bank_name=:dividend_bank_name, "
        "remittance_fee_note=:remittance_fee_note, status=:status WHERE id=:id",
        values,
    )
    return {"id": account_id}


@router.patch("/api/v1/broker-accounts/{account_id}")
def patch_account(
    account_id: str, body: dict, conn: sqlite3.Connection = Depends(get_db)
):
    return edit_account(conn, account_id, body)


@router.post("/settings/accounts/{account_id}")
def account_form(
    account_id: str,
    request: Request,
    name: str = Form(""),
    dividend_bank_name: str = Form(""),
    remittance_fee_note: str = Form(""),
    status: str = Form("active"),
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
):
    values = {
        "name": name,
        "dividend_bank_name": dividend_bank_name,
        "remittance_fee_note": remittance_fee_note,
        "status": status,
    }
    errors = {}
    if not name.strip():
        errors["name"] = "請填帳戶名稱。"
    if status not in ("active", "disabled"):
        errors["status"] = "請選擇有效狀態。"
    if (
        account_id != "new"
        and not conn.execute(
            "SELECT 1 FROM broker_accounts WHERE id=?", (account_id,)
        ).fetchone()
    ):
        raise HTTPException(404, "找不到券商帳戶")
    if errors:
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                **settings_context(conn, data_dir),
                "settings_error": "請修正帳戶欄位後再儲存。",
                "account_values": values,
                "editing_account": account_id,
                "field_errors": errors,
            },
            status_code=422,
        )
    values["name"] = name.strip()
    for key in ("dividend_bank_name", "remittance_fee_note"):
        values[key] = values[key] or None
    if account_id == "new":
        broker_accounts.create_broker_account(conn, values)
    else:
        edit_account(conn, account_id, values)
    return RedirectResponse("/settings?saved=account#accounts", 303)


def _edit_context(conn, trade_id):
    row = conn.execute(
        "SELECT t.*,s.code AS security_code,s.name AS security_name,"
        "s.security_type,s.market,b.name AS broker_account_name FROM trades t "
        "JOIN securities s ON s.id=t.security_id "
        "JOIN broker_accounts b ON b.id=t.broker_account_id WHERE t.id=?",
        (trade_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "找不到交易")
    return {
        "trade": dict(row),
        "form_values": dict(row),
        "error": None,
        "field_errors": {},
    }


@router.get("/transactions/{trade_id}/edit")
def edit_trade_page(trade_id: str, request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="trade_edit.html",
        context={
            **_edit_context(conn, trade_id),
            "return_to": local_return(request.query_params.get("return_to")),
        },
    )


@router.post("/transactions/{trade_id}/edit")
def edit_trade_form(
    trade_id: str,
    request: Request,
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
    conn=Depends(get_db),
):
    context = _edit_context(conn, trade_id)
    values = {
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
    errors = trade_forms.validate(values)
    if not errors:
        try:
            fields = trade_forms.normalize_fields(values, enhanced == "1")
            trades.update_trade(conn, trade_id, fields)
        except (ValueError, trades.replay.OversellError) as exc:
            errors = trade_forms.domain_error(str(exc))
        else:
            target = local_return(return_to)
            return RedirectResponse(
                with_query(target, saved=trade_id),
                303,
            )
    return templates.TemplateResponse(
        request=request,
        name="trade_edit.html",
        context={
            **context,
            "form_values": {**context["trade"], **values},
            "error": "請修正交易欄位後再儲存。",
            "field_errors": errors,
            "return_to": local_return(return_to),
        },
        status_code=422,
    )


@router.post("/transactions/{trade_id}/void")
def void_trade_form(
    trade_id: str,
    request: Request,
    return_to: str = Form("/transactions"),
    conn=Depends(get_db),
):
    return _set_voided_form(request, conn, trade_id, return_to, trades.void_trade)


@router.post("/transactions/{trade_id}/unvoid")
def unvoid_trade_form(
    trade_id: str,
    request: Request,
    return_to: str = Form("/transactions"),
    conn=Depends(get_db),
):
    return _set_voided_form(request, conn, trade_id, return_to, trades.unvoid_trade)


def _set_voided_form(request, conn, trade_id, return_to, action):
    context = _edit_context(conn, trade_id)
    try:
        action(conn, trade_id)
    except trades.replay.OversellError as exc:
        return templates.TemplateResponse(
            request=request,
            name="trade_edit.html",
            context={
                **context,
                "error": f"無法變更作廢狀態：{exc}",
                "return_to": local_return(return_to),
            },
            status_code=409,
        )
    return RedirectResponse(with_query(local_return(return_to), saved=trade_id), 303)


_V2_FIELD_NAMES = {
    "distribution_id": "distributionId",
    "broker_account_id": "brokerAccountId",
    "broker_account_name": "brokerAccountName",
    "security_id": "securityId",
    "security_code": "securityCode",
    "security_name": "securityName",
    "ex_dividend_date": "exDividendDate",
    "pay_date": "payDate",
    "amount_per_share": "amountPerShare",
    "amount_status": "amountStatus",
    "entitled_quantity": "entitledQuantity",
    "expected_gross": "expectedGross",
    "is_future": "isFuture",
    "bank": "bank",
    "status": "status",
    "reconciliation_status": "reconciliationStatus",
    "reconciliation_note": "reconciliationNote",
}


@router.get("/api/v1/income-calendar")
def calendar_api(
    month: str | None = None,
    preview: bool = False,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Design 009 §4.7: frozen for backward compatibility — the
    historical-frequency preview and manual-receipt tracking this
    endpoint used to expose are gone from the underlying calculation,
    not silently reinterpreted. `preview=true` now explicitly errors
    rather than returning different data than before; `received`/
    `received_net_amount` are kept in the shape but always report
    "not applicable" since the calendar no longer reads
    `dividend_receipts`. New integrations should use
    `/api/v2/income-calendar`.
    """
    with read_transaction(conn):
        if preview:
            raise HTTPException(
                410, "preview 推估功能已下線，請改用 /api/v2/income-calendar"
            )
        try:
            result = income_calendar.calendar_result(conn, month=month)
            if not result["data_complete"]:
                raise ValueError(
                    "交易歷史不一致，月曆資料不完整；請使用 v2 查看排除部位並修正交易"
                )
            entries = result["items"]
        except (ValueError, trades.replay.OversellError) as exc:
            raise HTTPException(422, str(exc)) from exc
        frozen = [
            {**e, "received": False, "received_net_amount": None} for e in entries
        ]
        return {"items": encode(frozen)}


@router.get("/api/v2/income-calendar")
def calendar_api_v2(
    month: str | None = None,
    scope: str = "all",
    account_id: str | None = None,
    bank: str | None = None,
    bank_unset: bool = False,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Filter by one account or receiving bank; omitted scope returns all."""
    with read_transaction(conn):
        try:
            filter_value = api_filter(
                scope=scope, account_id=account_id, bank=bank, bank_unset=bank_unset
            )
            result = income_calendar.calendar_result(
                conn, month=month, filter_value=filter_value
            )
            entries = result["items"]
        except (ValueError, trades.replay.OversellError) as exc:
            raise HTTPException(422, str(exc)) from exc
        items = [
            {
                _V2_FIELD_NAMES[key]: value
                for key, value in e.items()
                if key in _V2_FIELD_NAMES
            }
            for e in entries
        ]

        def camel_fields(row):
            return {
                key.split("_")[0]
                + "".join(part.title() for part in key.split("_")[1:]): value
                for key, value in row.items()
            }

        return {
            "items": encode(items),
            "dataComplete": result["data_complete"],
            "excludedPositions": [
                camel_fields(w) for w in result["excluded_positions"]
            ],
            "warnings": [camel_fields(w) for w in result["warnings"]],
        }


@router.get("/api/v1/dividend-receipts")
def receipts_api(conn: sqlite3.Connection = Depends(get_db)):
    """Design 009 §4.7: read-only compatibility access to historical
    manual receipts — kept for existing data, not a calculation source
    for the new calendar/homepage (see `distribution_entitlement`)."""
    return {
        "items": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM dividend_receipts ORDER BY received_date DESC"
            )
        ]
    }


@router.post("/api/v1/dividend-receipts", status_code=410)
def add_receipt():
    """Design 009 §4.7: retired — manual receipt entry is no longer part
    of the income calendar/homepage calculation. Existing rows are
    preserved (see `receipts_api`); no new ones are accepted."""
    raise HTTPException(410, "實收登記已下線，不再接受新增；歷史資料仍可查詢")
