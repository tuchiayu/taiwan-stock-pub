"""Receiving-bank and account scopes across calendar, navigation and API."""

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import income_calendar, trades
from taiwan_stock.portfolio.income_scope import ScopeOptions


@pytest.fixture
def calendar_client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "taiwan_stock.web.pages_income.today_taipei", lambda: "2026-09-20"
    )
    monkeypatch.setattr(
        "taiwan_stock.portfolio.distribution_entitlement.today_taipei",
        lambda: "2026-09-20",
    )
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        for account_id, name, bank, status in [
            ("taishin", "台新證券", "台新銀行", "active"),
            ("esun", "玉山證券", "華南商業銀行", "disabled"),
            ("unset", "未設定帳戶", "  ", "active"),
            ("raw", "自訂帳戶", "自訂:銀行 & 分行", "active"),
            ("empty", "未交易帳戶", "玉山銀行", "active"),
            ("hidden", "無歷史停用", "不應出現", "disabled"),
        ]:
            conn.execute(
                "INSERT INTO broker_accounts "
                "(id, name, dividend_bank_name, status) VALUES (?, ?, ?, ?)",
                (account_id, name, bank, status),
            )
        for security_id, code, amount in [
            ("s1", "0056", "1"),
            ("s2", "00878", "2"),
            ("s3", "00919", "3"),
        ]:
            conn.execute(
                "INSERT INTO securities "
                "(id, market, code, name, security_type, status) "
                "VALUES (?, 'TWSE', ?, ?, 'etf', 'active')",
                (security_id, code, f"測試 ETF {code}"),
            )
            conn.execute(
                "INSERT INTO distributions "
                "(id, security_id, ex_dividend_date, pay_date, "
                "amount_per_share, amount_status) "
                "VALUES (?, ?, '2026-09-01', '2026-09-15', ?, 'final')",
                (security_id, security_id, amount),
            )
        for dist_id, sec_id, ex_date, pay_date, amount in [
            ("unknown", "s3", "2026-09-25", "2026-09-30", None),
            ("unscheduled", "s3", "2026-09-02", None, "4"),
            ("october", "s1", "2026-09-03", "2026-10-01", "5"),
        ]:
            conn.execute(
                "INSERT INTO distributions (id, security_id, ex_dividend_date, "
                "pay_date, amount_per_share, amount_status) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dist_id,
                    sec_id,
                    ex_date,
                    pay_date,
                    amount,
                    "unknown" if amount is None else "final",
                ),
            )
        for account_id, sec_id, quantity in [
            ("taishin", "s1", 100),
            ("taishin", "s2", 100),
            ("taishin", "s3", 100),
            ("esun", "s3", 200),
            ("unset", "s1", 10),
            ("raw", "s1", 30),
        ]:
            trades.create_trade(
                conn,
                {
                    "broker_account_id": account_id,
                    "security_id": sec_id,
                    "event_type": "buy",
                    "trade_date": "2024-01-01",
                    "quantity": quantity,
                    "unit_price": "50",
                },
            )
        conn.executemany(
            "INSERT INTO dividend_bank_preferences VALUES (?, ?, ?)",
            [("taishin", "s2", "富邦銀行"), ("taishin", "s3", "華南銀行")],
        )
        conn.execute(
            "INSERT INTO etf_profiles (security_id, custodian_bank) "
            "VALUES ('s1', '保管銀行不應出現')"
        )
        conn.close()
        yield client


def test_account_includes_all_its_banks_and_filters_every_summary(calendar_client):
    page = calendar_client.get("/income-calendar?month=2026-09&filter=account:taishin")
    assert page.status_code == 200
    context = page.context
    assert {e["broker_account_id"] for e in context["entries"]} == {"taishin"}
    assert {e["bank"] for e in context["entries"]} == {
        "台新銀行",
        "富邦銀行",
        "華南銀行",
    }
    assert context["overview"]["gross"] == Decimal(600)
    assert context["overview"]["count"] == 4
    assert context["overview"]["unknown_count"] == 1
    assert context["overview"]["payment_day_count"] == 2
    assert len(context["unscheduled"]) == 1
    assert sum(d["gross"] or 0 for d in context["overview"]["payment_days"]) == 600
    assert 'id="income-scope"' in page.text
    assert page.text.count("<select ") == 1


def test_bank_spans_accounts_and_keeps_unknowns_separate(calendar_client):
    page = calendar_client.get("/income-calendar", params={"filter": "bank:華南銀行"})
    assert {e["broker_account_id"] for e in page.context["entries"]} == {
        "taishin",
        "esun",
    }
    overview = page.context["overview"]
    assert overview["gross"] == 900
    assert overview["count"] == 4
    assert overview["unknown_count"] == 2
    assert len(page.context["unscheduled"]) == 2
    items = calendar_client.get(
        "/api/v2/income-calendar",
        params={
            "scope": "bank",
            "bank": "華南商業銀行",
            "month": "2026-09",
        },
    ).json()["items"]
    assert len(items) == 4
    assert sum(Decimal(e["expectedGross"] or "0") for e in items) == 900
    assert {e["bank"] for e in items} == {"華南銀行", "華南商業銀行"}


