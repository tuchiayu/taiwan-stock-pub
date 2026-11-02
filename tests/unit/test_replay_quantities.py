"""Design 069: one replay gives the same holdings as one replay per date."""

import random
from datetime import date, timedelta

import pytest

from taiwan_stock.portfolio import replay

START = date(2024, 1, 1)


def day(offset):
    return (START + timedelta(days=offset)).isoformat()


def history(rng):
    """Buys, openings, sells, splits and stock dividends, often on one day."""
    trades, actions = [], []
    for index in range(rng.randint(1, 40)):
        when = rng.randint(0, 60)
        kind = rng.choice(["buy", "buy", "opening", "sell"])
        quantity = rng.randint(1, 500)
        trades.append(
            {
                "id": f"t{index}",
                "trade_date": day(when),
                "created_at": f"2026-01-01T00:00:{rng.randint(0, 59):02d}.000Z",
                "trade_order": index,
                "event_type": kind,
                "quantity": quantity,
                "unit_price": "10",
                "executed_amount": None,
                "commission": None,
                "other_fees": None,
            }
        )
    for index in range(rng.randint(0, 3)):
        numerator, denominator = rng.choice([(2, 1), (3, 2), (11, 10), (10, 3)])
        actions.append(
            {
                "id": f"a{index}",
                "action_type": rng.choice(["split", "stock_dividend"]),
                "ex_date": day(rng.randint(0, 60)),
                "ratio_numerator": numerator,
                "ratio_denominator": denominator,
            }
        )
    return trades, actions


def consistent_histories(count):
    rng = random.Random(69)
    found = []
    while len(found) < count:
        trades, actions = history(rng)
        try:
            replay.replay_trades(trades, actions, as_of_date=day(61))
        except replay.OversellError:
            continue
        found.append((trades, actions))
    return found


@pytest.mark.parametrize("trades,actions", consistent_histories(200))
def test_quantities_match_a_replay_per_date(trades, actions):
    dates = [day(offset) for offset in range(-1, 63)]
    expected = {
        when: replay.replay_trades(trades, actions, as_of_date=when).remaining_quantity
        for when in dates
    }
    assert replay.quantities_at(trades, actions, dates) == expected


def test_dates_may_repeat_and_come_unsorted():
    [(trades, actions)] = consistent_histories(1)
    dates = [day(30), day(5), day(30), day(-10)]
    result = replay.quantities_at(trades, actions, dates)
    assert set(result) == {day(30), day(5), day(-10)}
    assert result[day(-10)] == 0


def test_an_oversell_raises_like_replay_trades():
    trades = [
        {
            "id": "s",
            "trade_date": day(1),
            "created_at": "2026-01-01T00:00:00.000Z",
            "trade_order": 1,
            "event_type": "sell",
            "quantity": 1,
            "unit_price": "10",
            "executed_amount": None,
            "commission": None,
            "other_fees": None,
        }
    ]
    with pytest.raises(replay.OversellError):
        replay.quantities_at(trades, [], [day(2)])
