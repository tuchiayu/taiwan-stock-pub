"""Unit tests for break-even pricing (design 002 §4.9).

Fee figures are hand-verified against the statutory formulas
(commission 0.1425%, stock tax 0.3%, floored/rounded down to whole
NTD) documented in pricing.py's module docstring.
"""

from decimal import Decimal

import pytest

from taiwan_stock.portfolio import pricing


def test_tick_size_schedule_boundaries():
    assert pricing.tick_size(Decimal("9.99")) == Decimal("0.01")
    assert pricing.tick_size(Decimal("10")) == Decimal("0.05")
    assert pricing.tick_size(Decimal("49.99")) == Decimal("0.05")
    assert pricing.tick_size(Decimal("50")) == Decimal("0.1")
    assert pricing.tick_size(Decimal("99.99")) == Decimal("0.1")
    assert pricing.tick_size(Decimal("100")) == Decimal("0.5")
    assert pricing.tick_size(Decimal("499.99")) == Decimal("0.5")
    assert pricing.tick_size(Decimal("500")) == Decimal("1")
    assert pricing.tick_size(Decimal("999.99")) == Decimal("1")
    assert pricing.tick_size(Decimal("1000")) == Decimal("5")
    assert pricing.tick_size(Decimal("5000")) == Decimal("5")


def test_stock_fee_schedule_matches_hand_calculation_at_the_tick_boundary():
    schedule = pricing.default_fee_schedule("stock")

    # 50,200: commission floor(50200*0.001425)=71, tax floor(50200*0.003)=150
    # -> net 49,979 (verified by hand in pricing.py's test derivation).
    assert schedule.net_proceeds(Decimal("50.20"), Decimal(1000)) == Decimal(49979)
    # 50,300 (one tick up — the 50-100 band's tick is 0.1, not 0.05):
    # commission floor(71.6775)=71, tax floor(150.9)=150 -> net 50,079.
    assert schedule.net_proceeds(Decimal("50.30"), Decimal(1000)) == Decimal(50079)


def test_gross_amount_drops_odd_lot_cents():
    # 37 × 15.55 = 575.35 -> 575 (issue #75); whole lots never carry cents.
    assert pricing.gross_amount(Decimal("15.55"), Decimal(37)) == Decimal(575)
    assert pricing.gross_amount(Decimal("15.55"), Decimal(1000)) == Decimal(15550)


def test_whole_twd_rounds_estimates_half_up():
    assert pricing.whole_twd(Decimal("575.35")) == Decimal(575)
    assert pricing.whole_twd(Decimal("40056.5")) == Decimal(40057)
    assert pricing.whole_twd(Decimal("-9.5")) == Decimal(-10)


def test_net_proceeds_starts_from_the_whole_ntd_gross():
    schedule = pricing.default_fee_schedule("stock")

    # gross floor(575.35)=575, commission max(floor(0.82), 20)=20,
    # tax floor(1.72605)=1 -> 554, not 554.35.
    assert schedule.net_proceeds(Decimal("15.55"), Decimal(37)) == Decimal(554)


def test_breakeven_price_for_the_hand_verified_case():
    schedule = pricing.default_fee_schedule("stock")

    price = pricing.find_breakeven_price(Decimal(1000), Decimal(50000), schedule)

    assert price == Decimal("50.30")
    # And the tick just below must NOT clear the cost, confirming this
    # is the minimum, not just "a" valid price.
    assert schedule.net_proceeds(Decimal("50.20"), Decimal(1000)) < Decimal(50000)


def test_etf_tax_rate_is_lower_so_breakeven_price_is_lower():
    stock_schedule = pricing.default_fee_schedule("stock")
    etf_schedule = pricing.default_fee_schedule("etf")

    stock_price = pricing.find_breakeven_price(
        Decimal(1000), Decimal(50000), stock_schedule
    )
    etf_price = pricing.find_breakeven_price(
        Decimal(1000), Decimal(50000), etf_schedule
    )

    assert etf_price < stock_price


def test_minimum_commission_floor_applies_at_low_prices():
    schedule = pricing.default_fee_schedule("stock")
    # At low quantity/price, 0.1425% of the trade is well under the
    # NT$20 floor, so the floor — not the rate — should determine cost.
    assert schedule.commission(Decimal("10"), Decimal(10)) == Decimal(20)


