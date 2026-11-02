"""Trade CRUD with the §4.8 oversell check enforced before every write.

`trades` is directly editable (design 002 §4.6.3 — no revision chain),
but every insert or edit is validated by replaying the *entire*
post-change history through `replay.replay_trades` first; a write that
would make some sell impossible is rejected before it touches the
database, never after.
"""

import sqlite3
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from taiwan_stock.catalog import query as catalog_query
from taiwan_stock.catalog import share_classes, sync_history, trading_days
from taiwan_stock.infrastructure.db import read_transaction, write_transaction
from taiwan_stock.portfolio import (
    distribution_entitlement,
    limits,
    pricing,
    received_yield,
    replay,
)
from taiwan_stock.portfolio.distribution_entitlement import (  # noqa: F401 (re-exported)
    compute_position,
    fetch_corporate_actions,
    fetch_trades,
)
from taiwan_stock.portfolio.trading_rules import resolve_trading_rules
from taiwan_stock.shared.dates import now_utc_iso, today_taipei
from taiwan_stock.shared.ids import new_id

TRADE_FIELDS = (
    "broker_account_id",
    "security_id",
    "event_type",
    "trade_date",
    "quantity",
    "unit_price",
    "executed_amount",
    "commission",
    "transaction_tax",
    "other_fees",
    "note",
    "is_day_trade",
)


class TradeValidationError(ValueError):
    """Invalid trade input that must be rejected before persistence."""


VOIDED_EDIT_MESSAGE = "此交易已作廢，請先取消作廢再編輯"


def void_trade(conn: sqlite3.Connection, trade_id: str) -> None:
    """Keep the row but drop it from every calculation (issue #115 #10).

    The remaining history is replayed first: voiding a buy that a later
    sell depends on raises `replay.OversellError` and changes nothing.
    """
    _set_voided(conn, trade_id, voided=True)


def unvoid_trade(conn: sqlite3.Connection, trade_id: str) -> None:
    """Bring a voided trade back, after the same oversell replay."""
    _set_voided(conn, trade_id, voided=False)


def _set_voided(conn: sqlite3.Connection, trade_id: str, *, voided: bool) -> None:
    with write_transaction(conn):
        current = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if current is None:
            raise ValueError(f"trade {trade_id} not found")
        if (current["voided_at"] is not None) == voided:
            return
        conn.execute(
            "UPDATE trades SET voided_at = ? WHERE id = ?",
            (now_utc_iso() if voided else None, trade_id),
        )
        history = fetch_trades(
            conn, current["broker_account_id"], current["security_id"]
        )
        if history:
            replay.replay_trades(
                history,
                fetch_corporate_actions(conn, current["security_id"]),
                as_of_date=max(t["trade_date"] for t in history),
            )


def create_trade(conn: sqlite3.Connection, fields: dict) -> str:
    with write_transaction(conn):
        candidate = {name: fields.get(name) for name in TRADE_FIELDS}
        candidate["id"] = new_id()
        candidate["created_at"] = now_utc_iso()
        candidate["is_day_trade"] = _normalize_day_trade_flag(
            candidate["is_day_trade"], candidate["event_type"]
        )

        candidate["trade_order"] = _next_trade_order(conn)
        _validate(conn, candidate)
        _apply_fee_defaults(conn, candidate)
        _insert(conn, candidate)
        return candidate["id"]


def prepare(conn: sqlite3.Connection, fields: dict) -> dict:
    """The exact values create_trade/update_trade would store for
    `fields` — day-trade flag normalized, fields validated, blank fees
    defaulted — without the history replay or any write, so a batch
    (CSV import) can compare against stored trades before deciding to
    write at all.
    """
    with read_transaction(conn):
        candidate = {name: fields.get(name) for name in TRADE_FIELDS}
        candidate["is_day_trade"] = _normalize_day_trade_flag(
            candidate["is_day_trade"], candidate["event_type"]
        )
        _validate_fields(conn, candidate)
        _apply_fee_defaults(conn, candidate)
        return candidate


