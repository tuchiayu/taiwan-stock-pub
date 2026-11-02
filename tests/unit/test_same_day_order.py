"""Design 072: same-day buys replay before same-day sells."""

import random
from datetime import date, timedelta

import pytest

from taiwan_stock.portfolio import replay
from taiwan_stock.portfolio.corporate_actions import AUTO_APPLIED_TYPES


def day(offset):
    return (date(2024, 1, 1) + timedelta(days=offset)).isoformat()


def trade(index, kind, when, quantity, second=0):
    return {
        "id": f"t{index}",
        "trade_date": day(when),
        "created_at": f"2026-01-01T00:00:{second:02d}.000Z",
        "trade_order": index,
        "event_type": kind,
        "quantity": quantity,
        "unit_price": "10",
        "executed_amount": None,
        "commission": "1",
        "other_fees": None,
    }


def entry_order_replay(trades, actions):
    """The previous order: same-day trades strictly as entered."""
    events = [
        ((a["ex_date"], 0, "", 0), a)
        for a in actions
        if a["action_type"] in AUTO_APPLIED_TYPES
    ]
    events += [
        ((t["trade_date"], 1, t["created_at"], t["trade_order"]), t) for t in trades
    ]
    events.sort(key=lambda event: event[0])
    lots = []
    for key, event in events:
        replay._step(lots, key[1], event)
    return replay._read_out(lots)


def history(rng):
    """Few dates, so most days carry several buys and sells."""
    trades = [
        trade(
            index,
            rng.choice(["buy", "opening", "sell", "sell"]),
            rng.randint(0, 8),
            rng.randint(1, 400),
            rng.randint(0, 59),
        )
        for index in range(rng.randint(2, 30))
    ]
    actions = [
        {
            "id": f"a{index}",
            "action_type": rng.choice(["split", "stock_dividend"]),
            "ex_date": day(rng.randint(0, 8)),
            "ratio_numerator": numerator,
            "ratio_denominator": denominator,
        }
        for index, (numerator, denominator) in enumerate(
            rng.choice([(2, 1), (3, 2), (11, 10)]) for _ in range(rng.randint(0, 2))
        )
    ]
    return trades, actions


def histories_valid_in_entry_order(count):
    rng = random.Random(72)
    found = []
    while len(found) < count:
        trades, actions = history(rng)
        try:
            entry_order_replay(trades, actions)
        except replay.OversellError:
            continue
        found.append((trades, actions))
    return found


@pytest.mark.parametrize("trades,actions", histories_valid_in_entry_order(300))
def test_a_history_valid_in_entry_order_gives_the_same_lots(trades, actions):
    assert replay.replay_trades(
        trades, actions, as_of_date=day(9)
    ) == entry_order_replay(trades, actions)


def test_a_same_day_sell_entered_before_its_buy_replays():
    trades = [trade(1, "sell", 0, 1000, 1), trade(2, "buy", 0, 1000, 2)]
    with pytest.raises(replay.OversellError):
        entry_order_replay(trades, [])
    position = replay.replay_trades(trades, [], as_of_date=day(0))
    assert position.remaining_quantity == 0
    assert replay.quantities_at(trades, [], [day(0)]) == {day(0): 0}


def test_an_action_still_applies_before_the_days_trades():
    trades = [trade(1, "buy", 0, 100), trade(2, "buy", 1, 100), trade(3, "sell", 1, 50)]
    split = {
        "id": "s",
        "action_type": "split",
        "ex_date": day(1),
        "ratio_numerator": 2,
        "ratio_denominator": 1,
    }
    # 100 doubled to 200 on the ex-date, then the same-day buy and sell.
    position = replay.replay_trades(trades, [split], as_of_date=day(1))
    assert position.remaining_quantity == 250
