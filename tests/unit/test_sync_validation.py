"""Design 062: strict source dates and honest sync outcome classes."""

import sqlite3

import pytest

from taiwan_stock.catalog import _parsing, sync_history


@pytest.mark.parametrize(
    "parse,value,expected",
    [
        (_parsing.roc_date_to_iso, "1150916", "2026-09-16"),
        (_parsing.roc_date_to_iso, "990101", "2010-01-01"),
        (_parsing.gregorian_yyyymmdd_to_iso, "20240229", "2024-02-29"),
        (_parsing.gregorian_yyyymmdd_to_iso, "", None),
        (_parsing.roc_dotted_date_to_iso, "115年08月18日", "2026-08-18"),
        (_parsing.roc_dotted_date_to_iso, "", None),
        (_parsing.roc_dotted_date_to_iso, "無收益分配", None),
        (_parsing.roc_slashed_date_to_iso, "115/09/24", "2026-09-24"),
        (_parsing.roc_slashed_date_to_iso, "日期", None),
    ],
)
def test_real_dates_and_blanks(parse, value, expected):
    assert parse(value) == expected


@pytest.mark.parametrize(
    "parse,value",
    [
        (_parsing.roc_date_to_iso, "1150230"),
        (_parsing.roc_date_to_iso, "1151301"),
        (_parsing.roc_date_to_iso, "abcdefg"),
        (_parsing.gregorian_yyyymmdd_to_iso, "abcdefgh"),
        (_parsing.gregorian_yyyymmdd_to_iso, "20250229"),
        (_parsing.gregorian_yyyymmdd_to_iso, "2025021"),
        (_parsing.roc_dotted_date_to_iso, "115年02月30日"),
        (_parsing.roc_slashed_date_to_iso, "114/02/29"),
    ],
)
def test_impossible_or_malformed_dates_raise(parse, value):
    with pytest.raises(_parsing.InvalidDate):
        parse(value)


def test_optional_dates_read_invalid_as_unknown():
    assert _parsing.optional_date(_parsing.roc_date_to_iso, "1150230") is None
    assert _parsing.optional_date(_parsing.roc_date_to_iso, None) is None
    assert _parsing.optional_date(_parsing.roc_date_to_iso, "1150916") == "2026-09-16"


def test_listed_raises_product_not_listed():
    assert sync_history.listed({"0050": 1}, "0050") == 1
    for catalog in ({"0050": 1}, None):
        with pytest.raises(sync_history.ProductNotListed):
            sync_history.listed(catalog, "00999")


@pytest.mark.parametrize(
    "exc,status,reason",
    [
        (sync_history.ProductNotListed("00999"), "not_listed", "官方名單未列出此商品"),
        (KeyError("Entries"), "parse_failed", "來源格式不符（缺少欄位 'Entries'）"),
        (
            sqlite3.OperationalError("database is locked"),
            "db_failed",
            "資料庫忙碌或無法寫入",
        ),
    ],
)
def test_error_info_classes(exc, status, reason):
    assert sync_history.error_info(exc)[0::2] == (status, reason)


def test_db_failures_are_transient_and_retried():
    assert sync_history.is_transient_failure(sync_history.DB_FAILED)
    assert sync_history.DB_FAILED not in sync_history.SETTLED
