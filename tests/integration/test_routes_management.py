"""Integration tests for account/preference/receipt/trade-edit routes
added on top of the design 002 API (routes_management.py)."""

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as test_client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
        )
        conn.close()
        yield test_client


def _create_account(client, name="測試證券"):
    response = client.post("/api/v1/broker-accounts", json={"name": name})
    return response.json()["id"]


# --- /api/v1/settings ---------------------------------------------------


def test_get_settings_returns_defaults(client):
    response = client.get("/api/v1/settings")

    assert response.status_code == 200
    assert response.json()["min_commission"] == "20"


def test_patch_settings_updates_a_value(client):
    response = client.patch("/api/v1/settings", json={"min_commission": "5"})

    assert response.status_code == 200
    assert response.json()["min_commission"] == "5"
    assert client.get("/api/v1/settings").json()["min_commission"] == "5"


def test_patch_settings_rejects_invalid_value(client):
    response = client.patch("/api/v1/settings", json={"min_commission": "-1"})

    assert response.status_code == 422


def test_settings_preferences_form_persists_and_redirects(client):
    response = client.post(
        "/settings/preferences",
        data={
            "commission_rate": "0.001425",
            "commission_discount": "0.6",
            "min_commission": "20",
            "tax_rate_stock": "0.003",
            "tax_rate_etf": "0.001",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert client.get("/api/v1/settings").json()["commission_discount"] == "0.6"


def test_settings_preferences_form_rejects_invalid_value(client):
    response = client.post(
        "/settings/preferences",
        data={"commission_rate": "2"},
        follow_redirects=False,
    )

    assert response.status_code == 422


# --- broker account editing ----------------------------------------------


def test_patch_broker_account_updates_fields(client):
    account_id = _create_account(client)

    response = client.patch(
        f"/api/v1/broker-accounts/{account_id}",
        json={"dividend_bank_name": "玉山銀行"},
    )

    assert response.status_code == 200
    accounts = client.get("/api/v1/broker-accounts").json()["items"]
    assert accounts[0]["dividend_bank_name"] == "玉山銀行"


def test_patch_unknown_broker_account_returns_404(client):
    response = client.patch(
        "/api/v1/broker-accounts/does-not-exist", json={"dividend_bank_name": "x"}
    )

    assert response.status_code == 404


def test_patch_broker_account_rejects_invalid_status(client):
    account_id = _create_account(client)

    response = client.patch(
        f"/api/v1/broker-accounts/{account_id}", json={"status": "not-a-status"}
    )

    assert response.status_code == 422


def test_settings_accounts_form_creates_a_new_account(client):
    response = client.post(
        "/settings/accounts/new",
        data={"name": "新券商", "dividend_bank_name": "台新銀行"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    accounts = client.get("/api/v1/broker-accounts").json()["items"]
    assert any(a["name"] == "新券商" for a in accounts)


def test_settings_accounts_form_edits_an_existing_account(client):
    account_id = _create_account(client, name="舊名稱")

    response = client.post(
        f"/settings/accounts/{account_id}",
        data={
            "name": "改過的名稱",
            "dividend_bank_name": "",
            "remittance_fee_note": "",
            "status": "active",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    accounts = client.get("/api/v1/broker-accounts").json()["items"]
    assert accounts[0]["name"] == "改過的名稱"


def test_create_and_patch_broker_account_reject_whitespace_only_name(client):
    create_resp = client.post("/api/v1/broker-accounts", json={"name": "   "})
    assert create_resp.status_code == 422

    account_id = _create_account(client, name="正常名稱")
    patch_resp = client.patch(
        f"/api/v1/broker-accounts/{account_id}", json={"name": "   "}
    )
    assert patch_resp.status_code == 422


def test_settings_accounts_form_creates_disabled_account_directly(client):
    response = client.post(
        "/settings/accounts/new",
        data={"name": "停用券商", "status": "disabled"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Active-only API list excludes the newly created disabled account.
    accounts = client.get("/api/v1/broker-accounts").json()["items"]
    assert not any(a["name"] == "停用券商" for a in accounts)


# --- trade edit page -------------------------------------------------------


def test_edit_trade_page_shows_existing_values(client):
    account_id = _create_account(client)
    trade = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    ).json()

    response = client.get(f"/transactions/{trade['id']}/edit")

    assert response.status_code == 200
    assert 'value="1000"' in response.text


def test_edit_unknown_trade_page_returns_404(client):
    assert client.get("/transactions/does-not-exist/edit").status_code == 404


def test_edit_trade_form_updates_the_trade(client):
    account_id = _create_account(client)
    trade = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    ).json()

    response = client.post(
        f"/transactions/{trade['id']}/edit",
        data={
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": "2000",
            "unit_price": "50",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    updated = client.get(
        "/api/v1/trades",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    ).json()["items"][0]
    assert updated["quantity"] == 2000


def test_edit_trade_form_rejects_an_oversell_and_shows_the_error(client):
    account_id = _create_account(client)
    trade = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    ).json()
    client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "sell",
            "trade_date": "2024-02-01",
            "quantity": 1000,
        },
    )

    response = client.post(
        f"/transactions/{trade['id']}/edit",
        data={
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": "500",  # now less than the 1000 already sold later
            "unit_price": "50",
        },
    )

    assert response.status_code == 422
    assert "shares" in response.text  # OversellError's message, shown verbatim


# --- income calendar JSON API ---------------------------------------------


def test_income_calendar_api_returns_items(client):
    account_id = _create_account(client)
    client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    conn = get_connection(client.app.state.data_dir)
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES ('dist-1', 'sec-2330', '2024-06-01', '5', 'final')"
    )
    conn.close()

    response = client.get("/api/v1/income-calendar")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["expected_gross"] == "5000"


def test_income_calendar_api_rejects_bad_month_format(client):
    response = client.get("/api/v1/income-calendar", params={"month": "2024/06"})

    assert response.status_code == 422


def test_income_calendar_v1_freezes_received_fields_and_retires_preview(client):
    # Design 009 §4.7: v1 keeps its old shape (received/received_net_amount
    # always present) but no longer reads dividend_receipts, and preview
    # (the removed historical-frequency projection) now errors explicitly
    # instead of silently returning different data.
    account_id = _create_account(client)
    client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    conn = get_connection(client.app.state.data_dir)
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES ('dist-1', 'sec-2330', '2024-06-01', '5', 'final')"
    )
    conn.close()
    client.post(
        "/api/v1/dividend-receipts",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "distribution_id": "dist-1",
            "received_date": "2024-06-10",
            "net_amount": "4900",
        },
    )

    items = client.get("/api/v1/income-calendar").json()["items"]
    assert items[0]["received"] is False
    assert items[0]["received_net_amount"] is None

    preview_response = client.get("/api/v1/income-calendar", params={"preview": "true"})
    assert preview_response.status_code == 410


def test_income_calendar_v2_returns_camel_case_fields(client):
    account_id = _create_account(client)
    client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    conn = get_connection(client.app.state.data_dir)
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status, "
        " reconciliation_status, reconciliation_note) "
        "VALUES ('dist-1', 'sec-2330', '2024-06-01', '5', 'final', "
        " 'needs_review', '待覆核')"
    )
    conn.close()

    items = client.get("/api/v2/income-calendar").json()["items"]

    assert len(items) == 1
    assert items[0]["distributionId"] == "dist-1"
    assert items[0]["expectedGross"] == "5000"
    assert items[0]["reconciliationStatus"] == "needs_review"
    assert items[0]["reconciliationNote"] == "待覆核"
    assert "received" not in items[0]
    assert "broker_account_id" not in items[0]


# --- dividend receipts JSON API --------------------------------------------


def test_dividend_receipts_api_write_is_retired(client):
    # Design 009 §4.7: manual receipt entry is retired — no new writes,
    # explicit 410 rather than a silent 500 or a quietly accepted no-op.
    account_id = _create_account(client)

    response = client.post(
        "/api/v1/dividend-receipts",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "received_date": "2024-06-10",
            "net_amount": "4900",
        },
    )

    assert response.status_code == 410


def test_dividend_receipts_api_still_lists_historical_rows_for_compatibility(client):
    account_id = _create_account(client)
    conn = get_connection(client.app.state.data_dir)
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES ('dist-1', 'sec-2330', '2024-06-01', '5', 'final')"
    )
    conn.execute(
        "INSERT INTO dividend_receipts "
        "(id, broker_account_id, security_id, distribution_id, received_date, "
        " net_amount) "
        "VALUES ('r-1', ?, 'sec-2330', 'dist-1', '2024-06-10', '4900')",
        (account_id,),
    )
    conn.close()

    items = client.get("/api/v1/dividend-receipts").json()["items"]

    assert len(items) == 1
    assert items[0]["net_amount"] == "4900"
