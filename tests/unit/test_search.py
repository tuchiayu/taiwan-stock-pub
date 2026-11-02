"""Unit tests for securities search filters (design 002 §4.3.2)."""

import pytest

from taiwan_stock.catalog import search
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


def _insert_security(conn, id_, code, name, security_type, industry=None):
    conn.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, industry, status) "
        "VALUES (?, 'TWSE', ?, ?, ?, ?, 'active')",
        (id_, code, name, security_type, industry),
    )


def _insert_etf_profile(
    conn, security_id, custodian_bank=None, distribution_frequency=None
):
    conn.execute(
        "INSERT INTO etf_profiles "
        "(security_id, custodian_bank, distribution_frequency) VALUES (?, ?, ?)",
        (security_id, custodian_bank, distribution_frequency),
    )


def test_search_by_industry(conn):
    _insert_security(conn, "s1", "2330", "台積電", "stock", industry="半導體業")
    _insert_security(conn, "s2", "2882", "國泰金", "stock", industry="金融業")

    results = search.search_securities(conn, industry="半導體業")

    assert [r["code"] for r in results] == ["2330"]


def test_search_by_security_type(conn):
    _insert_security(conn, "s1", "2330", "台積電", "stock")
    _insert_security(conn, "e1", "0050", "元大台灣50", "etf")

    results = search.search_securities(conn, security_type="etf")

    assert [r["code"] for r in results] == ["0050"]


def test_search_by_custodian_bank_only_matches_etfs_with_that_bank(conn):
    _insert_security(conn, "e1", "0050", "元大台灣50", "etf")
    _insert_security(conn, "e2", "0056", "元大高股息", "etf")
    _insert_security(conn, "s1", "2330", "台積電", "stock")
    _insert_etf_profile(conn, "e1", custodian_bank="玉山銀行")
    _insert_etf_profile(conn, "e2", custodian_bank="兆豐銀行")

    results = search.search_securities(conn, custodian_bank="玉山銀行")

    assert [r["code"] for r in results] == ["0050"]


def test_search_by_distribution_frequency(conn):
    _insert_security(conn, "e1", "0056", "元大高股息", "etf")
    _insert_security(conn, "e2", "0050", "元大台灣50", "etf")
    _insert_etf_profile(conn, "e1", distribution_frequency="quarterly")
    _insert_etf_profile(conn, "e2", distribution_frequency="annual")

    results = search.search_securities(conn, distribution_frequency="quarterly")

    assert [r["code"] for r in results] == ["0056"]


def test_custodian_bank_and_distribution_frequency_combine_with_and(conn):
    _insert_security(conn, "e1", "0056", "元大高股息", "etf")
    _insert_security(conn, "e2", "00878", "國泰永續高股息", "etf")
    _insert_etf_profile(
        conn, "e1", custodian_bank="玉山銀行", distribution_frequency="quarterly"
    )
    _insert_etf_profile(
        conn, "e2", custodian_bank="兆豐銀行", distribution_frequency="quarterly"
    )

    results = search.search_securities(
        conn, custodian_bank="玉山銀行", distribution_frequency="quarterly"
    )

    assert [r["code"] for r in results] == ["0056"]


def test_search_by_pay_month_matches_announced_distributions(conn):
    _insert_security(conn, "e1", "00878", "國泰永續高股息", "etf")
    _insert_security(conn, "e2", "0050", "元大台灣50", "etf")
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_status) "
        "VALUES ('d1', 'e1', '2026-08-18', '2026-09-11', 'final')"
    )
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_status) "
        "VALUES ('d2', 'e2', '2026-08-01', '2026-08-20', 'final')"
    )

    results = search.search_securities(conn, pay_month="2026-09")

    assert [r["code"] for r in results] == ["00878"]


def test_search_by_pay_month_excludes_events_with_unannounced_pay_date(conn):
    # Design 004 §4.7.3: an event whose date is known but pay_date
    # isn't yet announced must not surface under any month filter —
    # not the ex-dividend month, not "無" mistaken for a real month.
    _insert_security(conn, "e1", "00918", "大華優利高填息30", "etf")
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_status) "
        "VALUES ('d1', 'e1', '2026-09-18', NULL, 'unknown')"
    )

    assert search.search_securities(conn, pay_month="2026-09") == []


def test_search_by_pay_month_does_not_duplicate_multi_event_security(conn):
    _insert_security(conn, "e1", "00878", "國泰永續高股息", "etf")
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_status) "
        "VALUES ('d1', 'e1', '2026-06-01', '2026-06-15', 'final')"
    )
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_status) "
        "VALUES ('d2', 'e1', '2026-06-05', '2026-06-20', 'final')"
    )

    results = search.search_securities(conn, pay_month="2026-06")

    assert [r["code"] for r in results] == ["00878"]


def test_list_custodian_banks_returns_distinct_sorted_non_null_values(conn):
    _insert_security(conn, "e1", "0050", "元大台灣50", "etf")
    _insert_security(conn, "e2", "0056", "元大高股息", "etf")
    _insert_security(conn, "e3", "00878", "國泰永續高股息", "etf")
    _insert_etf_profile(conn, "e1", custodian_bank="玉山銀行")
    _insert_etf_profile(conn, "e2", custodian_bank="玉山銀行")
    _insert_etf_profile(conn, "e3", custodian_bank=None)

    assert search.list_custodian_banks(conn) == ["玉山銀行"]


def test_list_industries_returns_distinct_sorted_non_null_values(conn):
    _insert_security(conn, "s1", "2330", "台積電", "stock", industry="半導體業")
    _insert_security(conn, "s2", "2317", "鴻海", "stock", industry="電子業")
    _insert_security(conn, "s3", "2882", "國泰金", "stock", industry=None)

    assert search.list_industries(conn) == ["半導體業", "電子業"]
