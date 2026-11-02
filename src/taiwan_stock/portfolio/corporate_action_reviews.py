"""Account-specific corporate-action reviews; never writes investment inputs."""

import hashlib
import json
import sqlite3
from collections import defaultdict

from taiwan_stock.infrastructure.db import read_transaction, write_transaction
from taiwan_stock.portfolio import pricing, replay
from taiwan_stock.portfolio.trading_rules import resolve_trading_rules
from taiwan_stock.shared.dates import now_utc_iso, today_taipei

DECISION_LABELS = {
    # Adoption records the user's settled accounting basis, including a
    # reconstructed historical cost; it does not assert broker verification.
    "confirmed": "確認採用此成本",
    "accepted_estimate": "暫時使用估算成本",
    "reopened": "重新開啟",
}
STATE_LABELS = {
    **DECISION_LABELS,
    "confirmed": "已採用成本基準",
    "pending": "待人工確認",
    "stale": "資料變更，待重新確認",
}
MANUAL_TYPES = ("cash_capital_reduction", "other")
HISTORY_PAGE_SIZE = 50
TRADE_INPUTS = (
    "id",
    "event_type",
    "trade_date",
    "quantity",
    "unit_price",
    "executed_amount",
    "commission",
    "transaction_tax",
    "other_fees",
    "is_day_trade",
    "created_at",
)
ACTION_INPUTS = (
    "id",
    "security_id",
    "action_type",
    "ex_date",
    "ratio_numerator",
    "ratio_denominator",
)


class ReviewNotFoundError(ValueError):
    pass


class ReviewValidationError(ValueError):
    pass


class StaleReviewError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _fields(row, names):
    return {name: row[name] for name in names}


class ReviewBook:
    """Batch inputs once for a page; evaluation performs no per-event queries."""

    def __init__(self, conn: sqlite3.Connection):
        with read_transaction(conn):
            self.today = today_taipei()
            self.actions = {
                r["id"]: dict(r)
                for r in conn.execute("SELECT * FROM corporate_actions")
            }
            self.trades = defaultdict(list)
            for row in conn.execute("SELECT * FROM live_trades"):
                self.trades[(row["broker_account_id"], row["security_id"])].append(
                    dict(row)
                )
            self.actions_by_security = defaultdict(list)
            for action in self.actions.values():
                self.actions_by_security[action["security_id"]].append(action)
            self.history = defaultdict(list)
            for row in conn.execute(
                "SELECT * FROM corporate_action_reviews ORDER BY id DESC"
            ):
                self.history[(row["action_id"], row["broker_account_id"])].append(
                    dict(row)
                )
            self._positions = {}
            self._fingerprints = {}

    def position(self, account_id, security_id):
        key = (account_id, security_id)
        if key not in self._positions:
            self._positions[key] = replay.replay_trades(
                self.trades[key], self.actions_by_security[security_id], self.today
            )
        return self._positions[key]

    def fingerprint(self, action_id, account_id, cutoff):
        key = (action_id, account_id, cutoff)
        if key not in self._fingerprints:
            action = self.actions[action_id]
            sid = action["security_id"]
            trades = sorted(
                (
                    _fields(t, TRADE_INPUTS)
                    for t in self.trades[(account_id, sid)]
                    if t["trade_date"] <= cutoff
                ),
                key=lambda t: t["id"],
            )
            actions = sorted(
                (
                    _fields(a, ACTION_INPUTS)
                    for a in self.actions_by_security[sid]
                    if a["action_type"] in ("split", "stock_dividend")
                    and a["ex_date"] <= cutoff
                ),
                key=lambda a: a["id"],
            )
            self._fingerprints[key] = _digest(
                {
                    "account": account_id,
                    "action": _fields(action, ACTION_INPUTS),
                    "trades": trades,
                    "auto_actions": actions,
                }
            )
        return self._fingerprints[key]

    def valid(self, row):
        return row["basis_fingerprint"] == self.fingerprint(
            row["action_id"], row["broker_account_id"], row["as_of_date"]
        )

    def state(self, action_id, account_id):
        history = self.history[(action_id, account_id)]
        if not history:
            return "pending"
        latest = history[0]
        if latest["decision"] == "reopened":
            return "reopened"
        return latest["decision"] if self.valid(latest) else "stale"

    def quality(self, account_id, security_id, position=None):
        """Only unresolved provisional choices carry an estimate warning."""
        position = (
            position if position is not None else self.position(account_id, security_id)
        )
        lot_ids = {lot.trade_id for lot in position.lots if lot.adjusted_quantity > 0}
        estimates = []
        for action in self.actions_by_security[security_id]:
            for row in self.history[(action["id"], account_id)]:
                if row["decision"] == "confirmed" and self.valid(row):
                    break
                if row["decision"] == "accepted_estimate":
                    covered = {
                        lot["trade_id"]
                        for lot in json.loads(row["basis_snapshot"])["lots"]
                    }
                    if lot_ids & covered:
                        estimates.append(
                            {"action_id": action["id"], "note": row["note"]}
                        )
                    break
        return estimates

    def annotate(self, positions):
        for row in positions:
            row["cost_estimates"] = self.quality(
                row["broker_account_id"], row["security_id"], row["position"]
            )


