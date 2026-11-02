"""User flows: correction, browsing, compatible preferences and restricted retry."""

import csv
import io
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trade_query
from taiwan_stock.web.routes_sync_jobs import get_job_launcher


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        with closing(get_connection(tmp_path)) as conn:
            conn.executemany(
                "INSERT INTO securities(id,market,code,name,security_type,status) "
                "VALUES(?,'TWSE',?,?,?,'active')",
                [
                    ("stock", "2330", "台積電", "stock"),
                    ("etf", "0050", "元大台灣50", "etf"),
                ],
            )
            conn.executemany(
                "INSERT INTO broker_accounts(id,name,status) VALUES(?,?,'active')",
                [("a", "帳戶 A"), ("b", "帳戶 B")],
            )
        yield client


def buy(client, account="a", security="stock", day="2026-01-02"):
    result = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account,
            "security_id": security,
            "event_type": "buy",
            "trade_date": day,
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    assert result.status_code == 201
    return result.json()["id"]


def fields(**changes):
    return {
        "broker_account_id": "a",
        "security_code": "2330",
        "event_type": "buy",
        "trade_date": "2026-01-03",
        "quantity": "1000",
        "unit_price": "50",
        **changes,
    }


def test_create_and_edit_preserve_every_attempted_value(client):
    trade_id = buy(client)
    attempted = fields(
        quantity="-1", unit_price="71.25", commission="8", note="<保留輸入>"
    )
    for path in ["/transactions", f"/transactions/{trade_id}/edit"]:
        response = client.post(path, data=attempted)
        assert response.status_code == 422
        assert 'value="-1"' in response.text
        assert 'value="71.25"' in response.text
        assert 'value="8"' in response.text
        assert 'value="&lt;保留輸入&gt;"' in response.text
        assert 'aria-invalid="true"' in response.text
        assert 'href="#trade-quantity"' in response.text
    with closing(get_connection(client.app.state.data_dir)) as conn:
        assert (
            conn.execute(
                "SELECT quantity FROM trades WHERE id=?", (trade_id,)
            ).fetchone()[0]
            == 1000
        )


@pytest.mark.parametrize(
    "changes,key",
    [
        ({"quantity": "abc"}, "quantity"),
        ({"trade_date": "2026-02-30"}, "trade_date"),
        ({"unit_price": "Infinity"}, "unit_price"),
        ({"event_type": "other"}, "event_type"),
        ({"broker_account_id": "missing"}, "broker_account_id"),
        ({"security_code": "missing"}, "security_code"),
    ],
)
def test_invalid_trade_is_correctable_html(client, changes, key):
    response = client.post("/transactions", data=fields(**changes))
    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"]
    assert f'id="trade-{key}-error"' in response.text


def test_enhanced_opening_ignores_inapplicable_stale_fees(client):
    response = client.post(
        "/transactions",
        data=fields(
            enhanced="1",
            event_type="opening",
            executed_amount="12345",
            commission="200",
            transaction_tax="300",
            other_fees="400",
            return_to="/",
        ),
    )
    assert response.status_code == 200
    assert "已儲存交易：2330 台積電" in response.text
    with closing(get_connection(client.app.state.data_dir)) as conn:
        row = conn.execute("SELECT * FROM trades").fetchone()
        assert row["executed_amount"] == "12345"
        assert row["unit_price"] is None
        assert row["commission"] is None
        assert row["transaction_tax"] is None


def test_edit_can_restore_automatic_fees_without_changing_json_contract(client):
    trade_id = buy(client)
    result = client.post(
        f"/transactions/{trade_id}/edit",
        data=fields(
            enhanced="1",
            quantity="2000",
            unit_price="100",
            commission="",
            return_to="/transactions?q=2330&page=2",
        ),
        follow_redirects=False,
    )
    assert result.status_code == 303
    returned = parse_qs(urlsplit(result.headers["location"]).query)
    assert returned["q"] == ["2330"]
    assert returned["page"] == ["2"]
    assert returned["saved"] == [trade_id]
    with closing(get_connection(client.app.state.data_dir)) as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        assert row["executed_amount"] == "200000"  # blank restores calculation
        assert row["commission"] == "285"


