"""Unit tests for the FIFO replay engine (design 002 §4.8, §4.9, §4.11)."""

from decimal import Decimal

import pytest

from taiwan_stock.portfolio import replay


def _trade(**overrides) -> dict:
    base = {
        "id": "t1",
        "event_type": "buy",
        "trade_date": "2024-01-01",
        "created_at": "2024-01-01T00:00:00.000Z",
        "quantity": 1000,
        "unit_price": None,
        "executed_amount": None,
        "commission": None,
        "other_fees": None,
    }
    base.update(overrides)
    return base


def test_design_4_9_worked_example_partial_sell():
    """Reproduces the §4.9 golden case verbatim: A 1,000@50+50 fee, B
    500@60+30 fee, sell 800 FIFO -> A left 200/10,010, B untouched
    500/30,030, total 700 shares / 40,040 cost.
    """
    lot_a = _trade(
        id="A",
        trade_date="2024-01-01",
        created_at="2024-01-01T00:00:00.000Z",
        quantity=1000,
        unit_price="50",
        commission="50",
    )
    lot_b = _trade(
        id="B",
        trade_date="2024-01-02",
        created_at="2024-01-02T00:00:00.000Z",
        quantity=500,
        unit_price="60",
        commission="30",
    )
    sell = _trade(
        id="S1",
        event_type="sell",
        trade_date="2024-06-01",
        created_at="2024-06-01T00:00:00.000Z",
        quantity=800,
    )

    result = replay.replay_trades(
        [lot_a, lot_b, sell], actions=[], as_of_date="2024-06-01"
    )

    assert result.remaining_quantity == Decimal(700)
    assert result.remaining_cost == Decimal(40040)
    lots_by_id = {lot.trade_id: lot for lot in result.lots}
    assert lots_by_id["A"].original_remaining_quantity == Decimal(200)
    assert lots_by_id["A"].cost == Decimal(10010)
    assert lots_by_id["B"].original_remaining_quantity == Decimal(500)
    assert lots_by_id["B"].cost == Decimal(30030)


def test_as_of_date_excludes_trades_dated_after_it():
    """A historical query must not be affected by trades that (from
    that date's perspective) haven't happened yet — this is the exact
    bug the income calendar's entitlement check caught: a trade dated
    after `as_of_date` must neither create nor consume a lot.
    """
    buy = _trade(id="A", trade_date="2024-01-01", quantity=1000, unit_price="50")
    later_sell = _trade(
        id="S1", event_type="sell", trade_date="2024-07-01", quantity=1000
    )

    as_of_before_sell = replay.replay_trades(
        [buy, later_sell], actions=[], as_of_date="2024-05-31"
    )
    as_of_after_sell = replay.replay_trades(
        [buy, later_sell], actions=[], as_of_date="2024-07-01"
    )

    assert as_of_before_sell.remaining_quantity == Decimal(1000)
    assert as_of_after_sell.remaining_quantity == Decimal(0)


def test_as_of_date_excludes_a_buy_dated_after_it():
    buy = _trade(id="A", trade_date="2024-06-01", quantity=1000, unit_price="50")

    result = replay.replay_trades([buy], actions=[], as_of_date="2024-05-31")

    assert result.remaining_quantity == Decimal(0)


def test_selling_more_than_available_raises_oversell():
    buy = _trade(id="A", quantity=100, unit_price="10")
    sell = _trade(id="S1", event_type="sell", trade_date="2024-02-01", quantity=200)

    with pytest.raises(replay.OversellError) as excinfo:
        replay.replay_trades([buy, sell], actions=[], as_of_date="2024-02-01")

    assert excinfo.value.trade_id == "S1"
    assert excinfo.value.shortfall == Decimal(100)


def test_split_after_holding_doubles_quantity_and_keeps_cost():
    buy = _trade(id="A", trade_date="2024-01-01", quantity=1000, unit_price="50")
    split = {
        "action_type": "split",
        "ex_date": "2024-03-01",
        "ratio_numerator": 2,
        "ratio_denominator": 1,
    }

    result = replay.replay_trades([buy], actions=[split], as_of_date="2024-06-01")

    assert result.remaining_quantity == Decimal(2000)
    assert result.remaining_cost == Decimal(50000)


