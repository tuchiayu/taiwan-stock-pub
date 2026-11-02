"""Received dividend yield (design 029): what the money actually earned.

Cost yield (`trades.list_position_overview`) scales the latest year of
per-share cash by today's share count, so a recent top-up reads as if it
had been held all year. Dividing the cash actually received by today's
cost would err the other way, as if the new money had been invested all
year. Here the numerator is each ex-date's real entitlement and the
denominator is the remaining cost averaged over every day of the same
12 months, so each amount counts only for the days it was invested.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from taiwan_stock.catalog.distribution_rows import countable
from taiwan_stock.portfolio import replay
from taiwan_stock.portfolio.yields import add_months


@dataclass
class ReceivedYield:
    value: Decimal | None
    label: str | None
    received: Decimal
    average_cost: Decimal | None
    pending_count: int


def compute(entries, trades, actions, today: str) -> ReceivedYield:
    """`entries` are one account/security's `EntitlementEntry` rows."""
    end = date.fromisoformat(today)
    start = add_months(end, -12)
    window = [
        e
        for e in entries
        if e.ex_dividend_date and start.isoformat() < e.ex_dividend_date <= today
    ]
    final = [
        e
        for e in window
        if e.amount_status == "final" and e.expected_gross is not None and countable(e)
    ]
    pending = len(window) - len(final)
    received = sum((e.expected_gross for e in final), Decimal(0))
    average_cost = _average_cost(trades, actions, start, end)

    def unavailable(label):
        return ReceivedYield(None, label, received, average_cost, pending)

    if average_cost is None:
        return unavailable("成本未知")
    if not window:
        return unavailable("持有期間尚無除息")
    if not final:
        return unavailable(f"{pending} 筆金額待確認")
    if average_cost <= 0:  # e.g. an opening position recorded at 0
        return unavailable("持有成本為 0，無法計算")
    value = (received / average_cost * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return ReceivedYield(
        value,
        f"{pending} 筆金額待確認" if pending else None,
        received,
        average_cost,
        pending,
    )


def _average_cost(trades, actions, start: date, end: date) -> Decimal | None:
    """Mean end-of-day remaining cost over `(start, end]`, 0 while unheld.

    Cost only changes on trade dates (a stock dividend or split moves the
    share count, never the cost), so each stretch between them is read
    once instead of replaying every day."""
    first = start + timedelta(days=1)
    changes = sorted(
        {first}
        | {
            date.fromisoformat(t["trade_date"])
            for t in trades
            if first <= date.fromisoformat(t["trade_date"]) <= end
        }
    )
    total = Decimal(0)
    for index, day in enumerate(changes):
        until = changes[index + 1] if index + 1 < len(changes) else end + timedelta(1)
        cost = replay.replay_trades(trades, actions, as_of_date=day.isoformat())
        if cost.remaining_cost is None:
            return None
        total += cost.remaining_cost * (until - day).days
    return total / (end - start).days
