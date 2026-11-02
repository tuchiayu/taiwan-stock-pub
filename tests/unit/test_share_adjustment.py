"""Design 059: per-share cash restated in today's shares across actions."""

from decimal import Decimal

import pytest

from taiwan_stock.catalog import query
from taiwan_stock.portfolio import yields

AS_OF = "2026-10-31"


def event(ex_date, amount="4"):
    return {
        "ex_dividend_date": ex_date,
        "amount_per_share": amount,
        "amount_status": "final",
        "reconciliation_status": None,
    }


def action(ex_date, numerator=None, denominator=None, kind="split"):
    return {
        "action_type": kind,
        "ex_date": ex_date,
        "ratio_numerator": numerator,
        "ratio_denominator": denominator,
    }


def adjusted(ex_date, *actions, amount="4"):
    return yields.adjust_for_actions([event(ex_date, amount)], actions, AS_OF)[0]


@pytest.mark.parametrize(
    "actions,expected",
    [
        ([action("2026-06-01", 4, 1)], Decimal("1")),
        ([action("2026-06-01", 1050, 1000, "stock_dividend")], Decimal("3.809524")),
        ([action("2026-03-01", 2, 1), action("2026-06-01", 2, 1)], Decimal("1")),
        ([action("2026-01-20", 4, 1)], Decimal("1")),
    ],
    ids=["split", "stock-dividend", "two-splits", "same-day"],
)
def test_amounts_before_an_action_are_restated(actions, expected):
    row = adjusted("2026-01-20", *actions)
    assert row["amount_per_share"] == expected
    assert row["original_amount_per_share"] == "4"
    assert row["share_adjusted"] and not row["unadjustable"]


@pytest.mark.parametrize(
    "ex_date,actions",
    [
        ("2026-07-01", [action("2026-06-01", 4, 1)]),
        ("2026-01-20", [action("2026-11-15", 4, 1)]),
        ("2026-01-20", []),
    ],
    ids=["paid-after-split", "split-after-valuation", "no-action"],
)
def test_amounts_outside_an_action_stay_as_announced(ex_date, actions):
    row = adjusted(ex_date, *actions)
    assert row["amount_per_share"] == "4"
    assert not row["share_adjusted"]


def test_unknown_amounts_are_not_restated():
    row = adjusted("2026-01-20", action("2026-06-01", 4, 1), amount=None)
    assert row["amount_per_share"] is None
    assert not row["share_adjusted"]


def test_whole_results_keep_a_plain_form():
    row = adjusted("2026-01-20", action("2026-06-01", 4, 1), amount="40")
    assert str(row["amount_per_share"]) == "10"


@pytest.mark.parametrize("kind", ["cash_capital_reduction", "other"])
def test_actions_without_a_ratio_cannot_be_restated(kind):
    assert adjusted("2026-01-20", action("2026-06-01", kind=kind))["unadjustable"]


def test_yield_hides_cash_that_crosses_an_unadjustable_action():
    rows = yields.adjust_for_actions(
        [event("2026-01-20")],
        [action("2026-06-01", kind="cash_capital_reduction")],
        AS_OF,
    )
    info = query.yield_summary(
        rows, "40", as_of=AS_OF, complete=True, frequency="annual"
    )
    assert info["known_yield"] is None and info["estimated_yield"] is None
    assert info["cash_label"] == info["quality_label"] == "股數異動無法換算"
    assert info["estimate_unavailable"] == "unadjustable"


def test_restated_yield_says_so():
    rows = yields.adjust_for_actions(
        [event("2026-01-20")], [action("2026-06-01", 4, 1)], AS_OF
    )
    info = query.yield_summary(
        rows, "40", as_of=AS_OF, complete=True, frequency="annual"
    )
    assert info["known_yield"] == Decimal("2.5")
    assert info["estimated_yield"] == Decimal("2.5")
    assert "已依股數異動換算" in info["notes"]
