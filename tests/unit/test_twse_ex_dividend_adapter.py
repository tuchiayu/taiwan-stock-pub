"""Unit tests for ex-dividend notice normalization (no network).

Sample rows are real field values from a live call to
`/exchangeReport/TWT48U_ALL`, made while writing the adapter — see the
module docstring for how CashDividend/StockDividendRatio's noise was
found and characterized.
"""

from decimal import Decimal

from taiwan_stock.catalog import twse_adapter

TSMC_CASH_ONLY_ROW = {
    "Date": "1150916",
    "Code": "2330",
    "Name": "台積電",
    "Exdividend": "息",
    "StockDividendRatio": "",
    "CashDividend": "7.000001",
}

MIXED_ROW = {
    "Date": "1150908",
    "Code": "8473",
    "Name": "山林水",
    "Exdividend": "權息",
    "StockDividendRatio": "0.10592943",
    "CashDividend": "0.782345",
}

SUBSCRIPTION_ONLY_ROW = {
    "Date": "1150916",
    "Code": "6830",
    "Name": "汎銓",
    "Exdividend": "權",
    "StockDividendRatio": "",
    "SubscriptionRatio": "0.10963641",
    "CashDividend": "0",
}


def test_roc_date_conversion():
    assert twse_adapter._roc_date_to_iso("1150916") == "2026-09-16"


def test_cash_only_row_cleans_floating_point_noise():
    distribution, action = twse_adapter.normalize_ex_dividend_notice(TSMC_CASH_ONLY_ROW)

    assert distribution == {
        "code": "2330",
        "ex_dividend_date": "2026-09-16",
        "amount_per_share": "7.0000",
        "amount_status": "final",
    }
    assert action is None


def test_mixed_cash_and_stock_dividend_row_produces_both():
    distribution, action = twse_adapter.normalize_ex_dividend_notice(MIXED_ROW)

    assert distribution["amount_per_share"] == "0.7823"
    assert action["action_type"] == "stock_dividend"
    # ratio 1.10592943 preserved at 8 decimal places -> 110592943/100000000
    # (a 4-decimal quantum would truncate this to 1.1059, losing real precision)
    assert action["ratio_numerator"] == 110592943
    assert action["ratio_denominator"] == 100000000
    assert Decimal(action["ratio_numerator"]) / Decimal(
        action["ratio_denominator"]
    ) == Decimal("1.10592943")


def test_subscription_rights_only_row_is_skipped_not_guessed():
    distribution, action = twse_adapter.normalize_ex_dividend_notice(
        SUBSCRIPTION_ONLY_ROW
    )

    assert distribution is None
    assert action is None


def test_zero_cash_dividend_is_not_synced_as_a_real_distribution():
    row = {**TSMC_CASH_ONLY_ROW, "CashDividend": "0"}

    distribution, _ = twse_adapter.normalize_ex_dividend_notice(row)

    assert distribution is None


def test_non_numeric_placeholder_text_is_skipped_not_crashed_on():
    # Defensive: the TPEx sibling of this table has confirmed live
    # rows with "尚未公告" ("not yet announced") instead of a number.
    row = {**TSMC_CASH_ONLY_ROW, "CashDividend": "尚未公告"}

    distribution, action = twse_adapter.normalize_ex_dividend_notice(row)

    assert distribution is None
    assert action is None
