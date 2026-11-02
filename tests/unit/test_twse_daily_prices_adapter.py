"""Unit tests for TWSE daily prices adapter."""

from taiwan_stock.catalog import twse_adapter


def test_twse_normalize_daily_price_valid_row():
    row = {
        "Date": "1150910",
        "Code": "2330",
        "Name": "台積電",
        "TradeVolume": "25,000,000",
        "ClosingPrice": "1,050.50",
    }
    normalized = twse_adapter.normalize_daily_price(row)
    assert normalized == {
        "code": "2330",
        "trade_date": "2026-09-10",
        "close": "1050.50",
        "volume": 25000000,
    }


def test_twse_normalize_daily_price_skips_non_numeric_close():
    row = {
        "Date": "1150910",
        "Code": "00625K",
        "TradeVolume": "",
        "ClosingPrice": "--",
    }
    assert twse_adapter.normalize_daily_price(row) is None


def test_twse_normalize_daily_price_skips_missing_code_or_date():
    assert (
        twse_adapter.normalize_daily_price(
            {"Date": "1150910", "Code": "", "ClosingPrice": "10"}
        )
        is None
    )
    assert (
        twse_adapter.normalize_daily_price(
            {"Date": "", "Code": "2330", "ClosingPrice": "10"}
        )
        is None
    )
    assert (
        twse_adapter.normalize_daily_price(
            {"Date": "1150910", "Code": "2330", "ClosingPrice": ""}
        )
        is None
    )


def test_twse_normalize_daily_price_handles_missing_volume():
    row = {
        "Date": "1150910",
        "Code": "2330",
        "TradeVolume": "",
        "ClosingPrice": "1000",
    }
    normalized = twse_adapter.normalize_daily_price(row)
    assert normalized == {
        "code": "2330",
        "trade_date": "2026-09-10",
        "close": "1000",
        "volume": None,
    }
