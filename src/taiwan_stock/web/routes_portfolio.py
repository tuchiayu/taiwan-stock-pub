"""`/api/v1/broker-accounts`, `/api/v1/trades`, `/api/v1/positions`,
`/api/v1/sale-simulations`."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import (
    broker_accounts,
    csv_trades,
    distribution_entitlement,
    pricing,
    replay,
    simulate,
    trade_imports,
    watchlist,
)
from taiwan_stock.portfolio import trades as trades_repo
from taiwan_stock.portfolio.trading_rules import resolve_trading_rules
from taiwan_stock.shared.dates import today_taipei
from taiwan_stock.web.deps import get_data_dir, get_db

router = APIRouter(prefix="/api/v1")


class BrokerAccountIn(BaseModel):
    name: str
    dividend_bank_name: str | None = None
    remittance_fee_note: str | None = None


class TradeIn(BaseModel):
    broker_account_id: str
    security_id: str
    event_type: str
    trade_date: str
    quantity: int
    unit_price: str | None = None
    executed_amount: str | None = None
    commission: str | None = None
    transaction_tax: str | None = None
    other_fees: str | None = None
    note: str | None = None
    is_day_trade: bool | None = None


class SaleSimulationIn(BaseModel):
    broker_account_id: str
    security_id: str
    quantity: int
    price: str | None = None


class TradeUpdate(BaseModel):
    recalculate: bool = Field(
        default=False,
        description=(
            "買賣交易依合併後的股數、單價及費率重算未於本次傳入的成交金額、"
            "手續費及賣出稅。明確非 null 值優先；舊人工費用若未重傳也會重算。"
            "other_fees 未傳時保留；期初持股或沒有單價的交易不接受重算。"
        ),
    )
    event_type: str | None = None
    trade_date: str | None = None
    quantity: int | None = None
    unit_price: str | None = None
    executed_amount: str | None = None
    commission: str | None = None
    transaction_tax: str | None = None
    other_fees: str | None = None
    note: str | None = None
    is_day_trade: bool | None = None


@router.get("/broker-accounts")
def list_broker_accounts(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = broker_accounts.list_broker_accounts(conn)
    return {"items": [dict(row) for row in rows]}


@router.post("/broker-accounts", status_code=201)
def create_broker_account(
    body: BrokerAccountIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    data = body.model_dump()
    data["name"] = data["name"].strip()
    if not data["name"]:
        raise HTTPException(status_code=422, detail="帳戶名稱不可為空")
    account_id = broker_accounts.create_broker_account(conn, data)
    return {"id": account_id}


@router.post("/trades", status_code=201)
def create_trade(body: TradeIn, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        trade_id = trades_repo.create_trade(conn, body.model_dump())
    except trades_repo.TradeValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except replay.OversellError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": trade_id}


@router.get("/trades")
def list_trades(
    broker_account_id: str,
    security_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    rows = trades_repo.fetch_trades(conn, broker_account_id, security_id)
    return {"items": [dict(row) for row in rows]}


@router.patch("/trades/{trade_id}")
def update_trade(
    trade_id: str, body: TradeUpdate, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    fields = body.model_dump(exclude_unset=True)
    recalculate = fields.pop("recalculate", False)
    try:
        trades_repo.update_trade(conn, trade_id, fields, recalculate=recalculate)
    except trades_repo.TradeValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except replay.OversellError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": trade_id}


@router.post("/trades/{trade_id}/void")
def void_trade(trade_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return _set_voided(conn, trade_id, trades_repo.void_trade)


@router.post("/trades/{trade_id}/unvoid")
def unvoid_trade(trade_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return _set_voided(conn, trade_id, trades_repo.unvoid_trade)


def _set_voided(conn: sqlite3.Connection, trade_id: str, action) -> dict:
    try:
        action(conn, trade_id)
    except replay.OversellError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    row = conn.execute(
        "SELECT voided_at FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()
    return {"id": trade_id, "voided_at": row["voided_at"]}


def _get_security_or_404(conn: sqlite3.Connection, security_id: str) -> sqlite3.Row:
    security = conn.execute(
        "SELECT * FROM securities WHERE id = ?", (security_id,)
    ).fetchone()
    if security is None:
        raise HTTPException(status_code=404, detail=f"security {security_id} not found")
    return security


@router.get("/positions")
def get_position(
    broker_account_id: str,
    security_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    with read_transaction(conn):
        security = _get_security_or_404(conn, security_id)
        rule_date = today_taipei()
        if problem := distribution_entitlement.stored_problem(
            conn, broker_account_id, security_id
        ):
            raise HTTPException(
                status_code=409, detail=simulate.PROBLEM_MESSAGES[problem["reason"]]
            )
        try:
            position = trades_repo.compute_position(
                conn, broker_account_id, security_id, as_of_date=rule_date
            )
        except replay.OversellError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        rules = resolve_trading_rules(conn, security, rule_date)
        fee_schedule = rules.fees
        breakeven = pricing.compute_breakeven(
            position, fee_schedule, tick_schedule=rules.tick_schedule
        )
        breakeven_by_lot = {
            lot["trade_id"]: lot["breakeven_price"] for lot in breakeven["lots"]
        }

        return {
            "pricing_warnings": list(rules.warnings),
            "pricing_rule_id": rules.rule_id,
            "pricing_rule_date": rules.rule_date,
            "remaining_quantity": str(position.remaining_quantity),
            "remaining_cost": (
                str(position.remaining_cost)
                if position.remaining_cost is not None
                else None
            ),
            "breakeven_price": (
                str(breakeven["overall_breakeven_price"])
                if breakeven["overall_breakeven_price"] is not None
                else None
            ),
            "lots": [
                {
                    "trade_id": lot.trade_id,
                    "trade_date": lot.trade_date,
                    "original_remaining_quantity": str(lot.original_remaining_quantity),
                    "adjusted_quantity": str(lot.adjusted_quantity),
                    "cost": str(lot.cost) if lot.cost is not None else None,
                    "breakeven_price": (
                        str(breakeven_by_lot[lot.trade_id])
                        if breakeven_by_lot.get(lot.trade_id) is not None
                        else None
                    ),
                }
                for lot in position.lots
            ],
        }


@router.post("/sale-simulations")
def create_sale_simulation(
    body: SaleSimulationIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Does not write to `trades` — see portfolio/simulate.py."""
    with read_transaction(conn):
        security = _get_security_or_404(conn, body.security_id)
        trades = trades_repo.fetch_trades(
            conn, body.broker_account_id, body.security_id
        )
        actions = trades_repo.fetch_corporate_actions(conn, body.security_id)
        rule_date = today_taipei()
        rules = resolve_trading_rules(conn, security, rule_date)
        fee_schedule = rules.fees

        try:
            result = simulate.simulate_partial_sale(
                trades,
                actions,
                body.quantity,
                fee_schedule,
                as_of_date=rule_date,
                tick_schedule=rules.tick_schedule,
                price=body.price,
            )
        except simulate.SimulationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {
            "pricing_warnings": list(rules.warnings),
            "pricing_rule_id": rules.rule_id,
            "pricing_rule_date": rules.rule_date,
            "quantity": result.quantity,
            "cost_consumed": str(result.cost_consumed)
            if result.cost_consumed is not None
            else None,
            "breakeven_price": (
                str(result.breakeven_price)
                if result.breakeven_price is not None
                else None
            ),
            "price": str(result.price) if result.price is not None else None,
            "net_proceeds": str(result.net_proceeds)
            if result.net_proceeds is not None
            else None,
            "realized_pnl": str(result.realized_pnl)
            if result.realized_pnl is not None
            else None,
        }


