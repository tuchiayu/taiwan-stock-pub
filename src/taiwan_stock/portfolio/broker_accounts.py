"""Broker account CRUD (design 002 §4.6.2 `broker_accounts`)."""

import sqlite3

from taiwan_stock.shared.ids import new_id


def list_broker_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM broker_accounts WHERE status = 'active' ORDER BY name"
    ).fetchall()


def create_broker_account(conn: sqlite3.Connection, fields: dict) -> str:
    account_id = new_id()
    conn.execute(
        """
        INSERT INTO broker_accounts
            (id, name, dividend_bank_name, remittance_fee_note, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            account_id,
            fields["name"],
            fields.get("dividend_bank_name"),
            fields.get("remittance_fee_note"),
            fields.get("status") or "active",
        ),
    )
    return account_id
