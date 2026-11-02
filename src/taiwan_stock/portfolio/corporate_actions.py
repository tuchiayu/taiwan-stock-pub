"""Corporate action entry (design 002 §4.11).

Split/stock-dividend ratios are stored here and auto-applied by the
replay engine at query time (replay.py's `_apply_action`) — no separate
"confirm" step, unlike trades. `cash_capital_reduction`/`other` are
recorded but never auto-applied. Their held-account warnings can be
resolved with an account-specific review (design 024), which records
the user's decision without modifying trades or action source data.
"""

import sqlite3
from datetime import date

from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.shared.ids import new_id

AUTO_APPLIED_TYPES = ("split", "stock_dividend")
ALL_ACTION_TYPES = ("split", "stock_dividend", "cash_capital_reduction", "other")


class DuplicateActionError(Exception):
    pass


class CorporateActionValidationError(ValueError):
    """Invalid corporate action input that must be rejected before persistence."""


def compute_split_ratio(from_shares: int, to_shares: int) -> tuple[int, int]:
    """ "1 股拆 4 股" -> from_shares=1, to_shares=4 -> ratio 4/1."""
    return to_shares, from_shares


def compute_stock_dividend_ratio(per_thousand_shares: int) -> tuple[int, int]:
    """公告「每千股配 50 股」-> ratio 1050/1000 (not reduced to 21/20 —
    the Decimal value is identical either way; reducing is cosmetic
    only, per §4.11 point 1)."""
    return 1000 + per_thousand_shares, 1000


def validate_auto_applied_ratio(fields: dict) -> None:
    """Split/stock-dividend must always increase share count (§4.11
    point 1) — on the ex-date the account's whole-share holding becomes
    `floor(holding * ratio)` (replay.py's `_apply_action`), which for a
    ratio >= 1 is never below the holding before it, so it can only
    ever make an existing sell *easier* to cover,
    never impossible; there is no oversell scenario to guard against
    here as long as the ratio itself is sane. A ratio that isn't
    reflects either bad manual input (from/to share counts entered
    backwards) or bad source data, and would otherwise only surface
    much later as an unexplained OversellError when some page tries to
    read the position back.
    """
    numerator = fields.get("ratio_numerator")
    denominator = fields.get("ratio_denominator")
    if (
        not isinstance(numerator, int)
        or not isinstance(denominator, int)
        or numerator <= 0
        or denominator <= 0
    ):
        raise CorporateActionValidationError("股票異動比例必須是正整數")
    if numerator <= denominator:
        raise CorporateActionValidationError(
            "分割／股票股利會讓股數增加，調整後股數必須大於調整前股數，請確認比例是否填反"
        )


def create_corporate_action(conn: sqlite3.Connection, fields: dict) -> str:
    action_id = new_id()
    security_id = fields.get("security_id")
    action_type = fields.get("action_type")

    if action_type not in ALL_ACTION_TYPES:
        raise CorporateActionValidationError(f"不支援的股票異動類型「{action_type}」")

    ex_date = (fields.get("ex_date") or "").strip()
    try:
        if (
            not isinstance(ex_date, str)
            or date.fromisoformat(ex_date).isoformat() != ex_date
        ):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise CorporateActionValidationError(
            "除權息日期必須是有效日期（YYYY-MM-DD）"
        ) from exc

    if not conn.execute(
        "SELECT 1 FROM securities WHERE id = ?", (security_id,)
    ).fetchone():
        raise CorporateActionValidationError("找不到此商品")

    if action_type in AUTO_APPLIED_TYPES:
        validate_auto_applied_ratio(fields)
    else:
        if (
            fields.get("ratio_numerator") is not None
            or fields.get("ratio_denominator") is not None
        ):
            raise CorporateActionValidationError(
                f"{action_type} 異動不可包含分割／配股比例"
            )

    try:
        conn.execute(
            """
            INSERT INTO corporate_actions
                (id, security_id, action_type, ratio_numerator,
                 ratio_denominator, ex_date, note, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')
            """,
            (
                action_id,
                security_id,
                action_type,
                fields.get("ratio_numerator"),
                fields.get("ratio_denominator"),
                ex_date,
                fields.get("note"),
            ),
        )
    except sqlite3.IntegrityError as exc:
        # Only translate the UNIQUE-constraint case (a genuine
        # duplicate); a CHECK-constraint failure (e.g. a non-positive
        # ratio) is a different problem and must not be mislabeled as
        # "this already exists".
        if "UNIQUE constraint failed" in str(exc):
            raise DuplicateActionError(
                "這檔商品在同一天已經有相同類型的股票異動紀錄"
            ) from exc
        raise CorporateActionValidationError(f"股票異動資料不符合限制：{exc}") from exc
    return action_id


def list_corporate_actions(
    conn: sqlite3.Connection, security_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM corporate_actions WHERE security_id = ? ORDER BY ex_date DESC",
        (security_id,),
    ).fetchall()


def list_unhandled_actions_for_held_securities(
    conn: sqlite3.Connection,
) -> list[dict]:
    """`cash_capital_reduction`/`other` actions on securities that have
    at least one trade and a positive current position — the data
    behind the homepage's "待處理股票異動" notice.
    """
    with read_transaction(conn):
        from taiwan_stock.portfolio.corporate_action_reviews import ReviewBook
        from taiwan_stock.portfolio.distribution_entitlement import (
            history_problem,
            quoted_in_foreign_currency,
        )

        book = ReviewBook(conn)

        rows = conn.execute(
            """
            SELECT DISTINCT ca.id, ca.action_type, ca.ex_date, ca.note,
                   s.id AS security_id, s.code AS security_code,
                   s.name AS security_name,
                   t.broker_account_id, b.name AS broker_account_name
            FROM corporate_actions ca
            JOIN securities s ON s.id = ca.security_id
            JOIN live_trades t ON t.security_id = ca.security_id
            JOIN broker_accounts b ON b.id = t.broker_account_id
            WHERE ca.action_type NOT IN ('split', 'stock_dividend')
            ORDER BY ca.ex_date DESC
            """
        ).fetchall()

        unhandled = []
        seen = set()
        for row in rows:
            key = (row["id"], row["broker_account_id"])
            if key in seen:
                continue
            seen.add(key)
            # Reported by distribution_entitlement.history_warnings (#115 #8).
            pair = (row["broker_account_id"], row["security_id"])
            if history_problem(
                book.trades[pair],
                book.actions_by_security[pair[1]],
                foreign_currency=quoted_in_foreign_currency(conn, pair[1]),
            ):
                continue
            position = book.position(*pair)
            if position.remaining_quantity <= 0:
                continue
            state = book.state(row["id"], row["broker_account_id"])
            if state in ("confirmed", "accepted_estimate"):
                continue
            unhandled.append({**dict(row), "review_state": state})
        return unhandled