def test_split_before_acquisition_does_not_apply():
    split = {
        "action_type": "split",
        "ex_date": "2024-01-01",
        "ratio_numerator": 2,
        "ratio_denominator": 1,
    }
    buy = _trade(id="A", trade_date="2024-06-01", quantity=1000, unit_price="50")

    result = replay.replay_trades([buy], actions=[split], as_of_date="2024-12-01")

    assert result.remaining_quantity == Decimal(1000)


def test_sell_after_split_consumes_post_split_quantity_correctly():
    """1,000 shares bought pre-split, 2-for-1 split, then sell 1,500 of
    the resulting 2,000 shares -> 500 post-split shares remain (25% of
    the post-split holding), carrying 25% of the original 50,000 cost
    = 12,500.
    """
    buy = _trade(id="A", trade_date="2024-01-01", quantity=1000, unit_price="50")
    split = {
        "action_type": "split",
        "ex_date": "2024-03-01",
        "ratio_numerator": 2,
        "ratio_denominator": 1,
    }
    sell = _trade(id="S1", event_type="sell", trade_date="2024-06-01", quantity=1500)

    result = replay.replay_trades([buy, sell], actions=[split], as_of_date="2024-06-01")

    assert result.remaining_quantity == Decimal(500)
    assert result.remaining_cost == Decimal(12500)


def test_unknown_cost_propagates_as_none_instead_of_zero():
    opening = _trade(id="A", event_type="opening", quantity=1000)

    result = replay.replay_trades([opening], actions=[], as_of_date="2024-01-01")

    assert result.remaining_quantity == Decimal(1000)
    assert result.remaining_cost is None


def test_opening_cost_excludes_commission_per_design():
    opening = _trade(
        id="A", event_type="opening", quantity=1000, executed_amount="50000"
    )
    # Commission on an opening entry must be ignored, not added, per
    # §4.8: "期初持股批次成本＝executed_amount（若已知）".
    opening["commission"] = "999"

    result = replay.replay_trades([opening], actions=[], as_of_date="2024-01-01")

    assert result.remaining_cost == Decimal(50000)


def test_cost_split_quantizes_to_cents_instead_of_leaking_division_precision():
    """Selling 1 of 3 shares is a non-terminating division (1/3 =
    0.333...); the split must land on a clean 2-decimal amount rather
    than a long or oddly-exponented Decimal, and consumed + remaining
    must still sum back to the original cost (allocation is exact even
    though the ratio isn't).
    """
    buy = _trade(id="A", quantity=3, unit_price="10")  # cost 30
    sell = _trade(id="S1", event_type="sell", trade_date="2024-02-01", quantity=1)

    result = replay.replay_trades([buy, sell], actions=[], as_of_date="2024-02-01")

    remaining_cost = result.lots[0].cost
    assert remaining_cost == remaining_cost.quantize(Decimal("0.01"))
    consumed = Decimal(30) - remaining_cost
    assert consumed + remaining_cost == Decimal(30)


def _stock_dividend(ex_date, numerator, denominator=1000):
    return {
        "action_type": "stock_dividend",
        "ex_date": ex_date,
        "ratio_numerator": numerator,
        "ratio_denominator": denominator,
    }


def test_stock_dividend_floors_the_fraction_and_keeps_cost():
    """Design 030: 1,009 shares × 1050/1000 = 1,059.45 — TDCC allots
    1,059 and pays the 0.45 in cash, so the holding is 1,059 with the
    whole cost, and selling those 1,059 leaves nothing behind."""
    buy = _trade(id="A", quantity=1009, unit_price="10", commission="20")
    dividend = _stock_dividend("2024-07-01", 1050)

    held = replay.replay_trades([buy], [dividend], as_of_date="2024-07-01")
    assert held.remaining_quantity == Decimal(1059)
    assert held.remaining_cost == Decimal(10110)
    assert held.lots[0].original_remaining_quantity == Decimal(1009)

    sell = _trade(id="S1", event_type="sell", trade_date="2024-08-01", quantity=1059)
    sold = replay.replay_trades([buy, sell], [dividend], as_of_date="2024-08-01")
    assert sold.remaining_quantity == Decimal(0)
    assert sold.remaining_cost == Decimal(0)
    assert sold.lots == []