def test_prefill_and_filtered_return_links(client):
    buy(client)
    response = client.get("/transactions?new=1&security_code=2330&new_account_id=b")
    assert 'value="2330" required' in response.text
    assert 'value="b" selected' in response.text
    assert 'name="trade_date" value="' in response.text
    response = client.get("/transactions?q=2330&broker_account_id=a&page_size=25")
    assert "return_to=%2Ftransactions" in response.text
    assert "匯出符合條件的全部交易" in response.text
    assert client.get("/transactions?new=1&security_code=bad").status_code == 200
    assert "預填條件已失效" in client.get("/transactions?new=1&new_account_id=bad").text


@pytest.mark.parametrize(
    "target", ["//evil.test/", "https://evil.test/", "/\\evil.test", "https://["]
)
def test_post_return_stays_local(client, target):
    response = client.post(
        "/transactions", data=fields(return_to=target), follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/transactions?")


def test_filters_are_inclusive_and_export_ignores_pagination(client):
    for day in range(1, 29):
        buy(client, day=f"2026-01-{day:02}")
    buy(client, account="b")
    buy(client, security="etf")
    params = {
        "q": "台積電",
        "broker_account_id": "a",
        "date_from": "2026-01-01",
        "date_to": "2026-01-28",
        "event_type": "buy",
        "page_size": 25,
    }
    response = client.get("/transactions", params=params)
    assert response.status_code == 200
    assert "符合 28 筆" in response.text
    assert "第 1／2 頁" in response.text
    with closing(get_connection(client.app.state.data_dir)) as conn:
        state, errors = trade_query.parse_filters(conn, params)
        assert not errors
        first = trade_query.browse(conn, state, 1, 25)
        second = trade_query.browse(conn, state, 2, 25)
        assert len(first["transactions"]) == 25
        assert len(second["transactions"]) == 3
        assert not {t["id"] for t in first["transactions"]} & {
            t["id"] for t in second["transactions"]
        }
        assert trade_query.browse(conn, {}, page_size=999)["page_size"] == 100
    export = client.get("/transactions/export", params={**params, "page": 2})
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == 28
    assert {r["security_code"] for r in rows} == {"2330"}
    assert (
        len(list(csv.DictReader(io.StringIO(client.get("/transactions/export").text))))
        == 30
    )
    assert client.get("/transactions", params={"q": "%"}).status_code == 200
    assert "符合 0 筆" in client.get("/transactions", params={"q": "%"}).text


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": "2026-02-30"},
        {"date_from": "2026-02-01", "date_to": "2026-01-01"},
        {"broker_account_id": "missing"},
        {"event_type": "bad"},
    ],
)
def test_invalid_filters_do_not_export_unfiltered_data(client, params):
    buy(client)
    assert client.get("/transactions", params=params).status_code == 422
    response = client.get("/transactions/export", params=params)
    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"]


def test_display_preferences_roundtrip_and_preserve_invalid_input(client):
    values = {
        "form_format": "display",
        "commission_rate": "0.1425",
        "commission_discount": "6",
        "min_commission": "0",
        "tax_rate_stock": "0.3",
        "tax_rate_etf": "0.1",
    }
    result = client.post("/settings/preferences", data=values)
    assert result.status_code == 200
    assert "已儲存費用設定" in result.text
    stored = client.get("/api/v1/settings").json()
    assert stored["commission_discount"] == "0.6"
    assert stored["commission_rate"] == "0.001425"
    assert stored["min_commission"] == "0"
    invalid = client.post(
        "/settings/preferences",
        data={**values, "commission_discount": "11", "tax_rate_stock": "NaN"},
    )
    assert invalid.status_code == 422
    assert 'value="11"' in invalid.text
    assert 'value="NaN"' in invalid.text
    assert stored == client.get("/api/v1/settings").json()


def test_account_error_preserves_input_in_the_right_form(client):
    for account in ["a", "new"]:
        response = client.post(
            f"/settings/accounts/{account}",
            data={
                "name": " ",
                "dividend_bank_name": "玉山銀行",
                "remittance_fee_note": "保留備註",
                "status": "active",
            },
        )
        assert response.status_code == 422
        assert 'value="保留備註"' in response.text
        assert f'id="account-{account}-name-error"' in response.text
    response = client.post(
        "/settings/accounts/new", data={"name": "停用測試", "status": "disabled"}
    )
    assert response.status_code == 200
    with closing(get_connection(client.app.state.data_dir)) as conn:
        assert (
            conn.execute(
                "SELECT status FROM broker_accounts WHERE name='停用測試'"
            ).fetchone()[0]
            == "disabled"
        )


