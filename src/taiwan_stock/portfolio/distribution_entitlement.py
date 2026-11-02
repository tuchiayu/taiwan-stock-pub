"""Per-distribution entitlement, shared by the income calendar and the
homepage's auto announced-dividend break-even (design 009 §4.6).

Kept independent of `trades.list_position_overview` — and doing its
own low-level `trades`/`corporate_actions` reads plus `replay` call
rather than going through `taiwan_stock.portfolio.trades` — so that
`trades.py` can use this module without a `trades` <->
`income_calendar` import cycle now that both need the same
entitlement computation. `trades.py` imports `compute_position` from
here (and keeps re-exporting it under its own name) rather than
defining its own copy.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from taiwan_stock.catalog import share_classes
from taiwan_stock.catalog.distribution_rows import countable
from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import limits, replay
from taiwan_stock.shared.dates import today_taipei


def fetch_trades(
    conn: sqlite3.Connection, broker_account_id: str, security_id: str
) -> list[sqlite3.Row]:
    # Voided trades never take part in a position (issue #115 #10).
    return conn.execute(
        "SELECT * FROM live_trades WHERE broker_account_id = ? AND security_id = ?",
        (broker_account_id, security_id),
    ).fetchall()


def fetch_corporate_actions(
    conn: sqlite3.Connection, security_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM corporate_actions WHERE security_id = ?", (security_id,)
    ).fetchall()


def oversell_error(trades, actions) -> replay.OversellError | None:
    """Replaying the whole history checks every earlier as-of date too."""
    if not trades:
        return None
    try:
        replay.replay_trades(
            trades, actions, as_of_date=max(t["trade_date"] for t in trades)
        )
    except replay.OversellError as exc:
        return exc
    return None


def out_of_range_trade(trades):
    """The first stored trade whose numbers exceed design 060's bounds."""
    return next((t for t in trades if limits.trade_problem(t)), None)


def history_problem(trades, actions, *, foreign_currency=False) -> dict | None:
    """Why a history can't be counted: a foreign-currency listing (design
    063), a trade outside the bounds (checked before replay, which could
    overflow) or an oversell."""
    problem = _unreplayable(trades, foreign_currency=foreign_currency)
    if problem is not None:
        return problem
    exc = oversell_error(trades, actions)
    if exc is not None:
        return {
            "reason": "oversell",
            "trade_id": exc.trade_id,
            "detail": str(exc),
            "shortfall": str(exc.shortfall),
        }
    return None


def quoted_in_foreign_currency(conn: sqlite3.Connection, security_id: str) -> bool:
    row = conn.execute(
        "SELECT s.name, p.fund_type FROM securities s "
        "LEFT JOIN etf_profiles p ON p.security_id = s.id WHERE s.id = ?",
        (security_id,),
    ).fetchone()
    return row is not None and share_classes.is_foreign_currency(
        row["fund_type"], row["name"]
    )


def stored_problem(
    conn: sqlite3.Connection, broker_account_id: str, security_id: str
) -> dict | None:
    """`history_problem`'s reasons that need no replay, for one holding read
    on its own (design 074); the caller replays next and reports an
    oversell its own way."""
    return _unreplayable(
        fetch_trades(conn, broker_account_id, security_id),
        foreign_currency=quoted_in_foreign_currency(conn, security_id),
    )


def _unreplayable(trades, *, foreign_currency) -> dict | None:
    if foreign_currency and trades:
        first = min(trades, key=lambda t: (t["trade_date"], t["created_at"]))
        return {
            "reason": "foreign_currency",
            "trade_id": first["id"],
            "detail": "外幣計價",
            "shortfall": None,
        }
    bad = out_of_range_trade(trades)
    if bad is not None:
        return {
            "reason": "out_of_range",
            "trade_id": bad["id"],
            "detail": f"{limits.trade_problem(bad)} 超出可計算範圍",
            "shortfall": None,
        }
    return None