def apply_batch(
    conn: sqlite3.Connection, creates: list[dict], updates: dict[str, dict]
) -> list[str]:
    """Writes already-`prepare`d trades, then replays each touched
    (account, security) history once. Validating only the final state
    lets a batch lower a buy and the sell depending on it in either
    order, which per-row replay would reject halfway through. Raises
    `replay.OversellError` (rolling everything back) like single writes.
    """
    with write_transaction(conn):
        touched = set()
        for trade_id, values in updates.items():
            current = conn.execute(
                "SELECT broker_account_id, security_id, voided_at FROM trades "
                "WHERE id = ?",
                (trade_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"trade {trade_id} not found")
            if current["voided_at"] is not None:
                raise TradeValidationError(VOIDED_EDIT_MESSAGE)
            if (
                "broker_account_id" in values
                and values["broker_account_id"] != current["broker_account_id"]
            ):
                raise TradeValidationError("不能變更既有交易的券商帳戶")
            if (
                "security_id" in values
                and values["security_id"] != current["security_id"]
            ):
                raise TradeValidationError("不能變更既有交易的商品")
            _update(conn, {**values, "id": trade_id})
            touched.add((current["broker_account_id"], current["security_id"]))
        created = []
        for values in creates:
            candidate = {
                **values,
                "id": values.get("id") or new_id(),
                "created_at": now_utc_iso(),
                "trade_order": _next_trade_order(conn),
            }
            _insert(conn, candidate)
            created.append(candidate["id"])
            touched.add((candidate["broker_account_id"], candidate["security_id"]))
        for broker_account_id, security_id in sorted(touched):
            history = fetch_trades(conn, broker_account_id, security_id)
            if not history:
                continue
            replay.replay_trades(
                history,
                fetch_corporate_actions(conn, security_id),
                as_of_date=max(t["trade_date"] for t in history),
            )
        return created


def _next_trade_order(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(trade_order), 0) + 1 FROM trades"
    ).fetchone()[0]


def _insert(conn: sqlite3.Connection, candidate: dict) -> None:
    conn.execute(
        """
        INSERT INTO trades
            (id, broker_account_id, security_id, event_type, trade_date,
             quantity, unit_price, executed_amount, commission,
             transaction_tax, other_fees, note, is_day_trade, created_at,
             trade_order)
        VALUES
            (:id, :broker_account_id, :security_id, :event_type, :trade_date,
             :quantity, :unit_price, :executed_amount, :commission,
             :transaction_tax, :other_fees, :note, :is_day_trade, :created_at,
             :trade_order)
        """,
        candidate,
    )


def _update(conn: sqlite3.Connection, merged: dict) -> None:
    conn.execute(
        """
        UPDATE trades SET
            event_type = :event_type,
            trade_date = :trade_date,
            quantity = :quantity,
            unit_price = :unit_price,
            executed_amount = :executed_amount,
            commission = :commission,
            transaction_tax = :transaction_tax,
            other_fees = :other_fees,
            note = :note,
            is_day_trade = :is_day_trade
        WHERE id = :id
        """,
        merged,
    )


def update_trade(
    conn: sqlite3.Connection, trade_id: str, fields: dict, *, recalculate: bool = False
) -> None:
    with write_transaction(conn):
        current = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if current is None:
            raise ValueError(f"trade {trade_id} not found")
        if current["voided_at"] is not None:
            raise TradeValidationError(VOIDED_EDIT_MESSAGE)

        if (
            "broker_account_id" in fields
            and fields["broker_account_id"] != current["broker_account_id"]
        ):
            raise TradeValidationError("不能變更既有交易的券商帳戶")
        if "security_id" in fields and fields["security_id"] != current["security_id"]:
            raise TradeValidationError("不能變更既有交易的商品")

        merged = {name: dict(current)[name] for name in TRADE_FIELDS}
        merged.update({k: v for k, v in fields.items() if k in TRADE_FIELDS})
        merged["id"] = trade_id
        merged["created_at"] = current["created_at"]
        merged["trade_order"] = current["trade_order"]
        merged["is_day_trade"] = _normalize_day_trade_flag(
            merged["is_day_trade"], merged["event_type"]
        )

        if recalculate:
            if (
                merged["event_type"] not in ("buy", "sell")
                or merged["unit_price"] is None
            ):
                raise TradeValidationError("成本重算僅適用於有單價的買賣交易")
            derived = ["executed_amount", "commission"]
            if merged["event_type"] == "sell":
                derived.append("transaction_tax")
            for name in derived:
                merged[name] = fields.get(name)

        _validate(conn, merged, excluding_trade_id=trade_id)
        _apply_fee_defaults(conn, merged)
        _update(conn, merged)