@router.post("/trade-imports/previews")
def preview_trades_csv(
    file: UploadFile,
    conn: sqlite3.Connection = Depends(get_db),
    data_dir=Depends(get_data_dir),
) -> dict:
    # Sync def, not async — get_db's sync generator runs in a
    # threadpool thread, and sqlite3.Connection objects reject use
    # from a different thread than the one that created them. An
    # async endpoint here would run on the event loop thread instead,
    # after the `await` for the file read, crossing that boundary.
    try:
        content = csv_trades.read_upload_text(file.file)
        preview_id, plan = trade_imports.create_preview(
            conn, data_dir, content, file.filename
        )
    except csv_trades.ImportError_ as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "preview_id": preview_id,
        "plan_digest": plan.digest,
        "summary": plan.summary,
        "rows": [
            {
                "row_number": row.row_number,
                "status": row.status,
                "trade_id": row.trade_id,
                "changes": {
                    field: [_json_value(old), _json_value(new)]
                    for field, (old, new) in row.changes.items()
                },
            }
            for row in plan.rows
        ],
    }


class TradeImportConfirmIn(BaseModel):
    preview_id: str
    plan_digest: str
    include_rows: list[int] = Field(default_factory=list)


@router.post("/trade-imports")
def confirm_trades_csv(
    payload: TradeImportConfirmIn,
    conn: sqlite3.Connection = Depends(get_db),
    data_dir=Depends(get_data_dir),
) -> dict:
    try:
        result, backup_filename = trade_imports.confirm_preview(
            conn,
            data_dir,
            payload.preview_id,
            payload.plan_digest,
            payload.include_rows,
        )
    except trade_imports.PreviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except csv_trades.StalePreviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except csv_trades.ImportError_ as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "created": result.created,
        "updated": result.updated,
        "backup_filename": backup_filename,
    }


def _json_value(value):
    """Amounts already travel as strings (design 002's API rule); the
    day-trade flag is stored as 0/1 but prepared as a bool."""
    return int(value) if isinstance(value, bool) else value


@router.get("/exports/trades", response_class=PlainTextResponse)
def export_trades(conn: sqlite3.Connection = Depends(get_db)) -> PlainTextResponse:
    content = csv_trades.export_trades_csv(conn)
    return PlainTextResponse(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


class WatchlistItemIn(BaseModel):
    note: str | None = None
    target_price: str | None = None


@router.get("/watchlist")
def get_watchlist(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """List all tracked securities in the watchlist (design 002 §4.12)."""
    return watchlist.list_watchlist(conn)


@router.post("/watchlist/{security_id}")
def add_watchlist_item(
    security_id: str,
    body: WatchlistItemIn | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Add or update a tracked security in the watchlist."""
    note = body.note if body else None
    try:
        target_price = watchlist.parse_target_price(body.target_price if body else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        watchlist.add_to_watchlist(
            conn, security_id, note=note, target_price=target_price
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "security_id": security_id}


@router.delete("/watchlist/{security_id}")
def delete_watchlist_item(
    security_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Remove a tracked security from the watchlist."""
    removed = watchlist.remove_from_watchlist(conn, security_id)
    return {"status": "ok", "removed": removed}
