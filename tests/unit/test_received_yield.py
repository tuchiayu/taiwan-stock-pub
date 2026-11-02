"""Unit tests for the received dividend yield (design 029)."""

from decimal import Decimal

from taiwan_stock.portfolio.distribution_entitlement import EntitlementEntry
from taiwan_stock.portfolio.received_yield import compute

TODAY = "2026-10-17"  # window (2025-10-17, 2026-10-17], 365 days


def buy(trade_id, day, quantity, amount, event_type="buy"):
    return {
        "id": trade_id,
        "event_type": event_type,
        "trade_date": day,
        "created_at": f"{day}T00:00:00.000Z",
        "quantity": quantity,
        "unit_price": None,
        "executed_amount": amount,
        "commission": None,
        "other_fees": None,
    }


def entry(ex_date, gross, status="final", review=None):
    return EntitlementEntry(
        distribution_id=ex_date,
        broker_account_id="a",
        broker_account_name="測試",
        security_id="s",
        security_code="00999",
        security_name="ETF",
        ex_dividend_date=ex_date,
        pay_date=None,
        amount_per_share="1",
        amount_status=status,
        entitled_quantity=Decimal(1000),
        expected_gross=None if gross is None else Decimal(gross),
        is_future=False,
        reconciliation_status=review,
        reconciliation_note=None,
    )


QUARTERLY = [entry(d, "1000") for d in ("2026-01-16", "2026-04-16", "2026-07-16")]


def test_a_year_without_changes_equals_the_cost_yield():
    result = compute(
        [entry("2025-10-20", "1000"), *QUARTERLY],
        [buy("t1", "2025-01-02", 1000, "100000")],
        [],
        TODAY,
    )

    assert result.average_cost == Decimal("100000")
    assert result.received == Decimal("4000")
    assert result.value == Decimal("4.00")  # 4 × 1 × 1000 ÷ 100000
    assert result.label is None


def test_top_up_counts_only_for_the_days_it_was_invested():
    # The issue's example: 20萬 all year, 30萬 more for the last 92 days.
    result = compute(
        [entry("2026-07-16", "21000")],
        [
            buy("t1", "2025-01-02", 10000, "200000"),
            buy("t2", "2026-07-18", 15000, "300000"),
        ],
        [],
        TODAY,
    )

    assert result.average_cost.quantize(Decimal("0.01")) == Decimal("275616.44")
    assert result.value == Decimal("7.62")  # not 21000 ÷ 500000 = 4.20


def test_first_purchase_inside_the_window_counts_unheld_days_as_zero():
    result = compute(
        [entry("2026-07-16", "1000")],
        [buy("t1", "2026-07-18", 1000, "36500")],
        [],
        TODAY,
    )

    assert result.average_cost == Decimal("9200")  # 36500 × 92 ÷ 365


def test_partial_sell_lowers_the_cost_from_its_date():
    sell = {**buy("t2", "2026-04-17", 500, "60000"), "event_type": "sell"}
    sell.update(transaction_tax=None)

    result = compute(
        QUARTERLY, [buy("t1", "2025-01-02", 1000, "100000"), sell], [], TODAY
    )

    # 100000 for 181 days (2025-10-18 to 2026-04-16), then 50000 for 184.
    assert result.average_cost == (Decimal(100000) * 181 + Decimal(50000) * 184) / 365


def test_stock_dividend_does_not_change_the_cost():
    action = {
        "action_type": "stock_dividend",
        "ratio_numerator": 110,
        "ratio_denominator": 100,
        "ex_date": "2026-06-01",
    }

    result = compute(
        QUARTERLY, [buy("t1", "2025-01-02", 1000, "100000")], [action], TODAY
    )

    assert result.average_cost == Decimal("100000")


def test_unconfirmed_amounts_are_left_out_and_counted():
    result = compute(
        [
            *QUARTERLY,
            entry("2026-10-15", None, status="unknown"),
            entry("2025-10-20", "1000", review="needs_review"),
        ],
        [buy("t1", "2025-01-02", 1000, "100000")],
        [],
        TODAY,
    )

    assert result.received == Decimal("3000")
    assert result.value == Decimal("3.00")
    assert result.pending_count == 2
    assert result.label == "2 筆金額待確認"


def test_only_unconfirmed_amounts_show_no_value():
    result = compute(
        [entry("2026-10-15", None, status="unknown")],
        [buy("t1", "2025-01-02", 1000, "100000")],
        [],
        TODAY,
    )

    assert result.value is None
    assert result.label == "1 筆金額待確認"


def test_window_excludes_its_start_and_includes_today():
    result = compute(
        [entry("2025-10-17", "5000"), entry(TODAY, "1000")],
        [buy("t1", "2025-01-02", 1000, "100000")],
        [],
        TODAY,
    )

    assert result.received == Decimal("1000")


def test_no_ex_date_while_held_shows_why():
    result = compute([], [buy("t1", "2026-10-01", 1000, "100000")], [], TODAY)

    assert result.value is None
    assert result.label == "持有期間尚無除息"


def test_unknown_cost_shows_why():
    unknown = buy("t1", "2025-01-02", 1000, None, event_type="opening")

    result = compute(QUARTERLY, [unknown], [], TODAY)

    assert result.value is None
    assert result.label == "成本未知"


def test_zero_cost_does_not_divide_by_zero():
    free = buy("t1", "2025-01-02", 1000, "0", event_type="opening")

    result = compute(QUARTERLY, [free], [], TODAY)

    assert result.value is None
    assert result.label == "持有成本為 0，無法計算"