def _normalize_day_trade_flag(value, event_type: str) -> bool | None:
    """Only a sell can be a day trade; every other event_type is forced
    to NULL regardless of what a caller sent, so a stray value left over
    from editing a trade's type can't linger as a meaningless flag."""
    if event_type != "sell":
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _apply_fee_defaults(conn: sqlite3.Connection, candidate: dict) -> None:
    """Fill blank commission/executed_amount/transaction_tax from the
    statutory formula; a value the caller already supplied always wins
    (design 002 §4.9's discounts/promotions and IPO-lottery flat fees
    aren't formula-derivable, so a manual entry must never be
    overwritten). `executed_amount` is the *gross* trade value
    (§4.8: "買進批次成本 = executed_amount + commission + other_fees") —
    computed here whenever `unit_price` is known, for every event_type.
    Commission/tax defaults are buy/sell only: an opening entry records
    a pre-existing position, not a fee-bearing trade in this app
    (`test_opening_cost_excludes_commission_per_design`).
    """
    if candidate["unit_price"] is None:
        return

    unit_price = Decimal(candidate["unit_price"])
    quantity = Decimal(candidate["quantity"])

    if candidate["executed_amount"] is None:
        candidate["executed_amount"] = str(pricing.gross_amount(unit_price, quantity))

    if candidate["event_type"] not in ("buy", "sell"):
        return

    security = conn.execute(
        "SELECT * FROM securities WHERE id = ?",
        (candidate["security_id"],),
    ).fetchone()
    rules = resolve_trading_rules(conn, security, candidate["trade_date"])
    fee_schedule = rules.fees

    if candidate["commission"] is None:
        candidate["commission"] = str(fee_schedule.commission(unit_price, quantity))

    if candidate["event_type"] == "sell" and candidate["transaction_tax"] is None:
        day_trade_discount = (
            rules.day_trade_eligible and candidate["is_day_trade"] is True
        )
        candidate["transaction_tax"] = str(
            fee_schedule.tax(
                unit_price, quantity, day_trade_discount=day_trade_discount
            )
        )


def _validate(
    conn: sqlite3.Connection, candidate: dict, *, excluding_trade_id: str | None = None
) -> None:
    _validate_fields(conn, candidate)

    existing = fetch_trades(
        conn, candidate["broker_account_id"], candidate["security_id"]
    )
    if excluding_trade_id is not None:
        existing = [t for t in existing if t["id"] != excluding_trade_id]
    actions = fetch_corporate_actions(conn, candidate["security_id"])

    # Validate every recorded trade, including future sells. Position
    # queries still use their requested cutoff; write validation must not.
    history = [*existing, candidate]
    replay.replay_trades(
        history, actions, as_of_date=max(t["trade_date"] for t in history)
    )


