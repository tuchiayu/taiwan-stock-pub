"""Partial-sell what-if simulation (design 002 §4.9).

"試算不新增交易、扣持股或下單" — this never writes to `trades`. It
reuses the same replay engine a real sell would go through (including
the §4.8 oversell check) by diffing the position before/after a
candidate sell that is never persisted, rather than re-deriving FIFO
consumption logic a second time.
"""

from dataclasses import dataclass
from decimal import Decimal

from taiwan_stock.portfolio import limits, pricing, replay

OUT_OF_RANGE_MESSAGE = "交易金額或股數超出可計算範圍，請先修正交易。"
FOREIGN_CURRENCY_MESSAGE = "外幣計價的 ETF 級別，目前不提供試算。"
# Design 074: by `distribution_entitlement.stored_problem` reason.
PROBLEM_MESSAGES = {
    "foreign_currency": FOREIGN_CURRENCY_MESSAGE,
    "out_of_range": OUT_OF_RANGE_MESSAGE,
}

_SIMULATION_TRADE_ID = "__simulation__"
# Sorts after any real trade on the same day, so the simulated sell
# never reorders same-day real activity ahead of itself.
_SIMULATION_CREATED_AT = "9999-12-31T23:59:59.999Z"


class SimulationError(Exception):
    pass


@dataclass
class SaleSimulationResult:
    quantity: int
    cost_consumed: Decimal | None
    breakeven_price: Decimal | None
    price: Decimal | None
    net_proceeds: Decimal | None
    realized_pnl: Decimal | None


def simulate_partial_sale(
    trades: list,
    actions: list,
    quantity: int,
    fee_schedule: pricing.FeeSchedule,
    as_of_date: str,
    price: Decimal | str | None = None,
    *,
    tick_schedule=None,
) -> SaleSimulationResult:
    if price is not None and str(price).strip():
        try:
            price = limits.check_amount(price, limits.MAX_UNIT_PRICE, "試算價格")
        except ValueError as exc:
            raise SimulationError(str(exc)) from exc
    else:
        price = None
    if quantity <= 0:
        raise SimulationError("試算股數必須大於 0")
    if any(limits.trade_problem(t) for t in trades):
        raise SimulationError(OUT_OF_RANGE_MESSAGE)

    try:
        before = replay.replay_trades(trades, actions, as_of_date=as_of_date)
    except replay.OversellError as exc:
        raise SimulationError(f"持股資料不一致，無法試算：{exc}") from exc

    candidate = {
        "id": _SIMULATION_TRADE_ID,
        "event_type": "sell",
        "trade_date": as_of_date,
        "created_at": _SIMULATION_CREATED_AT,
        "quantity": quantity,
    }
    try:
        after = replay.replay_trades(
            [*trades, candidate], actions, as_of_date=as_of_date
        )
    except replay.OversellError as exc:
        raise SimulationError(
            f"目前可賣 {before.remaining_quantity} 股，無法試算賣出 {quantity} 股"
        ) from exc

    cost_consumed = None
    if before.remaining_cost is not None and after.remaining_cost is not None:
        cost_consumed = before.remaining_cost - after.remaining_cost

    breakeven_price = None
    if cost_consumed is not None:
        breakeven_price = pricing.find_breakeven_price(
            Decimal(quantity), cost_consumed, fee_schedule, tick_schedule=tick_schedule
        )

    net_proceeds = None
    realized_pnl = None
    if price is not None:
        net_proceeds = fee_schedule.net_proceeds(price, Decimal(quantity))
        if cost_consumed is not None:
            # Against the whole-NTD cost the page shows, so the three
            # figures visibly add up (issue #75).
            realized_pnl = net_proceeds - pricing.whole_twd(cost_consumed)

    return SaleSimulationResult(
        quantity=quantity,
        cost_consumed=cost_consumed,
        breakeven_price=breakeven_price,
        price=price,
        net_proceeds=net_proceeds,
        realized_pnl=realized_pnl,
    )