def preview(conn, action_id, account_id, *, book=None):
    with read_transaction(conn):
        book = book or ReviewBook(conn)
        action = book.actions.get(action_id)
        account = conn.execute(
            "SELECT * FROM broker_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if action is None or account is None:
            raise ReviewNotFoundError("找不到股票異動或券商帳戶")
        security = conn.execute(
            "SELECT * FROM securities WHERE id=?", (action["security_id"],)
        ).fetchone()
        position = book.position(account_id, action["security_id"])
        problem = None
        if action["action_type"] not in MANUAL_TYPES:
            problem = "分割與股票股利已自動套用，不適用人工確認"
        elif action["ex_date"] > book.today:
            problem = "股票異動尚未生效，不能確認"
        elif position.remaining_quantity <= 0:
            problem = "此帳戶目前沒有持股，不能確認"
        elif position.remaining_cost is None:
            problem = "成本未知，請先補齊交易成本再確認"
        history = book.history[(action_id, account_id)]
        fingerprint = book.fingerprint(action_id, account_id, book.today)
        snapshot = {
            "lots": [
                {
                    "trade_id": lot.trade_id,
                    "quantity": str(lot.adjusted_quantity),
                    "cost": str(lot.cost) if lot.cost is not None else None,
                }
                for lot in position.lots
                if lot.adjusted_quantity > 0
            ],
            "action": dict(action),
        }
        state = book.state(action_id, account_id)
        rules = resolve_trading_rules(conn, security, book.today)
        fees = rules.fees
        breakeven = pricing.compute_breakeven(
            position, fees, tick_schedule=rules.tick_schedule
        )["overall_breakeven_price"]
        return {
            "action": action,
            "account": dict(account),
            "security": dict(security),
            "position": position,
            "problem": problem,
            "state": state,
            "as_of_date": book.today,
            "basis_fingerprint": fingerprint,
            "basis_snapshot": snapshot,
            "review_token": _digest(
                {
                    "date": book.today,
                    "basis": fingerprint,
                    "latest": history[0]["id"] if history else None,
                }
            ),
            "average_cost": (
                position.remaining_cost / position.remaining_quantity
                if position.remaining_cost is not None
                and position.remaining_quantity > 0
                else None
            ),
            "breakeven_price": breakeven,
            "pricing_warnings": list(rules.warnings),
            "cost_estimates": book.quality(account_id, action["security_id"], position),
            "can_reopen": bool(history) and state != "reopened",
        }


def record(conn, action_id, account_id, decision, note, review_token):
    """Reserve the writer before checking the preview; append exactly one event."""
    if decision not in DECISION_LABELS:
        raise ReviewValidationError("請選擇確認方式")
    note = note.strip()
    if not note or len(note) > 2000:
        raise ReviewValidationError("請填寫依據或重新開啟原因（1–2,000 字）")
    with write_transaction(conn):
        current = preview(conn, action_id, account_id)
        if review_token != current["review_token"]:
            raise StaleReviewError("資料或確認狀態已變更，請重新檢視後再送出")
        if decision == "reopened":
            if not current["can_reopen"]:
                raise ReviewValidationError("目前沒有可重新開啟的確認紀錄")
        elif current["problem"]:
            raise ReviewValidationError(current["problem"])
        position = current["position"]
        conn.execute(
            "INSERT INTO corporate_action_reviews "
            "(action_id,broker_account_id,decision,reviewed_at,as_of_date,quantity,"
            "total_cost,basis_snapshot,basis_fingerprint,note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                action_id,
                account_id,
                decision,
                now_utc_iso(),
                current["as_of_date"],
                str(position.remaining_quantity),
                str(position.remaining_cost)
                if position.remaining_cost is not None
                else None,
                _json(current["basis_snapshot"]),
                current["basis_fingerprint"],
                note,
            ),
        )


def history_page_info(conn, action_id, account_id, page=1):
    total = conn.execute(
        "SELECT count(*) FROM corporate_action_reviews "
        "WHERE action_id=? AND broker_account_id=?",
        (action_id, account_id),
    ).fetchone()[0]
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    return {"page": min(total_pages, max(1, page)), "total_pages": total_pages}


def history_page(conn, action_id, account_id, page=1):
    page = max(1, page)
    rows = conn.execute(
        "SELECT * FROM corporate_action_reviews WHERE action_id=? AND broker_accoun"
        "t_id=? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (action_id, account_id, HISTORY_PAGE_SIZE + 1, (page - 1) * HISTORY_PAGE_SIZE),
    ).fetchall()
    return [dict(r) for r in rows[:HISTORY_PAGE_SIZE]], len(rows) > HISTORY_PAGE_SIZE
