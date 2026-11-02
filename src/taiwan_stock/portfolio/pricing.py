"""Break-even price calculation (design 002 §4.9).

Fee/tax figures used as defaults are verified, not guessed, as of
2026-09: TWSE's tick-size schedule (six tiers) from
https://www.twse.com.tw/downloads/zh/trading/introduce/introduce004.pdf,
and the statutory commission ceiling (0.1425%) and transaction-tax
rates (0.3% stocks, 0.1% ETFs — the ETF discount is the whole reason
§4.7's `securities.transfer_agent_name` vs `etf_profiles` distinction
matters here too). Actual brokerage commission discounts and minimum
fees are NOT statutory — those stay user-configurable via
`app_settings`, defaulting to no discount and an illustrative NT$20
floor that is a common convention, not a verified universal one.
"""

import sqlite3
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import NamedTuple

from taiwan_stock.portfolio.replay import PositionResult

TICK_SCHEDULE: list[tuple[Decimal | None, Decimal]] = [
    (Decimal("10"), Decimal("0.01")),
    (Decimal("50"), Decimal("0.05")),
    (Decimal("100"), Decimal("0.1")),
    (Decimal("500"), Decimal("0.5")),
    (Decimal("1000"), Decimal("1")),
    (None, Decimal("5")),
]

STATUTORY_COMMISSION_RATE = Decimal("0.001425")
STATUTORY_TAX_RATE_STOCK = Decimal("0.003")
STATUTORY_TAX_RATE_ETF = Decimal("0.001")
ILLUSTRATIVE_MIN_COMMISSION = Decimal("20")


def tick_size(price: Decimal, tick_schedule=None) -> Decimal:
    for threshold, size in TICK_SCHEDULE if tick_schedule is None else tick_schedule:
        if threshold is None or price < threshold:
            return size
    raise AssertionError("unreachable: last TICK_SCHEDULE entry has threshold None")


def round_down_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if price <= 0:
        return tick
    steps = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return steps * tick


def _round_currency(amount: Decimal) -> Decimal:
    """Whole-NTD, rounded down. Real brokers' rounding rules can
    differ slightly — this is a documented simplification, not a claim
    every broker rounds identically (design 002 §4.9)."""
    return amount.quantize(Decimal("1"), rounding=ROUND_DOWN)


def gross_amount(price: Decimal, quantity: Decimal) -> Decimal:
    """Auto-computed 成交價金, whole NTD rounded down (issue #75). Only
    odd lots can carry cents (a 0.01 tick × 1,000 shares is always whole);
    no official settlement rounding rule was found, so this follows the
    same round-down convention as fees and tax."""
    return _round_currency(price * quantity)


def whole_twd(amount: Decimal) -> Decimal:
    """Estimated NTD figures (market value, cost, P&L) for display —
    nearest whole NTD, half up. Not a cash flow, so no settlement rule
    applies; whole-NTD output just matches what an account can hold."""
    return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


class FeeSchedule(NamedTuple):
    commission_rate: Decimal
    commission_discount: Decimal
    min_commission: Decimal
    tax_rate: Decimal

    def commission(self, price: Decimal, quantity: Decimal) -> Decimal:
        raw = price * quantity * self.commission_rate * self.commission_discount
        return max(_round_currency(raw), self.min_commission)

    def tax(
        self, price: Decimal, quantity: Decimal, *, day_trade_discount: bool = False
    ) -> Decimal:
        # Day-trade discount halves the *rate* before rounding, not the
        # rounded normal-rate tax — matches how the exchange computes it,
        # and avoids a double-rounding drift on the rare tick where that
        # would differ by a cent. Caller decides eligibility (stock vs
        # ETF, day-trade flag) — this class only knows the arithmetic.
        rate = self.tax_rate / 2 if day_trade_discount else self.tax_rate
        return _round_currency(price * quantity * rate)

    def net_proceeds(self, price: Decimal, quantity: Decimal) -> Decimal:
        gross = gross_amount(price, quantity)
        return gross - self.commission(price, quantity) - self.tax(price, quantity)


def default_fee_schedule(security_type: str) -> FeeSchedule:
    tax_rate = (
        STATUTORY_TAX_RATE_ETF if security_type == "etf" else STATUTORY_TAX_RATE_STOCK
    )
    return FeeSchedule(
        commission_rate=STATUTORY_COMMISSION_RATE,
        commission_discount=Decimal("1"),
        min_commission=ILLUSTRATIVE_MIN_COMMISSION,
        tax_rate=tax_rate,
    )


