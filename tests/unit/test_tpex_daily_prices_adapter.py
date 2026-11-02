"""Unit tests for TPEx daily prices adapter."""

from taiwan_stock.catalog import tpex_adapter


def test_tpex_normalize_daily_price_valid_row():
    row = {
        "Date": "1150911",
        "SecuritiesCompanyCode": "1240",
        "CompanyName": "茂生農經",
        "TradingShares": "11,705,520",
        "Close": "9.72",
    }
    normalized = tpex_adapter.normalize_daily_price(row)
    assert normalized == {
        "code": "1240",
        "trade_date": "2026-09-11",
        "close": "9.72",
        "volume": 11705520,
    }


def test_tpex_normalize_daily_price_skips_non_numeric_close():
    row = {
        "Date": "1150911",
        "SecuritiesCompanyCode": "1240",
        "TradingShares": "0",
        "Close": "--",
    }
    assert tpex_adapter.normalize_daily_price(row) is None


def test_tpex_normalize_daily_price_skips_missing_code_or_date():
    assert (
        tpex_adapter.normalize_daily_price(
            {"Date": "1150911", "SecuritiesCompanyCode": "", "Close": "9.72"}
        )
        is None
    )
    assert (
        tpex_adapter.normalize_daily_price(
            {"Date": "", "SecuritiesCompanyCode": "1240", "Close": "9.72"}
        )
        is None
    )
    assert (
        tpex_adapter.normalize_daily_price(
            {"Date": "1150911", "SecuritiesCompanyCode": "1240", "Close": ""}
        )
        is None
    )


def test_tpex_normalize_daily_price_handles_missing_volume():
    row = {
        "Date": "1150911",
        "SecuritiesCompanyCode": "1240",
        "TradingShares": "",
        "Close": "9.72",
    }
    normalized = tpex_adapter.normalize_daily_price(row)
    assert normalized == {
        "code": "1240",
        "trade_date": "2026-09-11",
        "close": "9.72",
        "volume": None,
    }
