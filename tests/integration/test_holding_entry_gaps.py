"""Design 074: every entry point leaves out a foreign-currency holding, and
the sale page answers 404 for an unknown security."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import corporate_actions, simulate

FOREIGN = "TWSE:00636K"
TWD = "TWSE:0050"


@pytest.fixture
def data_dir(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        for index, (sid, code, name, fund_type) in enumerate(
            (
                (FOREIGN, "00636K", "元大中國A50+U", "加掛外幣"),
                (TWD, "0050", "元大台灣50", "國內成分證券指數股票型基金"),
            )
        ):
            conn.execute(
                "INSERT INTO securities(id,code,name,market,security_type,status) "
                "VALUES(?,?,?,'TWSE','etf','active')",
                (sid, code, name),
            )
            conn.execute(
                "INSERT INTO etf_profiles(security_id,issuer_name,benchmark,"
                "fund_tax_id,fund_type) VALUES(?,'元大投信','指數',?,?)",
                (sid, str(index), fund_type),
            )
            # Written directly: new foreign-currency trades are refused.
            conn.execute(
                "INSERT INTO trades(id,broker_account_id,security_id,event_type,"
                "trade_date,quantity,unit_price,created_at,trade_order) "
                "VALUES(?,'a',?,'buy','2026-10-01',100,'7.54',"
                "'2026-10-01T00:00:00.000Z',?)",
                (f"t{index}", sid, index),
            )
            conn.execute(
                "INSERT INTO corporate_actions(id,security_id,action_type,ex_date,"
                "source) VALUES(?,?,'other','2026-10-15','manual')",
                (f"ca{index}", sid),
            )
    return tmp_path


@pytest.fixture
def client(data_dir):
    return TestClient(create_app(data_dir), raise_server_exceptions=False)


def sale_page(client, security_id, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(
        f"/sale-simulation?broker_account_id=a&security_id={security_id}&{query}"
    )


def test_the_sale_page_does_not_simulate_a_foreign_currency_holding(client):
    response = sale_page(client, FOREIGN, quantity=10, price=8)
    assert response.status_code == 200
    assert simulate.FOREIGN_CURRENCY_MESSAGE in response.text
    assert "預估淨損益" not in response.text
    twd = sale_page(client, TWD, quantity=10, price=8)
    assert simulate.FOREIGN_CURRENCY_MESSAGE not in twd.text
    assert "預估淨損益" in twd.text


def test_the_position_api_refuses_a_foreign_currency_holding(client):
    response = client.get(
        f"/api/v1/positions?broker_account_id=a&security_id={FOREIGN}"
    )
    assert response.status_code == 409
    assert response.json()["detail"] == simulate.FOREIGN_CURRENCY_MESSAGE
    twd = client.get(f"/api/v1/positions?broker_account_id=a&security_id={TWD}")
    assert twd.status_code == 200
    assert twd.json()["remaining_quantity"] == "100"


def test_pending_actions_leave_out_a_foreign_currency_holding(data_dir):
    with closing(get_connection(data_dir)) as conn:
        pending = corporate_actions.list_unhandled_actions_for_held_securities(conn)
    assert [row["id"] for row in pending] == ["ca1"]


@pytest.mark.parametrize("params", [{"quantity": 10}, {}])
def test_the_sale_page_answers_404_for_an_unknown_security(client, params):
    assert sale_page(client, "NOPE", **params).status_code == 404
