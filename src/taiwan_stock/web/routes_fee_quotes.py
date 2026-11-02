"""Read-only Decimal quotes used by both create and edit forms."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import trades
from taiwan_stock.portfolio.trading_rules import resolve_trading_rules
from taiwan_stock.web.deps import get_db
from taiwan_stock.web.routes_portfolio import TradeIn

router = APIRouter()


class FeeQuoteIn(TradeIn):
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid")


@router.post("/api/v2/trade-fee-quotes")
def quote(body: FeeQuoteIn, conn: sqlite3.Connection = Depends(get_db)):
    with read_transaction(conn):
        try:
            candidate = trades.prepare(conn, body.model_dump())
            security = conn.execute(
                "SELECT * FROM securities WHERE id=?", (candidate["security_id"],)
            ).fetchone()
            rules = resolve_trading_rules(conn, security, candidate["trade_date"])
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        total = trades._trade_total_amount(candidate)
        return {
            "executedAmount": candidate["executed_amount"],
            "commission": candidate["commission"],
            "transactionTax": candidate["transaction_tax"],
            "grossAmount": candidate["executed_amount"],
            "netAmount": str(total) if total is not None else None,
            "ruleId": rules.rule_id,
            "ruleDate": rules.rule_date,
            "warnings": list(rules.warnings),
            "notices": list(rules.notices),
        }