class HistoryBook:
    """Materialized live histories and one diagnosis shared by an aggregate."""

    def __init__(self, conn):
        with read_transaction(conn):
            self.pairs = conn.execute(
                "SELECT DISTINCT t.broker_account_id, t.security_id, "
                "b.name AS broker_account_name, s.code AS security_code, "
                "s.name AS security_name, s.market, p.fund_type FROM live_trades t "
                "JOIN broker_accounts b ON b.id=t.broker_account_id "
                "JOIN securities s ON s.id=t.security_id "
                "LEFT JOIN etf_profiles p ON p.security_id=s.id ORDER BY s.code"
            ).fetchall()
            self.trades = {}
            for trade in conn.execute("SELECT * FROM live_trades"):
                key = (trade["broker_account_id"], trade["security_id"])
                self.trades.setdefault(key, []).append(trade)
            self.actions = {}
            for action in conn.execute("SELECT * FROM corporate_actions"):
                self.actions.setdefault(action["security_id"], []).append(action)
            self.inconsistent = set()
            self.warnings = []
            for pair in self.pairs:
                key = (pair["broker_account_id"], pair["security_id"])
                problem = history_problem(
                    self.trades[key],
                    self.actions.get(key[1], []),
                    foreign_currency=share_classes.is_foreign_currency(
                        pair["fund_type"], pair["security_name"]
                    ),
                )
                if problem is not None:
                    self.inconsistent.add(key)
                    self.warnings.append(
                        {
                            **dict(pair),
                            **problem,
                            "revision_id": _latest_revision(conn, key[1], key[0])
                            if problem["reason"] == "oversell"
                            else None,
                        }
                    )


def history_warnings(conn: sqlite3.Connection) -> list[dict]:
    return HistoryBook(conn).warnings


def _latest_revision(conn, security_id, account_id):
    for row in conn.execute(
        "SELECT id, impact_json FROM corporate_action_revisions WHERE security_id=? "
        "ORDER BY changed_at DESC, rowid DESC",
        (security_id,),
    ):
        if any(
            impact["broker_account_id"] == account_id and impact["after"]
            for impact in json.loads(row["impact_json"])
        ):
            return row["id"]
    return None


def compute_position(
    conn: sqlite3.Connection,
    broker_account_id: str,
    security_id: str,
    as_of_date: str | None = None,
) -> replay.PositionResult:
    with read_transaction(conn):
        trades = fetch_trades(conn, broker_account_id, security_id)
        actions = fetch_corporate_actions(conn, security_id)
        return replay.replay_trades(
            trades, actions, as_of_date=as_of_date or today_taipei()
        )


@dataclass
class EntitlementEntry:
    distribution_id: str
    broker_account_id: str
    broker_account_name: str
    security_id: str
    security_code: str
    security_name: str
    ex_dividend_date: str | None
    pay_date: str | None
    amount_per_share: str | None
    amount_status: str
    entitled_quantity: Decimal
    expected_gross: Decimal | None
    is_future: bool
    reconciliation_status: str | None
    reconciliation_note: str | None


def _previous_day(iso_date: str) -> str:
    return (date.fromisoformat(iso_date) - timedelta(days=1)).isoformat()


