"""Independent tick enumeration catches rounded-proceeds dips (design 032)."""

from decimal import Decimal as D

import pytest

from taiwan_stock.portfolio import pricing


def legal_prices(limit):
    # Integer cents oracle, independent of production tick helpers.
    for lower, upper, step in [
        (1, 1000, 1),
        (1000, 5000, 5),
        (5000, 10000, 10),
        (10000, 50000, 50),
        (50000, 100000, 100),
        (100000, int(limit * 100) + 1, 500),
    ]:
        for cents in range(lower, min(upper, int(limit * 100) + 1), step):
            yield D(cents) / 100


def test_rounding_dip_does_not_hide_the_first_solution():
    fees = pricing.default_fee_schedule("stock")
    assert [fees.net_proceeds(D(p), D(1)) for p in ("333", "333.5", "334")] == [
        313,
        312,
        313,
    ]
    assert pricing.find_breakeven_price(D(1), D(313), fees) == D(333)


@pytest.mark.parametrize("quantity", ["0.01", "0.5", "1", "37", "1000"])
@pytest.mark.parametrize(
    "fees",
    [
        pricing.default_fee_schedule("stock"),
        pricing.default_fee_schedule("etf"),
        pricing.FeeSchedule(D(0), D(1), D(0), D(0)),
        pricing.FeeSchedule(D(".5"), D(1), D(0), D(".5")),
        pricing.FeeSchedule(D(".7"), D(".9"), D(".5"), D(".6")),
        pricing.FeeSchedule(D(".2"), D(".3"), D(2), D(".8")),
    ],
)
def test_minimum_matches_brute_force_for_costs_and_tier_boundaries(quantity, fees):
    quantity = D(quantity)
    limit = D("1015.1")  # Includes every tick-size boundary and an invalid upper tick.
    values = [(p, fees.net_proceeds(p, quantity)) for p in legal_prices(limit)]
    costs = {D(0), D(1), D(".5"), D(313), D(99999999)}
    for p in [
        D("9.99"),
        D(10),
        D("49.95"),
        D(50),
        D("99.9"),
        D(100),
        D("499.5"),
        D(500),
        D(999),
        D(1000),
    ]:
        n = fees.net_proceeds(p, quantity)
        costs.update([max(D(0), n), max(D(0), n + D(".1"))])
    for cost in costs:
        expected = next((p for p, net in values if net >= cost), None)
        assert pricing.find_breakeven_price(quantity, cost, fees, limit) == expected


def test_unprofitable_upper_endpoint_does_not_exclude_lower_solution():
    fees = pricing.FeeSchedule(D(".5"), D(1), D(0), D(".5"))
    assert fees.net_proceeds(D(100000), D(1)) == 0
    assert pricing.find_breakeven_price(D(1), D(1), fees) == D(1)


def test_max_price_is_a_hard_limit_even_when_it_is_not_a_legal_tick():
    fees = pricing.FeeSchedule(D(0), D(1), D(0), D(0))
    assert pricing.find_breakeven_price(D(1), D("50.1"), fees, D("50.15")) is None
    assert pricing.find_breakeven_price(D(1000), D(0), fees, D(".009")) is None
