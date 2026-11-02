"""Unit tests for the partial-sell simulator (design 002 §4.9)."""

from decimal import Decimal

import pytest

from taiwan_stock.portfolio import pricing, simulate


def _buy(quantity, unit_price, commission="0", trade_date="2024-01-01"):
    return {
        "id": "A",
        "event_type": "buy",
        "trade_date": trade_date,
        "created_at": f"{trade_date}T00:00:00.000Z",
        "quantity": quantity,
        "unit_price": unit_price,
        "executed_amount": None,
        "commission": commission,
        "other_fees": None,
    }


def test_simulating_a_partial_sale_consumes_cost_proportionally():
    trades = [_buy(1000, "50")]  # cost 50,000
    schedule = pricing.default_fee_schedule("stock")

    result = simulate.simulate_partial_sale(
        trades, actions=[], quantity=800, fee_schedule=schedule, as_of_date="2024-06-01"
    )

    assert result.quantity == 800
    assert result.cost_consumed == Decimal(40000)  # 800/1000 * 50,000
    assert result.price is None
    assert result.net_proceeds is None
    assert result.realized_pnl is None
    assert result.breakeven_price is not None


def test_simulation_with_a_price_computes_net_proceeds_and_pnl():
    trades = [_buy(1000, "50")]
    schedule = pricing.default_fee_schedule("stock")

    result = simulate.simulate_partial_sale(
        trades,
        actions=[],
        quantity=800,
        fee_schedule=schedule,
        as_of_date="2024-06-01",
        price=Decimal("55"),
    )

    expected_net = schedule.net_proceeds(Decimal("55"), Decimal(800))
    assert result.net_proceeds == expected_net
    assert result.realized_pnl == expected_net - Decimal(40000)


def test_simulated_pnl_uses_the_whole_ntd_cost_shown():
    trades = [_buy(3, "10.15")]  # cost 30.45
    schedule = pricing.default_fee_schedule("stock")

    result = simulate.simulate_partial_sale(
        trades,
        actions=[],
        quantity=1,
        fee_schedule=schedule,
        as_of_date="2024-06-01",
        price=Decimal("11"),
    )

    assert result.cost_consumed == Decimal("10.15")
    assert result.net_proceeds == Decimal(-9)  # 11 - 20 minimum commission
    # -9 - 10 (cost shown as whole NTD), so the page's figures add up.
    assert result.realized_pnl == Decimal(-19)


def test_simulating_more_than_available_raises_without_touching_input():
    trades = [_buy(100, "50")]
    schedule = pricing.default_fee_schedule("stock")

    with pytest.raises(simulate.SimulationError, match="目前可賣 100 股"):
        simulate.simulate_partial_sale(
            trades,
            actions=[],
            quantity=200,
            fee_schedule=schedule,
            as_of_date="2024-06-01",
        )

    # The simulator must never mutate the caller's trade list.
    assert trades == [_buy(100, "50")]


def test_simulation_leaves_no_trace_in_a_second_call():
    trades = [_buy(1000, "50")]
    schedule = pricing.default_fee_schedule("stock")

    first = simulate.simulate_partial_sale(
        trades, actions=[], quantity=500, fee_schedule=schedule, as_of_date="2024-06-01"
    )
    second = simulate.simulate_partial_sale(
        trades, actions=[], quantity=500, fee_schedule=schedule, as_of_date="2024-06-01"
    )

    assert first == second


def test_unknown_cost_gives_none_instead_of_a_fake_number():
    opening = {
        "id": "A",
        "event_type": "opening",
        "trade_date": "2024-01-01",
        "created_at": "2024-01-01T00:00:00.000Z",
        "quantity": 1000,
        "unit_price": None,
        "executed_amount": None,
        "commission": None,
        "other_fees": None,
    }
    schedule = pricing.default_fee_schedule("stock")

    result = simulate.simulate_partial_sale(
        [opening],
        actions=[],
        quantity=500,
        fee_schedule=schedule,
        as_of_date="2024-06-01",
    )

    assert result.quantity == 500
    assert result.cost_consumed is None
    assert result.breakeven_price is None


def test_zero_or_negative_quantity_is_rejected():
    schedule = pricing.default_fee_schedule("stock")

    with pytest.raises(simulate.SimulationError):
        simulate.simulate_partial_sale(
            [_buy(1000, "50")],
            actions=[],
            quantity=0,
            fee_schedule=schedule,
            as_of_date="2024-06-01",
        )