def test_fraction_is_floored_on_the_account_total_from_the_newest_lot():
    # 333×1.05 = 349.65 and 777×1.05 = 815.85: per-lot flooring would
    # give 1,164, but the account total 1,165.5 floors to 1,165.
    lot_a = _trade(id="A", trade_date="2024-01-01", quantity=333, unit_price="10")
    lot_b = _trade(
        id="B",
        trade_date="2024-02-01",
        created_at="2024-02-01T00:00:00.000Z",
        quantity=777,
        unit_price="10",
    )

    result = replay.replay_trades(
        [lot_a, lot_b], [_stock_dividend("2024-07-01", 1050)], as_of_date="2024-07-01"
    )

    assert result.remaining_quantity == Decimal(1165)
    lots = {lot.trade_id: lot for lot in result.lots}
    assert lots["A"].adjusted_quantity == Decimal("349.65")
    assert lots["B"].adjusted_quantity == Decimal("815.35")
    assert result.remaining_cost == Decimal(11100)


def test_lot_trimmed_to_nothing_moves_its_cost_to_an_older_lot():
    lot_a = _trade(id="A", trade_date="2024-01-01", quantity=1, unit_price="10")
    lot_b = _trade(
        id="B",
        trade_date="2024-02-01",
        created_at="2024-02-01T00:00:00.000Z",
        quantity=1,
        unit_price="10",
    )
    split = {
        "action_type": "split",
        "ex_date": "2024-07-01",
        "ratio_numerator": 5,
        "ratio_denominator": 4,
    }

    result = replay.replay_trades([lot_a, lot_b], [split], as_of_date="2024-07-01")

    # 1.25 + 1.25 = 2.5 → 2: B gives up 0.25, then A the other 0.25.
    assert result.remaining_quantity == Decimal(2)
    assert result.remaining_cost == Decimal(20)


def test_non_terminating_split_ratio_does_not_cause_a_false_oversell():
    buy_a = _trade(id="A", trade_date="2024-01-01", quantity=100, unit_price="30")
    buy_b = _trade(
        id="B",
        trade_date="2024-02-01",
        created_at="2024-02-01T00:00:00.000Z",
        quantity=200,
        unit_price="30",
    )
    split = {
        "action_type": "split",
        "ex_date": "2024-03-01",
        "ratio_numerator": 10,
        "ratio_denominator": 3,
    }
    sell = _trade(id="S1", event_type="sell", trade_date="2024-04-01", quantity=1000)

    result = replay.replay_trades(
        [buy_a, buy_b, sell], [split], as_of_date="2024-04-01"
    )

    assert result.remaining_quantity == Decimal(0)
    assert result.lots == []


def test_ex_date_buy_is_not_adjusted_but_ex_date_sell_sees_the_adjustment():
    held = _trade(id="A", trade_date="2024-01-01", quantity=1000, unit_price="10")
    ex_date_buy = _trade(
        id="B",
        trade_date="2024-07-01",
        created_at="2024-07-01T00:00:00.000Z",
        quantity=100,
        unit_price="10",
    )
    ex_date_sell = _trade(
        id="S1",
        event_type="sell",
        trade_date="2024-07-01",
        created_at="2024-07-01T01:00:00.000Z",
        quantity=1050,
    )

    result = replay.replay_trades(
        [held, ex_date_buy, ex_date_sell],
        [_stock_dividend("2024-07-01", 1050)],
        as_of_date="2024-07-01",
    )

    assert result.remaining_quantity == Decimal(100)
    assert [lot.trade_id for lot in result.lots] == ["B"]


def test_consecutive_stock_dividends_floor_before_the_next_applies():
    buy = _trade(id="A", quantity=100, unit_price="10")

    result = replay.replay_trades(
        [buy],
        [_stock_dividend("2024-07-01", 1005), _stock_dividend("2025-07-01", 1005)],
        as_of_date="2025-07-01",
    )

    # 100.5 → 100, then 100.5 → 100 again (not 101.0025 → 101).
    assert result.remaining_quantity == Decimal(100)


def test_cash_capital_reduction_is_not_auto_applied():
    buy = _trade(id="A", trade_date="2024-01-01", quantity=1000, unit_price="50")
    reduction = {
        "action_type": "cash_capital_reduction",
        "ex_date": "2024-03-01",
        "ratio_numerator": 1,
        "ratio_denominator": 2,
    }

    result = replay.replay_trades([buy], actions=[reduction], as_of_date="2024-06-01")

    assert result.remaining_quantity == Decimal(1000)