def seed_job(client, operation="prices", status="failed"):
    with closing(get_connection(client.app.state.data_dir)) as conn:
        conn.execute(
            "INSERT INTO sync_jobs(id,operation,origin,request_key,parameters,status,"
            "submitted_at,owner_token) VALUES('prior',?,'web','original','{}',?,"
            "'2026-01-01T00:00:00Z','owner')",
            (operation, status),
        )


@pytest.mark.parametrize(
    "operation",
    [
        "prices",
        "catalog",
        "stock-announcements",
        "held-etf-distributions",
        "investment-trust",
    ],
)
def test_retry_is_idempotent_and_keeps_scope(client, operation):
    seed_job(client, operation)

    def launcher(data_dir, job_id):
        sync_jobs.run_worker(
            data_dir,
            job_id,
            executor=lambda *args: (_ for _ in ()).throw(ValueError("測試來源失敗")),
        )

    client.app.dependency_overrides[get_job_launcher] = lambda: launcher
    results = [
        client.post(
            "/sync-jobs/prior/retry",
            data={"request_key": "retry-once"},
            follow_redirects=False,
        )
        for _ in range(2)
    ]
    assert results[0].status_code == results[1].status_code == 303
    assert results[0].headers["location"] == results[1].headers["location"]
    with closing(get_connection(client.app.state.data_dir)) as conn:
        rows = conn.execute(
            "SELECT operation,retry_of FROM sync_jobs WHERE retry_of='prior'"
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == (operation, "prior")
    assert (
        client.post(
            "/sync-jobs/prior/retry",
            data={"request_key": "x"},
            headers={"Origin": "https://evil.test"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "operation,status",
    [
        ("full-etf-distributions", "partial"),
        ("prices", "success"),
    ],
)
def test_retry_rejects_cli_only_or_completed_jobs(client, operation, status):
    seed_job(client, operation, status)
    assert (
        client.post(
            "/sync-jobs/prior/retry", data={"request_key": "blocked"}
        ).status_code
        == 409
    )
    with closing(get_connection(client.app.state.data_dir)) as conn:
        assert conn.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 1
    assert (
        client.post("/sync-jobs/missing/retry", data={"request_key": "x"}).status_code
        == 404
    )


def test_retry_conflict_has_link_and_does_not_submit(client, monkeypatch):
    seed_job(client)

    def conflict(*args, **kwargs):
        raise sync_jobs.Conflict("active-id")

    monkeypatch.setattr(sync_jobs, "retry", conflict)
    response = client.post("/sync-jobs/prior/retry", data={"request_key": "conflict"})
    assert response.status_code == 409
    assert 'href="/sync-jobs/active-id"' in response.text
    assert "其他同步正在執行" in response.text


def test_failed_create_keeps_filtered_history_context(client):
    buy(client)
    response = client.post(
        "/transactions",
        data=fields(
            quantity="bad",
            return_to="/transactions?q=2330&broker_account_id=a&page_size=25",
        ),
    )
    assert response.status_code == 422
    assert response.context["filters"]["q"] == "2330"
    assert response.context["filters"]["broker_account_id"] == "a"
    assert response.context["page_size"] == 25


def test_holdings_server_sort_and_fallback_preserve_raw_sort_values(client):
    buy(client)
    buy(client, security="etf")
    with closing(get_connection(client.app.state.data_dir)) as conn:
        conn.execute(
            "INSERT INTO daily_prices(security_id,trade_date,close) "
            "VALUES('stock','2026-10-01','0')"
        )
    response = client.get("/?sort=security&direction=ascending&display=cards")
    assert response.status_code == 200
    assert [row["security_code"] for row in response.context["positions"]] == [
        "0050",
        "2330",
    ]
    response = client.get("/?sort=market_value&direction=ascending")
    assert response.context["positions"][0]["security_code"] == "2330"
    assert 'data-sort="0"' in response.text or 'data-sort="0.00"' in response.text
    fallback = client.get("/?sort=missing&direction=bad&display=bad")
    assert fallback.context["holding_state"]["display"] == "auto"
    assert fallback.context["holding_state"]["direction"] == "descending"


def test_first_account_error_has_linked_field_and_preserves_name(client):
    with closing(get_connection(client.app.state.data_dir)) as conn:
        conn.execute("DELETE FROM broker_accounts")
    response = client.post("/broker-accounts", data={"name": " "})
    assert response.status_code == 422
    assert 'id="account-name-error"' in response.text
    assert 'aria-describedby="account-name-error"' in response.text
    assert 'value=" " required' in response.text