def _validate_fields(conn: sqlite3.Connection, candidate: dict) -> None:
    if candidate["event_type"] not in ("buy", "sell", "opening"):
        raise TradeValidationError("event_type 必須是 buy／sell／opening")
    try:
        value = candidate["trade_date"]
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise TradeValidationError("trade_date 必須是有效日期（YYYY-MM-DD）") from exc
    try:
        limits.check_quantity(candidate["quantity"])
    except ValueError as exc:
        raise TradeValidationError(
            f"quantity 必須是 1 到 {limits.MAX_QUANTITY:,} 的整數"
        ) from exc

    account_exists = conn.execute(
        "SELECT 1 FROM broker_accounts WHERE id = ?",
        (candidate["broker_account_id"],),
    ).fetchone()
    if account_exists is None:
        raise TradeValidationError(f"找不到券商帳戶 {candidate['broker_account_id']}")
    security = conn.execute(
        "SELECT s.name, p.fund_type FROM securities s "
        "LEFT JOIN etf_profiles p ON p.security_id = s.id WHERE s.id = ?",
        (candidate["security_id"],),
    ).fetchone()
    if security is None:
        raise TradeValidationError(f"找不到商品 {candidate['security_id']}")
    # Design 063: no exchange rates yet, so a foreign-currency class can't be
    # recorded as if it were priced in TWD.
    if share_classes.is_foreign_currency(security["fund_type"], security["name"]):
        raise TradeValidationError(
            "security_code 是外幣計價的 ETF 級別，目前只支援新台幣計價的交易"
        )

    # Design 060: bounded so no stored value can overflow a later calculation.
    for field in limits.MONEY_FIELDS:
        value = candidate[field]
        if value is None:
            continue
        try:
            amount = limits.check_amount(value, limits.maximum_for(field), f"{field} ")
        except ValueError as exc:
            raise TradeValidationError(str(exc)) from exc
        if field == "unit_price":
            try:
                limits.check_gross(amount, candidate["quantity"])
            except ValueError as exc:
                raise TradeValidationError(
                    f"unit_price 乘以股數後的成交金額超過 {limits.MAX_AMOUNT:,}"
                ) from exc


def list_all_trades(conn: sqlite3.Connection) -> list[dict]:
    """Complete joined history for existing API/internal callers, voided
    trades included (each row carries `voided_at`).

    The HTML history page uses trade_query.browse for filtered pagination.
    """
    rows = conn.execute(
        """
        SELECT t.*, b.name AS broker_account_name,
               s.code AS security_code, s.name AS security_name
        FROM trades t
        JOIN broker_accounts b ON b.id = t.broker_account_id
        JOIN securities s ON s.id = t.security_id
        ORDER BY t.trade_date DESC, t.created_at DESC, t.trade_order DESC
        """
    ).fetchall()
    trades = [dict(row) for row in rows]
    for trade in trades:
        total = _trade_total_amount(trade)
        trade["total_amount"] = str(total) if total is not None else None
    return trades


def _trade_total_amount(trade: dict) -> Decimal | None:
    """Actual cash impact of one trade — what left/entered the account,
    including fees. Mirrors `replay._trade_cost`'s buy/opening formula
    and the sell-net formula in `list_position_overview` below, so this
    display value always agrees with what the app's own cost/P&L engine
    is actually using; it must never drift into a third formula.
    """
    if trade["executed_amount"] is None:
        return None
    amount = Decimal(trade["executed_amount"])
    if trade["event_type"] == "opening":
        return amount
    commission = Decimal(trade["commission"] or 0)
    other = Decimal(trade["other_fees"] or 0)
    if trade["event_type"] == "sell":
        tax = Decimal(trade["transaction_tax"] or 0)
        return amount - commission - tax - other
    return amount + commission + other


