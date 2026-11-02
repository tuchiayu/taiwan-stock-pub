"""Validated preferences shared by HTML and JSON settings endpoints."""

import sqlite3
from decimal import Decimal, InvalidOperation

from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.portfolio import limits

MAX_MIN_COMMISSION = Decimal(10**6)

DEFAULTS = {
    "commission_rate": "0.001425",
    "commission_discount": "1",
    "min_commission": "20",
    "tax_rate_stock": "0.003",
    "tax_rate_etf": "0.001",
    "quantity_display": "shares",
}


def read_settings(conn: sqlite3.Connection) -> dict:
    return {
        **DEFAULTS,
        **{
            r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM app_settings")
            if r["key"] in DEFAULTS
        },
    }


def save_settings(conn: sqlite3.Connection, values: dict) -> dict:
    cleaned = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"未知設定：{key}")
        value = str(value)
        if key == "quantity_display":
            valid = value in ("shares", "lots")
        else:
            try:
                number = Decimal(value)
                # Design 060: bounded like trade amounts.
                valid = (
                    number.is_finite()
                    and number >= 0
                    and number.as_tuple().exponent >= -limits.MAX_DECIMAL_PLACES
                )
                if key == "min_commission":
                    valid = valid and number <= MAX_MIN_COMMISSION
                if key in (
                    "commission_rate",
                    "commission_discount",
                    "tax_rate_stock",
                    "tax_rate_etf",
                ):
                    valid = (
                        valid and number < 1
                        if key != "commission_discount"
                        else valid and number <= 1
                    )
                if valid:
                    value = format(number, "f")
            except (InvalidOperation, ValueError):
                valid = False
        if not valid:
            raise ValueError(f"{key} 設定值無效")
        cleaned[key] = value
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO app_settings(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            cleaned.items(),
        )
        return read_settings(conn)