def test_day_trade_discount_halves_the_stock_tax_rate():
    schedule = pricing.default_fee_schedule("stock")
    # 50000 * 0.003 = 150 (normal); 50000 * 0.0015 = 75 (day trade) —
    # the rate is halved before rounding, not the rounded tax.
    assert schedule.tax(Decimal("50"), Decimal(1000)) == Decimal(150)
    assert schedule.tax(
        Decimal("50"), Decimal(1000), day_trade_discount=True
    ) == Decimal(75)


def test_day_trade_discount_is_the_callers_responsibility_for_etf():
    # ETF's own tax rate is already the 0.1% preferential rate; this
    # class has no opinion on whether day-trade discount is ETF-eligible
    # (it isn't, per design — the caller simply never passes True here).
    schedule = pricing.default_fee_schedule("etf")
    assert schedule.tax(Decimal("50"), Decimal(1000)) == Decimal(50)
    assert schedule.tax(
        Decimal("50"), Decimal(1000), day_trade_discount=True
    ) == Decimal(25)


@pytest.mark.parametrize("quantity,expected", [(100, "0.20"), (1000, "0.02")])
def test_zero_cost_breakeven_covers_minimum_commission(quantity, expected):
    schedule = pricing.default_fee_schedule("stock")
    quantity = Decimal(quantity)
    price = pricing.find_breakeven_price(quantity, Decimal(0), schedule)

    assert price == Decimal(expected)
    assert schedule.net_proceeds(price, quantity) >= 0
    assert schedule.net_proceeds(price - Decimal("0.01"), quantity) < 0


def test_zero_cost_without_fees_returns_smallest_positive_tick():
    schedule = pricing.FeeSchedule(*(Decimal(0) for _ in range(4)))
    assert pricing.find_breakeven_price(Decimal(100), Decimal(0), schedule) == Decimal(
        "0.01"
    )


@pytest.mark.parametrize("quantity", [Decimal(0), Decimal(-100)])
def test_non_positive_quantity_returns_none(quantity):
    schedule = pricing.default_fee_schedule("stock")

    assert pricing.find_breakeven_price(quantity, Decimal(1000), schedule) is None


def test_unreachable_breakeven_returns_none_instead_of_raising():
    # A fraction of a share can never cover the NT$20 minimum commission
    # below the search ceiling; this used to step 100,000 ticks and raise.
    schedule = pricing.default_fee_schedule("stock")

    assert (
        pricing.find_breakeven_price(Decimal("0.0001"), Decimal("4.29"), schedule)
        is None
    )


def test_compute_breakeven_skips_lots_and_overall_with_unknown_cost():
    from taiwan_stock.portfolio.replay import LotPosition, PositionResult

    position = PositionResult(
        remaining_quantity=Decimal(1000),
        remaining_cost=None,
        lots=[
            LotPosition(
                trade_id="A",
                trade_date="2024-01-01",
                original_remaining_quantity=Decimal(1000),
                adjusted_quantity=Decimal(1000),
                cost=None,
            )
        ],
    )
    schedule = pricing.default_fee_schedule("stock")

    result = pricing.compute_breakeven(position, schedule)

    assert result["overall_breakeven_price"] is None
    assert result["lots"][0]["breakeven_price"] is None


def test_compute_breakeven_computes_both_overall_and_per_lot():
    from taiwan_stock.portfolio.replay import LotPosition, PositionResult

    position = PositionResult(
        remaining_quantity=Decimal(1000),
        remaining_cost=Decimal(50000),
        lots=[
            LotPosition(
                trade_id="A",
                trade_date="2024-01-01",
                original_remaining_quantity=Decimal(1000),
                adjusted_quantity=Decimal(1000),
                cost=Decimal(50000),
            )
        ],
    )
    schedule = pricing.default_fee_schedule("stock")

    result = pricing.compute_breakeven(position, schedule)

    assert result["overall_breakeven_price"] == Decimal("50.30")
    assert result["lots"][0]["breakeven_price"] == Decimal("50.30")
