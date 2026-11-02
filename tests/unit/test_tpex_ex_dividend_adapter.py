"""Unit tests for TPEx ex-dividend notice normalization (no network).

Sample rows are real field values from a live call to
`/openapi/v1/tpex_exright_prepost`, made while writing the adapter —
see the module docstring for how this table's field names differ from
TWSE's `TWT48U_ALL` (`CashDividend`/`StockDividendRatio` are the same,
only the date/code keys differ).
"""

from decimal import Decimal

from taiwan_stock.catalog import tpex_adapter

CASH_ONLY_ROW = {
    "ExRrightsExDividendDate": "1150901",
    "SecuritiesCompanyCode": "3078",
    "CompanyName": "僑威",
    "ExRrightsExDividend": "除息",
    "StockDividendRatio": "0.00000000",
    "CashDividend": "4.00000000",
}

STOCK_DIVIDEND_ROW = {
    "ExRrightsExDividendDate": "1150901",
    "SecuritiesCompanyCode": "4760",
    "CompanyName": "勤凱",
    "ExRrightsExDividend": "除權",
    "StockDividendRatio": "0.01993215",
    "CashDividend": "0.00000000",
}


def test_roc_date_conversion():
    assert tpex_adapter._roc_date_to_iso("1150901") == "2026-09-01"


def test_cash_only_row():
    distribution, action = tpex_adapter.normalize_ex_dividend_notice(CASH_ONLY_ROW)

    assert distribution == {
        "code": "3078",
        "ex_dividend_date": "2026-09-01",
        "amount_per_share": "4.0000",
        "amount_status": "final",
    }
    assert action is None


def test_stock_dividend_row_produces_a_corporate_action():
    distribution, action = tpex_adapter.normalize_ex_dividend_notice(STOCK_DIVIDEND_ROW)

    assert distribution is None
    assert action["code"] == "4760"
    assert action["action_type"] == "stock_dividend"
    # ratio 1.01993215 preserved at 8 decimal places -> 101993215/100000000
    # (a 4-decimal quantum would truncate this to 1.0199, losing real precision)
    assert action["ratio_numerator"] == 101993215
    assert action["ratio_denominator"] == 100000000
    assert Decimal(action["ratio_numerator"]) / Decimal(
        action["ratio_denominator"]
    ) == Decimal("1.01993215")


def test_zero_cash_dividend_is_not_synced_as_a_real_distribution():
    row = {**CASH_ONLY_ROW, "CashDividend": "0"}

    distribution, _ = tpex_adapter.normalize_ex_dividend_notice(row)

    assert distribution is None


def test_not_yet_announced_placeholder_text_is_skipped_not_crashed_on():
    # Real live row (2026-09): several bond-ETF rows carry "尚未公告"
    # ("not yet announced") in CashDividend instead of a number.
    row = {
        "ExRrightsExDividendDate": "1150915",
        "SecuritiesCompanyCode": "00942B",
        "CompanyName": "台新美A公司債20+",
        "StockDividendRatio": "0.00000000",
        "CashDividend": "尚未公告",
    }

    distribution, action = tpex_adapter.normalize_ex_dividend_notice(row)

    assert distribution is None
    assert action is None