_SETTINGS_KEYS = {
    "commission_rate": "commission_rate",
    "commission_discount": "commission_discount",
    "min_commission": "min_commission",
}
_TAX_KEY = {"stock": "tax_rate_stock", "etf": "tax_rate_etf"}


def load_fee_schedule(conn: sqlite3.Connection, security_type: str) -> FeeSchedule:
    """Statutory/illustrative defaults, overridden by whatever the user
    has saved in `app_settings` (no settings page writes these yet —
    this just leaves the door open for one without requiring rework).
    """
    defaults = default_fee_schedule(security_type)
    overrides = {
        row["key"]: row["value"]
        for row in conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN (?, ?, ?, ?)",
            (
                *_SETTINGS_KEYS.values(),
                _TAX_KEY.get(security_type, _TAX_KEY["stock"]),
            ),
        )
    }
    return FeeSchedule(
        commission_rate=Decimal(
            overrides.get("commission_rate", defaults.commission_rate)
        ),
        commission_discount=Decimal(
            overrides.get("commission_discount", defaults.commission_discount)
        ),
        min_commission=Decimal(
            overrides.get("min_commission", defaults.min_commission)
        ),
        tax_rate=Decimal(
            overrides.get(
                _TAX_KEY.get(security_type, "tax_rate_stock"), defaults.tax_rate
            )
        ),
    )


def find_breakeven_price(
    quantity: Decimal,
    cost: Decimal,
    fee_schedule: FeeSchedule,
    max_price: Decimal = Decimal(100000),
    *,
    tick_schedule=None,
) -> Decimal | None:
    """Find the first legal tick without assuming rounded proceeds are monotone.

    For x=p*q, c=commission_rate*discount, t=tax_rate and m=minimum:
        net <= x*(1-t)-m+1 and net <= x*(1-c-t)+2.
    Each positive-slope envelope gives a necessary lower bound on p.
    Start at or below that bound in each tier, then inspect every tick.
    Nonpositive slopes never justify a lower bound; their upper envelope
    can instead prove that the entire remaining interval is impossible.
    """
    if quantity <= 0 or cost < 0 or max_price < Decimal("0.01"):
        return None
    c = fee_schedule.commission_rate * fee_schedule.commission_discount
    slopes = (
        Decimal(1) - fee_schedule.tax_rate,
        Decimal(1) - c - fee_schedule.tax_rate,
    )
    intercepts = (Decimal(1) - fee_schedule.min_commission, Decimal(2))
    lower_bound = Decimal(0)
    for slope, intercept in zip(slopes, intercepts, strict=True):
        if slope > 0:
            lower_bound = max(lower_bound, (cost - intercept) / slope / quantity)

    floor = Decimal("0.01")
    for ceiling, tick in TICK_SCHEDULE if tick_schedule is None else tick_schedule:
        # Round DOWN deliberately: the envelope is only a necessary bound,
        # and checking its preceding tick also avoids division-edge drift.
        candidate = max(floor, round_down_to_tick(lower_bound, tick))
        while candidate <= max_price and (ceiling is None or candidate < ceiling):
            x = candidate * quantity
            if any(
                slope <= 0 and x * slope + intercept < cost
                for slope, intercept in zip(slopes, intercepts, strict=True)
            ):
                return None
            if fee_schedule.net_proceeds(candidate, quantity) >= cost:
                return candidate
            candidate += tick
        if ceiling is None or ceiling > max_price:
            break
        floor = ceiling
    return None


def compute_breakeven(
    position: PositionResult, fee_schedule: FeeSchedule, *, tick_schedule=None
) -> dict:
    overall = None
    if position.remaining_cost is not None and position.remaining_quantity > 0:
        overall = find_breakeven_price(
            position.remaining_quantity,
            position.remaining_cost,
            fee_schedule,
            tick_schedule=tick_schedule,
        )

    lots = []
    for lot in position.lots:
        price = None
        if lot.cost is not None and lot.adjusted_quantity > 0:
            price = find_breakeven_price(
                lot.adjusted_quantity,
                lot.cost,
                fee_schedule,
                tick_schedule=tick_schedule,
            )
        lots.append({"trade_id": lot.trade_id, "breakeven_price": price})

    return {"overall_breakeven_price": overall, "lots": lots}
