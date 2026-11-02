"""Design 060: oversized numbers are rejected, and stored ones are isolated."""

import io
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import settings, trades

OUT_OF_RANGE = "金額或股數超出可計算範圍"


@pytest.fixture
def data_dir(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        conn.executemany(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES(?,'TWSE',?,?,'stock','active')",
            [("s", "2330", "台積電"), ("t", "2317", "鴻海")],
        )
    return tmp_path


@pytest.fixture
def client(data_dir):
    with TestClient(
        create_app(data_dir), base_url="http://127.0.0.1", raise_server_exceptions=False
    ) as client:
        yield client


def trade(**fields):
    return {
        "broker_account_id": "a",
        "security_id": "s",
        "event_type": "buy",
        "trade_date": "2026-10-30",
        "quantity": 100,
        **fields,
    }


def count(data_dir):
    with closing(get_connection(data_dir)) as conn:
        return conn.execute("SELECT count(*) FROM trades").fetchone()[0]


def test_api_rejects_oversized_numbers_and_the_home_page_stays_up(client, data_dir):
    """The 2026-10-31 code review case B3, now with the expected results."""
    for fields in (
        {"quantity": 2**63},
        {"unit_price": "1e30"},
        {"executed_amount": "1e30"},
    ):
        response = client.post("/api/v1/trades", json=trade(**fields))
        assert response.status_code == 422, (fields, response.text)
    assert count(data_dir) == 0
    assert client.get("/").status_code == 200


def test_fee_quote_rejects_an_oversized_price(client):
    response = client.post(
        "/api/v2/trade-fee-quotes",
        json={
            "brokerAccountId": "a",
            "securityId": "s",
            "eventType": "buy",
            "tradeDate": "2026-10-30",
            "quantity": 1000,
            "unitPrice": "1e30",
        },
    )
    assert response.status_code == 422
    assert "最多 6 位小數" in response.json()["detail"]


def test_csv_preview_names_the_bad_row(client, data_dir):
    content = (
        "broker_account_name,security_code,market,event_type,trade_date,quantity,unit_price\n"
        "券商,2330,TWSE,buy,2026-10-30,100,500\n"
        "券商,2330,TWSE,buy,2026-10-30,100,1e30\n"
    )
    response = client.post(
        "/api/v1/trade-imports/previews",
        files={"file": ("trades.csv", io.BytesIO(content.encode()), "text/csv")},
    )
    assert response.status_code == 422
    assert "3" in response.text and "最多 6 位小數" in response.text
    assert count(data_dir) == 0


def test_watchlist_api_and_sale_simulation_price_are_bounded(client):
    response = client.post("/api/v1/watchlist/s", json={"target_price": "1e30"})
    assert response.status_code == 422
    simulation = client.post(
        "/api/v1/sale-simulations",
        json={
            "broker_account_id": "a",
            "security_id": "s",
            "quantity": 1,
            "price": "1e30",
        },
    )
    assert simulation.status_code == 422
    assert "試算價格必須介於" in simulation.json()["detail"]


def test_settings_bound_minimum_commission(data_dir):
    with closing(get_connection(data_dir)) as conn:
        for value in ("1e30", "1000000.5", "20.1234567"):
            with pytest.raises(ValueError):
                settings.save_settings(conn, {"min_commission": value})
        assert (
            settings.save_settings(conn, {"min_commission": "1000000"})[
                "min_commission"
            ]
            == "1000000"
        )


@pytest.fixture
def stored_bad(data_dir):
    """An oversized cost written before these bounds existed."""
    with closing(get_connection(data_dir)) as conn:
        trades.create_trade(conn, trade(security_id="t", unit_price="100"))
        conn.execute(
            "INSERT INTO trades(id,broker_account_id,security_id,event_type,"
            "trade_date,quantity,executed_amount,created_at,trade_order) "
            "VALUES('bad','a','s','buy','2026-10-30',100,'1e30',"
            "'2026-10-30T00:00:00.000Z',99)"
        )
    return data_dir


def test_a_stored_oversized_trade_only_isolates_its_holding(client, stored_bad):
    for url in ("/", "/income-calendar", "/securities/s"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert OUT_OF_RANGE in response.text, url
        assert "/transactions/bad/edit" in response.text, url
    assert "鴻海" in client.get("/").text

    position = client.get(
        "/api/v1/positions", params={"broker_account_id": "a", "security_id": "s"}
    )
    assert position.status_code == 409
    sale = client.get(
        "/sale-simulation",
        params={"broker_account_id": "a", "security_id": "s", "quantity": 1},
    )
    assert sale.status_code == 200
    assert "請先修正交易" in sale.text


def test_fixing_the_trade_clears_the_warning(client, stored_bad):
    with closing(get_connection(stored_bad)) as conn:
        trades.update_trade(conn, "bad", {"executed_amount": "50000"})
    assert OUT_OF_RANGE not in client.get("/").text
