"""Design 057: dated tax rules come from one table and warn near their end."""

from contextlib import closing
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trades, trading_rules
from taiwan_stock.portfolio.trading_rules import (
    DAY_TRADE_PERIODS,
    TAX_EXEMPT_PERIODS,
    RulePeriod,
    resolve_trading_rules,
)
from taiwan_stock.web import pages_holdings, pages_settings

EXPIRING = "被動式債券 ETF免證交稅至 2026-12-31，之後依當時已生效的法規計算。"
LAPSED = "被動式債券 ETF免證交稅已於 2026-12-31 屆滿，目前依一般稅率試算。"


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        conn.executemany(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES(?,'TWSE',?,?,?,'active')",
            [
                ("bond", "00679B", "債券", "etf"),
                ("etf", "0050", "台灣50", "etf"),
                ("stock", "2330", "台積電", "stock"),
            ],
        )
        yield conn


def security(conn, security_id):
    return conn.execute(
        "SELECT * FROM securities WHERE id=?", (security_id,)
    ).fetchone()


@pytest.mark.parametrize("periods", [TAX_EXEMPT_PERIODS, DAY_TRADE_PERIODS])
def test_periods_are_ordered_and_never_overlap(periods):
    for p in periods:
        assert p.start <= p.end
    for category in {p.category for p in periods}:
        same = sorted(
            (p for p in periods if p.category == category), key=lambda p: p.start
        )
        for earlier, later in pairwise(same):
            assert earlier.end < later.start


@pytest.mark.parametrize(
    "day,rule_id",
    [
        ("2016-12-31", "passive-bond-etf:general-tax"),
        ("2017-01-01", "passive-bond-etf:bond-exempt-2017-2026"),
        ("2026-12-31", "passive-bond-etf:bond-exempt-2017-2026"),
        ("2027-01-01", "passive-bond-etf:general-tax"),
    ],
)
def test_rule_id_is_unchanged_for_the_current_period(conn, day, rule_id):
    assert resolve_trading_rules(conn, security(conn, "bond"), day).rule_id == rule_id


@pytest.mark.parametrize(
    "day,expected",
    [
        ("2026-10-31", ()),
        ("2026-11-01", (EXPIRING,)),
        ("2026-12-31", (EXPIRING,)),
        ("2027-01-01", (LAPSED,)),
        ("2027-06-29", (LAPSED,)),
        ("2027-06-30", ()),
    ],
)
def test_bond_notice_window(conn, day, expected):
    assert resolve_trading_rules(conn, security(conn, "bond"), day).notices == expected


def test_notices_only_follow_the_security_category(conn):
    assert (
        resolve_trading_rules(conn, security(conn, "etf"), "2026-12-01").notices == ()
    )
    stock = resolve_trading_rules(conn, security(conn, "stock"), "2027-12-01")
    assert stock.notices == ("股票當沖減稅至 2027-12-31，之後依當時已生效的法規計算。",)
    assert stock.warnings == ()


def test_a_following_period_extends_the_rule_and_silences_the_notice(conn, monkeypatch):
    monkeypatch.setattr(
        trading_rules,
        "TAX_EXEMPT_PERIODS",
        (
            *TAX_EXEMPT_PERIODS,
            RulePeriod(
                "passive-bond-etf", "2027-01-01", "2036-12-31", "被動式債券 ETF", ""
            ),
            RulePeriod(
                "active-bond-etf", "2027-01-01", "2036-12-31", "主動式債券 ETF", ""
            ),
        ),
    )
    passive = resolve_trading_rules(conn, security(conn, "bond"), "2026-12-15")
    assert passive.notices == ()
    later = resolve_trading_rules(conn, security(conn, "bond"), "2027-01-01")
    assert later.fees.tax_rate == 0
    assert later.rule_id == "passive-bond-etf:bond-exempt-2027-2036"
    conn.execute("UPDATE securities SET code='00982D' WHERE id='bond'")
    active = resolve_trading_rules(conn, security(conn, "bond"), "2027-01-01")
    assert active.fees.tax_rate == 0


def quote(client, day):
    return client.post(
        "/api/v2/trade-fee-quotes",
        json={
            "brokerAccountId": "a",
            "securityId": "bond",
            "eventType": "buy",
            "tradeDate": day,
            "quantity": 1000,
            "unitPrice": "20",
        },
    )


def test_quote_api_adds_notices_without_changing_other_fields(conn, tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        before = quote(client, "2026-10-01").json()
        near = quote(client, "2026-12-01")
    assert near.status_code == 200, near.text
    assert before["notices"] == []
    assert near.json()["notices"] == [EXPIRING]
    assert set(near.json()) == set(before)
    assert {k for k in before if k != "notices"} == {
        "executedAmount",
        "commission",
        "transactionTax",
        "grossAmount",
        "netAmount",
        "ruleId",
        "ruleDate",
        "warnings",
    }


def test_sale_simulation_and_settings_show_the_notice_but_holdings_do_not(
    conn, tmp_path, monkeypatch
):
    trades.create_trade(
        conn,
        {
            "broker_account_id": "a",
            "security_id": "bond",
            "event_type": "buy",
            "trade_date": "2026-01-05",
            "quantity": 1000,
            "unit_price": "20",
        },
    )
    for module in (pages_holdings, pages_settings, trades):
        monkeypatch.setattr(module, "today_taipei", lambda: "2026-12-01")
    with TestClient(create_app(tmp_path)) as client:
        sale = client.get(
            "/sale-simulation", params={"broker_account_id": "a", "security_id": "bond"}
        )
        settings_page = client.get("/settings")
        home = client.get("/")
    assert EXPIRING in sale.text
    assert EXPIRING in settings_page.text
    assert (
        "被動式債券 ETF：2017-01-01～2026-12-31 免證交稅（證券交易稅條例第 2 條之 1）"
        in settings_page.text
    )
    assert home.status_code == 200
    assert EXPIRING not in home.text


def test_settings_lists_the_period_without_notice_far_from_its_end(
    conn, tmp_path, monkeypatch
):
    monkeypatch.setattr(pages_settings, "today_taipei", lambda: "2026-06-01")
    with TestClient(create_app(tmp_path)) as client:
        text = client.get("/settings").text
    assert "2017-01-01～2026-12-31 免證交稅" in text
    assert EXPIRING not in text