def list_position_overview(
    conn: sqlite3.Connection, *, security_id: str | None = None, history=None
) -> list[dict]:
    """Current position, break-even price, latest price, market value,
    unrealized P&L, daily P&L, and dividend yield for every
    (broker_account, security) pair that has at least one trade — the
    data behind the homepage's holdings table (§4.3.2).
    """
    with read_transaction(conn):
        where_sql = "WHERE t.security_id = ?" if security_id is not None else ""
        where_params = (security_id,) if security_id is not None else ()
        pairs = conn.execute(
            f"""
            SELECT DISTINCT t.broker_account_id, t.security_id,
                   b.name AS broker_account_name,
                   s.code AS security_code, s.name AS security_name,
                   s.security_type AS security_type,
                   s.market AS market, s.listed_date AS listed_date
            FROM live_trades t
            JOIN broker_accounts b ON b.id = t.broker_account_id
            JOIN securities s ON s.id = t.security_id
            {where_sql}
            ORDER BY s.code
            """,
            where_params,
        ).fetchall()
        if not pairs:
            return []

        history = (
            history
            if history is not None
            else distribution_entitlement.HistoryBook(conn)
        )
        entitlements = distribution_entitlement.list_entitlements(conn, history=history)
        confirmed_income = distribution_entitlement.confirmed_income_by_pair(
            conn, entitlements
        )
        entitlements_by_pair = {}
        for entry in entitlements:
            key = (entry.broker_account_id, entry.security_id)
            entitlements_by_pair.setdefault(key, []).append(entry)
        # Design 028 §4.2: read once, shared by every ETF row's data check.
        sync_units = sync_history.latest_units(conn)
        sync_successes = sync_history.latest_successes(conn)
        calendar = trading_days.TradingCalendar(conn)
        actions_by_security = history.actions
        as_of_today = today_taipei()
        rules_by_security = {
            pair["security_id"]: resolve_trading_rules(
                conn, {**dict(pair), "code": pair["security_code"]}, as_of_today
            )
            for pair in pairs
        }

        overview = []
        for pair in pairs:
            sec_id = pair["security_id"]
            if sec_id not in actions_by_security:
                actions_by_security[sec_id] = fetch_corporate_actions(conn, sec_id)
            pair_actions = actions_by_security[sec_id]
            pair_trades = history.trades[(pair["broker_account_id"], sec_id)]
            # Reported by distribution_entitlement.history_warnings (#115 #8).
            if (pair["broker_account_id"], sec_id) in history.inconsistent:
                continue
            position = replay.replay_trades(
                pair_trades, pair_actions, as_of_date=as_of_today
            )
            if position.remaining_quantity <= 0:
                continue
            rules = rules_by_security[sec_id]
            fee_schedule = rules.fees
            breakeven = pricing.compute_breakeven(
                position, fee_schedule, tick_schedule=rules.tick_schedule
            )

            price_rows = conn.execute(
                """
                SELECT trade_date, close, volume
                FROM daily_prices
                WHERE security_id = ?
                ORDER BY trade_date DESC
                LIMIT 2
                """,
                (sec_id,),
            ).fetchall()

            latest_close = None
            close_date = None
            market_value = None
            unrealized_pnl = None
            unrealized_pnl_pct = None
            daily_pnl = None
            daily_pnl_unknown = False

            if price_rows:
                latest_row = price_rows[0]
                latest_close = latest_row["close"]
                close_date = latest_row["trade_date"]
                close_dec = Decimal(latest_close)
                qty_dec = Decimal(position.remaining_quantity)
                market_value, unrealized_pnl, unrealized_pnl_pct = _paper_pnl(
                    qty_dec, position.remaining_cost, close_dec
                )

                # Issue #89: only same-day trades are netted below, so a gap
                # in stored closes would count a buy inside it as profit.
                if len(price_rows) > 1 and calendar.adjacent(
                    price_rows[1]["trade_date"], close_date
                ):
                    prev_row = price_rows[1]
                    prev_close = Decimal(prev_row["close"])
                    prev_date = prev_row["trade_date"]
                    prev_pos = replay.replay_trades(
                        pair_trades, pair_actions, as_of_date=prev_date
                    )
                    prev_qty = Decimal(prev_pos.remaining_quantity)

                    # Anchored on close_date (the last day we actually have
                    # a price for), not `qty_dec`'s live "as of today"
                    # quantity — a trade dated after close_date (price sync
                    # hasn't run yet today) must not silently inflate this
                    # day's P&L with an uncosted share count; excluding it
                    # here means it's simply not part of this day's P&L yet.
                    t_pos = replay.replay_trades(
                        pair_trades, pair_actions, as_of_date=close_date
                    )
                    t_qty = Decimal(t_pos.remaining_quantity)

                    t_trades = [t for t in pair_trades if t["trade_date"] == close_date]
                    t_sell_net = Decimal("0")
                    t_buy_gross = Decimal("0")
                    # Design 061: a same-day buy/sell with neither price nor
                    # amount has unknown cash, which must not read as zero.
                    daily_pnl_unknown = any(
                        t["event_type"] in ("buy", "sell")
                        and t["executed_amount"] is None
                        and t["unit_price"] is None
                        for t in t_trades
                    )
                    if daily_pnl_unknown or any(
                        t["event_type"] == "opening" for t in t_trades
                    ):
                        daily_pnl = None
                    else:
                        for t in t_trades:
                            amount = (
                                Decimal(t["executed_amount"])
                                if t["executed_amount"] is not None
                                else Decimal(t["unit_price"]) * t["quantity"]
                            )
                            comm = Decimal(t["commission"] or 0)
                            tax = Decimal(t["transaction_tax"] or 0)
                            other = Decimal(t["other_fees"] or 0)
                            if t["event_type"] == "sell":
                                t_sell_net += amount - comm - tax - other
                            elif t["event_type"] == "buy":
                                t_buy_gross += amount + comm + other

                        dp = pricing.whole_twd(
                            (close_dec * t_qty)
                            - (prev_close * prev_qty)
                            + t_sell_net
                            - t_buy_gross
                        )
                        daily_pnl = str(dp)

            dist_rows = conn.execute(
                """
                SELECT *
                FROM distributions
                WHERE security_id = ? AND ex_dividend_date IS NOT NULL
                """,
                (sec_id,),
            ).fetchall()
            # Same frequency, months and data check as the catalog list and
            # detail page, so all three show one yield (design 058 §4.2.2).
            profile = (
                conn.execute(
                    "SELECT * FROM etf_profiles WHERE security_id = ?",
                    (sec_id,),
                ).fetchone()
                if pair["security_type"] == "etf"
                else None
            )
            _, yield_info = catalog_query.frequency_and_yield(
                profile,
                dist_rows,
                latest_close,
                as_of=as_of_today,
                security_type=pair["security_type"],
                security_id=sec_id,
                market=pair["market"],
                listed_date=pair["listed_date"],
                units=sync_units,
                successes=sync_successes,
                actions=pair_actions,
            )
            # Design 046: the same expected annual cash as the yield column,
            # so a newly paying ETF is not costed on one quarter alone.
            dist_sum = yield_info["annual_cash_per_share"]
            cost_yield = None
            dividend_yield = None
            if yield_info["known_yield"] is not None:
                dividend_yield = str(
                    yield_info["known_yield"].quantize(Decimal("0.01"))
                )
            if (
                yield_info["cash_reliable"]
                and position.remaining_cost is not None
                and position.remaining_cost > 0
            ):
                cost_yield = str(
                    (
                        dist_sum
                        * position.remaining_quantity
                        / position.remaining_cost
                        * 100
                    ).quantize(Decimal("0.01"))
                )
            # Design 029: an unreliable ETF dividend record can't be called
            # received either, so it shares the cost yield's reason.
            received = received_yield.compute(
                entitlements_by_pair.get((pair["broker_account_id"], sec_id), []),
                pair_trades,
                pair_actions,
                as_of_today,
            )
            if yield_info["cash_label"]:
                received.value, received.label = None, yield_info["cash_label"]
            # Design 009 §4.6: the auto announced-dividend basis — confirmed
            # (pay_date arrived, amount final, not flagged for reconciliation)
            # distribution income only. Not the manual dividend_receipts net
            # amount, not a bank-confirmed deposit, and never mixed with it.
            confirmed_dividend_income = confirmed_income.get(
                (pair["broker_account_id"], sec_id), Decimal(0)
            )
            income_breakeven = None
            if position.remaining_cost is not None:
                income_breakeven = pricing.find_breakeven_price(
                    position.remaining_quantity,
                    max(
                        Decimal(0), position.remaining_cost - confirmed_dividend_income
                    ),
                    fee_schedule,
                    tick_schedule=rules.tick_schedule,
                )
            breakeven_by_trade = {
                lot["trade_id"]: lot["breakeven_price"] for lot in breakeven["lots"]
            }
            trade_prices = {t["id"]: _trade_price(t) for t in pair_trades}
            today_date = date.fromisoformat(as_of_today)
            lot_breakevens = []
            for lot in position.lots:
                # Design 049 §4.2.2: same paper P&L as the holding row.
                lot_mv = lot_pnl = lot_pct = None
                if latest_close is not None:
                    lot_mv, lot_pnl, lot_pct = _paper_pnl(
                        Decimal(lot.adjusted_quantity),
                        lot.cost,
                        Decimal(latest_close),
                    )
                lot_breakevens.append(
                    {
                        "trade_id": lot.trade_id,
                        "trade_date": lot.trade_date,
                        "holding_days": (
                            today_date - date.fromisoformat(lot.trade_date)
                        ).days,
                        "original_quantity": lot.original_remaining_quantity,
                        "adjusted_quantity": lot.adjusted_quantity,
                        "cost": lot.cost,
                        "trade_price": trade_prices.get(lot.trade_id),
                        "breakeven_price": breakeven_by_trade.get(lot.trade_id),
                        "market_value": lot_mv,
                        "unrealized_pnl": lot_pnl,
                        "unrealized_pnl_pct": lot_pct,
                    }
                )
            overview.append(
                {
                    "broker_account_id": pair["broker_account_id"],
                    "security_id": pair["security_id"],
                    "broker_account_name": pair["broker_account_name"],
                    "security_code": pair["security_code"],
                    "security_name": pair["security_name"],
                    "position": position,
                    "pricing_warnings": list(rules.warnings),
                    "pricing_rule_id": rules.rule_id,
                    "pricing_rule_date": rules.rule_date,
                    "average_cost": _average_cost(
                        position.remaining_cost, position.remaining_quantity
                    ),
                    "lot_breakevens": lot_breakevens,
                    "yield_info": yield_info,
                    "income_breakeven": income_breakeven,
                    "confirmed_dividend_income": confirmed_dividend_income,
                    "breakeven_price": breakeven["overall_breakeven_price"],
                    "latest_close": latest_close,
                    "close_date": close_date,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_pct": unrealized_pnl_pct,
                    "daily_pnl": daily_pnl,
                    "daily_pnl_unknown": daily_pnl_unknown,
                    "dividend_yield": dividend_yield,
                    "cost_yield": cost_yield,
                    "cost_yield_label": yield_info["cash_label"]
                    or (
                        f"推估（依 {yield_info['annualized_from']} 次）"
                        if yield_info["annualized_from"]
                        else None
                    ),
                    "received_yield": str(received.value)
                    if received.value is not None
                    else None,
                    "received_yield_label": received.label,
                }
            )
        _attach_weights(overview)
        # Design 012 §4.4: largest holdings first, so the no-JS order is
        # already the one the page's client-side sort defaults to.
        overview.sort(
            key=lambda row: (
                row["market_value"] is None,
                -Decimal(row["market_value"] or 0),
                row["security_code"],
                row["broker_account_name"],
            )
        )
        return overview