def list_entitlements(
    conn: sqlite3.Connection, *, history=None
) -> list[EntitlementEntry]:
    """One entry per (broker_account, security, distribution) that the
    account held shares of at the relevant as-of date (design 009
    §4.3 point 1: the day before the ex-dividend date once it has
    passed; today's holdings, unchanged, while it is still upcoming).
    """
    with read_transaction(conn):
        history = history if history is not None else HistoryBook(conn)
        pairs = history.pairs
        if not pairs:
            return []

        security_ids = sorted({row["security_id"] for row in pairs})
        placeholders = ",".join("?" * len(security_ids))
        distributions = conn.execute(
            f"""
            SELECT d.*, s.code AS security_code, s.name AS security_name
            FROM distributions d
            JOIN securities s ON s.id = d.security_id
            WHERE d.security_id IN ({placeholders})
            """,
            security_ids,
        ).fetchall()

        # Load each history once. The old per-distribution compute_position call
        # reread trades and corporate actions for every account/security/event.
        pairs_by_security = {}
        for pair in pairs:
            pairs_by_security.setdefault(pair["security_id"], []).append(pair)
        trades_by_pair = history.trades
        actions_by_security = history.actions
        inconsistent = history.inconsistent

        today = today_taipei()

        def as_of(dist):
            # Entitlement is decided by the holding at the close of the day
            # BEFORE the ex-dividend date (buying on the ex-date itself
            # does not carry the right); for a future date this becomes a
            # same-holdings-unchanged projection using today's position.
            is_future = not dist["ex_dividend_date"] or dist["ex_dividend_date"] > today
            return today if is_future else _previous_day(dist["ex_dividend_date"])

        # Design 069: one replay per holding, read at every distribution's date.
        dates_by_security = {}
        for dist in distributions:
            dates_by_security.setdefault(dist["security_id"], set()).add(as_of(dist))
        held = {
            key: replay.quantities_at(
                trades_by_pair[key],
                actions_by_security.get(key[1], []),
                dates_by_security.get(key[1], ()),
            )
            for key in (
                (pair["broker_account_id"], pair["security_id"]) for pair in pairs
            )
            if key not in inconsistent and key[1] in dates_by_security
        }

        entries: list[EntitlementEntry] = []
        for dist in distributions:
            is_future = not dist["ex_dividend_date"] or dist["ex_dividend_date"] > today
            day = as_of(dist)

            for pair in pairs_by_security.get(dist["security_id"], []):
                key = (pair["broker_account_id"], dist["security_id"])
                if key in inconsistent:
                    continue
                quantity = held[key][day]
                if quantity <= 0:
                    continue

                # Paid per holder in whole NTD, cents dropped — the common
                # 「計算至元為止，元以下捨去」 announcement wording (issue #75).
                # Rounded per entry so day/month totals sum what is paid.
                expected_gross = None
                if dist["amount_per_share"] is not None:
                    expected_gross = (
                        quantity * Decimal(dist["amount_per_share"])
                    ).quantize(Decimal(1), rounding=ROUND_DOWN)

                entries.append(
                    EntitlementEntry(
                        distribution_id=dist["id"],
                        broker_account_id=pair["broker_account_id"],
                        broker_account_name=pair["broker_account_name"],
                        security_id=dist["security_id"],
                        security_code=dist["security_code"],
                        security_name=dist["security_name"],
                        ex_dividend_date=dist["ex_dividend_date"],
                        pay_date=dist["pay_date"],
                        amount_per_share=dist["amount_per_share"],
                        amount_status=dist["amount_status"],
                        entitled_quantity=quantity,
                        expected_gross=expected_gross,
                        is_future=is_future,
                        reconciliation_status=dist["reconciliation_status"],
                        reconciliation_note=dist["reconciliation_note"],
                    )
                )

        entries.sort(key=lambda e: e.ex_dividend_date or "9999")
        return entries


def confirmed_income_by_pair(
    conn: sqlite3.Connection,
    entries: list[EntitlementEntry] | None = None,
) -> dict[tuple[str, str], Decimal]:
    """Design 009 §4.6: the homepage's auto announced-dividend break-even
    basis — summed per (broker_account_id, security_id), counting only
    entries whose pay_date has arrived, amount is confirmed (`final`,
    never `estimated`/`unknown`), and whose source data isn't flagged
    `needs_review` (design 009 §4.4). Not future-projected, not
    unconfirmed, not from a distribution currently under reconciliation.
    `entries` lets a caller that already listed them avoid a second pass.
    """
    with read_transaction(conn):
        today = today_taipei()
        totals: dict[tuple[str, str], Decimal] = {}
        for entry in list_entitlements(conn) if entries is None else entries:
            if (
                entry.is_future
                or entry.amount_status != "final"
                or entry.expected_gross is None
                or not entry.pay_date
                or entry.pay_date > today
                or not countable(entry)
            ):
                continue
            key = (entry.broker_account_id, entry.security_id)
            totals[key] = totals.get(key, Decimal(0)) + entry.expected_gross
        return totals


def completeness(warnings):
    return {
        "data_complete": not warnings,
        "excluded_positions": warnings,
        "warnings": warnings,
    }


def entitlement_result(conn):
    with read_transaction(conn):
        history = HistoryBook(conn)
        return {
            "items": list_entitlements(conn, history=history),
            **completeness(history.warnings),
        }