def test_choices_survive_empty_month_and_include_disabled_history(calendar_client):
    page = calendar_client.get("/income-calendar?month=2027-01&filter=account:esun")
    assert page.context["overview"]["count"] == 0
    assert "本月沒有符合此查看範圍" in page.text
    options = page.context["scope_options"]
    assert {o.key for o in options.accounts} == {
        "taishin",
        "esun",
        "unset",
        "raw",
        "empty",
    }
    assert "已停用" in options.select("account:esun").label
    assert options.select("bank:華南商業銀行").value == "bank:華南銀行"
    assert len([o for o in options.banks if o.key == "華南銀行"]) == 1
    assert all(o.key != "保管銀行不應出現" for o in options.banks)


@pytest.mark.parametrize(
    "value,account",
    [
        ("bank-unset", "unset"),
        ("bank:自訂:銀行 & 分行", "raw"),
        ("bank: 台新銀行 ", "taishin"),
    ],
)
def test_unset_and_free_text_banks_are_queryable(calendar_client, value, account):
    page = calendar_client.get("/income-calendar", params={"filter": value})
    assert page.status_code == 200
    assert {e["broker_account_id"] for e in page.context["entries"]} == {account}
    query = parse_qs(urlsplit(page.context["next_url"]).query)
    assert query["filter"] == [page.context["scope"].value]


def test_navigation_preserves_scope_without_limiting_all_announcements(calendar_client):
    page = calendar_client.get("/income-calendar?month=2026-09&filter=account:taishin")
    for key in ["previous_url", "next_url", "today_url", "announcements_url"]:
        target = calendar_client.get(page.context[key])
        assert target.status_code == 200
        assert target.context["scope"].value == "account:taishin"
    announcements = calendar_client.get(page.context["announcements_url"])
    assert any(e["pay_date"] == "2026-10-01" for e in announcements.context["entries"])
    returned = calendar_client.get(announcements.context["calendar_url"])
    assert returned.context["month"] == "2026-09"
    assert returned.context["scope"].value == "account:taishin"
    cleared = calendar_client.get(page.context["clear_filter_url"])
    assert cleared.context["scope"].kind == "all"
    assert cleared.context["month"] == "2026-09"
    assert 'name="filter" value="account:taishin"' in page.text


@pytest.mark.parametrize(
    "params",
    [
        {"scope": "account"},
        {"scope": "bank"},
        {"scope": "invalid"},
        {"scope": "account", "account_id": "missing"},
        {"scope": "bank", "bank": "不存在"},
        {"account_id": "taishin"},
        {"scope": "bank", "bank": "華南銀行", "bank_unset": "true"},
        {"scope": "account", "account_id": "taishin", "bank": "華南銀行"},
        {"scope": "bank", "account_id": "taishin", "bank": "華南銀行"},
    ],
)
def test_api_rejects_missing_invalid_and_intersecting_scopes(calendar_client, params):
    assert (
        calendar_client.get("/api/v2/income-calendar", params=params).status_code == 422
    )


@pytest.mark.parametrize("value", ["account:", "account:hidden", "bank:", "bad"])
def test_page_rejects_invalid_scope_without_showing_all(calendar_client, value):
    page = calendar_client.get("/income-calendar", params={"filter": value})
    assert page.status_code == 422
    assert 'role="alert"' in page.text
    assert 'class="income-calendar"' not in page.text


def test_api_default_compatibility_and_unset_bank(calendar_client):
    v1 = calendar_client.get("/api/v1/income-calendar").json()["items"]
    v2 = calendar_client.get("/api/v2/income-calendar").json()["items"]
    assert len(v1) == len(v2) == 13
    assert {e["distribution_id"] for e in v1} == {e["distributionId"] for e in v2}
    items = calendar_client.get(
        "/api/v2/income-calendar",
        params={
            "scope": "bank",
            "bank_unset": "true",
        },
    ).json()["items"]
    assert {e["brokerAccountId"] for e in items} == {"unset"}


def test_changed_bank_setting_reclassifies_history(calendar_client, tmp_path):
    conn = get_connection(tmp_path)
    conn.execute("DELETE FROM dividend_bank_preferences WHERE security_id='s3'")
    selected = income_calendar.calendar_view(conn, filter_value="bank:華南銀行")
    assert {e["broker_account_id"] for e in selected} == {"esun"}
    conn.close()


def test_empty_database_has_only_all_option(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        page = client.get("/income-calendar")
        assert page.status_code == 200
        assert "<optgroup " not in page.text
        assert 'value="all"' in page.text


def test_bank_lookups_are_batched(calendar_client, tmp_path):
    conn = get_connection(tmp_path)
    queries = []
    conn.set_trace_callback(queries.append)
    options = ScopeOptions(conn)
    income_calendar.calendar_view(conn, options=options)
    assert len([q for q in queries if "FROM dividend_bank_preferences" in q]) == 1
    conn.close()