def position_overview_result(conn, *, security_id=None):
    with read_transaction(conn):
        history = distribution_entitlement.HistoryBook(conn)
        items = list_position_overview(conn, security_id=security_id, history=history)
        warnings = [
            w
            for w in history.warnings
            if security_id is None or w["security_id"] == security_id
        ]
        if warnings:
            for item in items:
                item["weight_pct"] = None
        return {"items": items, **distribution_entitlement.completeness(warnings)}


def _paper_pnl(
    quantity: Decimal, cost: Decimal | None, close: Decimal
) -> tuple[str, str | None, str | None]:
    """Market value, P&L and return % before selling costs.

    Issue #75: whole-NTD per row, and P&L is the difference of the two
    whole figures shown, so the row visibly adds up. Design 049 reuses it
    per lot, so lots may sum to a few NTD off the holding row.
    """
    mv_dec = pricing.whole_twd(quantity * close)
    if cost is None:
        return str(mv_dec), None, None
    cost_dec = pricing.whole_twd(Decimal(cost))
    pnl_dec = mv_dec - cost_dec
    pct = None
    if cost_dec > 0:
        pct = str(
            ((pnl_dec / cost_dec) * 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    return str(mv_dec), str(pnl_dec), pct


def _average_cost(cost: Decimal | None, quantity: Decimal) -> str | None:
    if cost is None or quantity <= 0:
        return None
    return str((cost / quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _trade_price(trade) -> str | None:
    """The price as recorded on the trade (fees excluded, not split
    adjusted), so a lot row matches what the user entered in 交易紀錄.
    Falls back to gross amount ÷ quantity when only the amount is known;
    an opening entry with neither has no price."""
    if trade["unit_price"] is not None:
        return str(trade["unit_price"])
    if trade["executed_amount"] is not None and trade["quantity"]:
        return str(
            (Decimal(trade["executed_amount"]) / trade["quantity"]).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    return None


def _attach_weights(overview: list[dict]) -> None:
    """Each position's share of the priced holdings' total market value.
    Positions without a price get None — they're already counted in the
    summary card's missing-price note rather than silently diluting
    everyone else's weight (design 012 §4.5)."""
    total = sum(
        (
            Decimal(row["market_value"])
            for row in overview
            if row["market_value"] is not None
        ),
        Decimal(0),
    )
    for row in overview:
        row["weight_pct"] = None
        if row["market_value"] is not None and total > 0:
            row["weight_pct"] = str(
                (Decimal(row["market_value"]) / total * 100).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )


def compute_portfolio_summary(positions: list[dict], *, excluded_positions=()) -> dict:
    """Computes total market value, remaining cost, unrealized P&L,
    daily P&L, and data completeness counts for the homepage summary
    cards (§4.3.2).
    """
    total_market_value = Decimal("0")
    total_cost = Decimal("0")
    total_daily_pnl = Decimal("0")
    has_market_value = False
    has_cost = False
    has_daily_pnl = False
    has_unrealized_pnl = False
    missing_price_count = 0
    missing_cost_count = 0
    unrealized_missing_count = 0
    daily_pnl_unknown_count = 0
    latest_quote_date = None
    quote_dates = []

    pnl_sum = Decimal("0")
    cost_sum = Decimal("0")

    for row in positions:
        mv = row.get("market_value")
        if mv is not None:
            total_market_value += Decimal(str(mv))
            has_market_value = True
        else:
            missing_price_count += 1

        # Same whole-NTD cost each row shows, so the total adds up (#75).
        cost = row["position"].remaining_cost
        if cost is not None:
            cost = pricing.whole_twd(Decimal(str(cost)))
            total_cost += cost
            has_cost = True
        else:
            missing_cost_count += 1

        dp = row.get("daily_pnl")
        if dp is not None:
            total_daily_pnl += Decimal(str(dp))
            has_daily_pnl = True
        if row.get("daily_pnl_unknown"):
            daily_pnl_unknown_count += 1

        q_date = row.get("close_date")
        if q_date:
            quote_dates.append(q_date)
            if latest_quote_date is None or q_date > latest_quote_date:
                latest_quote_date = q_date

        # A row's P&L exists only with both its price and cost, so the
        # total needs a row that has both, not one row of each (design 061).
        if row.get("unrealized_pnl") is not None and cost is not None:
            pnl_sum += Decimal(str(row["unrealized_pnl"]))
            cost_sum += Decimal(str(cost))
            has_unrealized_pnl = True
        else:
            unrealized_missing_count += 1

    total_unrealized_pnl = None
    total_unrealized_pnl_pct = None
    if has_unrealized_pnl:
        total_unrealized_pnl = str(pnl_sum)
        if cost_sum > 0:
            total_unrealized_pnl_pct = str(
                ((pnl_sum / cost_sum) * 100).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )

    return {
        # Row figures are already whole NTD, so these sums are too.
        "total_market_value": str(total_market_value) if has_market_value else None,
        "total_cost": str(total_cost) if has_cost else None,
        "total_unrealized_pnl": total_unrealized_pnl,
        "total_unrealized_pnl_pct": None
        if excluded_positions
        else total_unrealized_pnl_pct,
        "data_complete": not excluded_positions,
        "excluded_count": len(excluded_positions),
        "total_daily_pnl": str(total_daily_pnl) if has_daily_pnl else None,
        "missing_price_count": missing_price_count,
        "missing_cost_count": missing_cost_count,
        "unrealized_missing_count": unrealized_missing_count,
        "daily_pnl_unknown_count": daily_pnl_unknown_count,
        "latest_quote_date": latest_quote_date,
        # TWSE and TPEx publish on their own schedules, so one market can
        # already be on today's close while the other is days behind —
        # the latest date alone would imply every holding is current.
        "stale_quote_count": sum(1 for d in quote_dates if d < latest_quote_date),
        "earliest_quote_date": min(quote_dates) if quote_dates else None,
    }
