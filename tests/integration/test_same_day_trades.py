"""Design 072: same-day sell-then-buy, and fractional lots on the homepage."""

import io
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import replay, trades


@pytest.fixture
def data_dir(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        conn.execute(
            "INSERT INTO securities(id,code,name,market,security_type,status) "
            "VALUES('TWSE:2330','2330','台積電','TWSE','stock','active')"
        )
    return tmp_path


def trade(kind, when, quantity=1000, price="500"):
    return {
        "broker_account_id": "a",
        "security_id": "TWSE:2330",
        "event_type": kind,
        "trade_date": when,
        "quantity": quantity,
        "unit_price": price,
    }


def test_a_csv_with_the_sell_listed_first_imports(data_dir):
    content = (
        "broker_account_name,security_code,market,event_type,trade_date,"
        "quantity,unit_price,is_day_trade\n"
        "券商,2330,TWSE,sell,2026-10-30,1000,505,1\n"
        "券商,2330,TWSE,buy,2026-10-30,1000,500,1\n"
    )
    with TestClient(create_app(data_dir)) as client:
        preview = client.post(
            "/api/v1/trade-imports/previews",
            files={"file": ("t.csv", io.BytesIO(content.encode()), "text/csv")},
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        confirm = client.post(
            "/api/v1/trade-imports",
            json={
                "preview_id": body["preview_id"],
                "plan_digest": body["plan_digest"],
                "include_rows": [],
            },
        )
        assert confirm.status_code in (200, 201), confirm.text
    with closing(get_connection(data_dir)) as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 2
        position = trades.compute_position(conn, "a", "TWSE:2330", "2026-10-30")
    assert position.remaining_quantity == 0


def test_a_sell_can_move_onto_the_day_of_a_later_entered_buy(data_dir):
    with closing(get_connection(data_dir)) as conn:
        cover = trades.create_trade(conn, trade("buy", "2026-10-28"))
        sell = trades.create_trade(conn, trade("sell", "2026-10-31"))
        trades.create_trade(conn, trade("buy", "2026-10-30"))
        trades.update_trade(conn, cover, {"trade_date": "2026-11-03"})
        trades.update_trade(conn, sell, {"trade_date": "2026-10-30"})
        position = trades.compute_position(conn, "a", "TWSE:2330", "2026-10-30")
    assert position.remaining_quantity == 0


def test_a_sell_entered_before_any_buy_exists_is_still_refused(data_dir):
    with closing(get_connection(data_dir)) as conn:
        with pytest.raises(replay.OversellError):
            trades.create_trade(conn, trade("sell", "2026-10-30"))


def test_fractional_lots_show_their_real_quantity(data_dir):
    with closing(get_connection(data_dir)) as conn:
        trades.create_trade(conn, trade("buy", "2026-01-05", quantity=1))
        trades.create_trade(conn, trade("buy", "2026-02-05", quantity=1))
        conn.execute(
            "INSERT INTO corporate_actions(id,security_id,action_type,ex_date,"
            "ratio_numerator,ratio_denominator,source) "
            "VALUES('s','TWSE:2330','split','2026-03-02',3,2,'manual')"
        )
        conn.execute(
            "INSERT INTO daily_prices(security_id,trade_date,close) "
            "VALUES('TWSE:2330','2026-10-30','600')"
        )
        lots = trades.compute_position(conn, "a", "TWSE:2330").lots
    assert [str(lot.adjusted_quantity) for lot in lots] == ["1.5", "1.5"]
    with TestClient(create_app(data_dir)) as client:
        page = client.get("/").text
    assert "· 1.5 股 ·" in page
    assert "· 2 股 ·" not in page
