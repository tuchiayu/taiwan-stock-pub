"""Design 060: shared bounds for share counts and money."""

from decimal import Decimal

import pytest

from taiwan_stock.portfolio import limits
from taiwan_stock.web import trade_forms


@pytest.mark.parametrize("value", [1, 10**12])
def test_quantity_inside_the_bounds(value):
    assert limits.check_quantity(value) == value


@pytest.mark.parametrize("value", [0, -1, 10**12 + 1, 2**63, True, "5", 1.0])
def test_quantity_outside_the_bounds(value):
    with pytest.raises(ValueError, match="1 到 1,000,000,000,000"):
        limits.check_quantity(value)


@pytest.mark.parametrize(
    "value,maximum",
    [
        ("10000000", limits.MAX_UNIT_PRICE),
        ("0", limits.MAX_UNIT_PRICE),
        ("12.345678", limits.MAX_UNIT_PRICE),
        ("1e3", limits.MAX_UNIT_PRICE),
        ("1000000000000000", limits.MAX_AMOUNT),
    ],
)
def test_amount_inside_the_bounds(value, maximum):
    assert limits.check_amount(value, maximum) == Decimal(value)


@pytest.mark.parametrize(
    "value,maximum",
    [
        ("10000000.000001", limits.MAX_UNIT_PRICE),
        ("1e30", limits.MAX_UNIT_PRICE),
        ("1.2345678", limits.MAX_UNIT_PRICE),
        ("-0.01", limits.MAX_UNIT_PRICE),
        ("NaN", limits.MAX_UNIT_PRICE),
        ("Infinity", limits.MAX_AMOUNT),
        ("abc", limits.MAX_AMOUNT),
        ("1000000000000000.000001", limits.MAX_AMOUNT),
    ],
)
def test_amount_outside_the_bounds(value, maximum):
    with pytest.raises(ValueError, match="最多 6 位小數"):
        limits.check_amount(value, maximum, "成交金額")


def test_price_times_quantity_is_bounded():
    limits.check_gross(Decimal(1000), 10**12)
    with pytest.raises(ValueError, match="請確認股數與單價"):
        limits.check_gross(Decimal(1001), 10**12)


def trade(**fields):
    base = {"quantity": 1000, "unit_price": "20", "executed_amount": None}
    return {**base, **fields}


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({}, None),
        ({"quantity": 2**63}, "quantity"),
        ({"unit_price": "1e30"}, "unit_price"),
        ({"unit_price": "10000000", "quantity": 10**9}, "unit_price"),
        ({"unit_price": None, "executed_amount": "1e30"}, "executed_amount"),
        ({"commission": "1.1234567"}, "commission"),
    ],
)
def test_trade_problem_names_the_first_bad_field(fields, expected):
    assert limits.trade_problem(trade(**fields)) == expected


def form(**fields):
    base = {"event_type": "buy", "trade_date": "2026-10-30", "quantity": "1000"}
    return {**base, **fields}


def test_form_errors_land_on_their_fields():
    errors = trade_forms.validate(
        form(quantity="9223372036854775808", executed_amount="1e30")
    )
    assert errors["quantity"].startswith("股數必須是 1 到")
    assert errors["executed_amount"].startswith("成交金額／期初總成本必須介於 0 到")
    gross = trade_forms.validate(form(quantity=str(10**12), unit_price="2000"))
    assert gross == {
        "unit_price": f"成交金額超過 {limits.MAX_AMOUNT:,}，請確認股數與單價。"
    }
    assert trade_forms.validate(form(unit_price="20.5")) == {}
