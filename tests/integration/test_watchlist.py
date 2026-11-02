"""Integration tests for watchlist management (design 002 §4.12)."""

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import watchlist


@pytest.fixture
def test_setup(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            """
            INSERT INTO securities (
                id, market, code, name, security_type, industry,
                transfer_agent_name, status
            ) VALUES (
                'sec-2330', 'TWSE', '2330', '台積電', 'stock',
                '半導體業', '中國信託', 'active'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO securities (
                id, market, code, name, security_type, industry,
                transfer_agent_name, status
            ) VALUES (
                'sec-2317', 'TWSE', '2317', '鴻海', 'stock',
                '其他電子業', '福邦證券', 'active'
            )
            """
        )
        conn.commit()
        conn.close()
        yield tmp_path, client


def test_watchlist_repo_crud(test_setup):
    tmp_path, _ = test_setup
    conn = get_connection(tmp_path)

    # Initially empty
    assert watchlist.list_watchlist(conn) == []
    assert watchlist.get_watchlist_security_ids(conn) == set()

    # Add item
    watchlist.add_to_watchlist(conn, "sec-2330", note="重要權值股", target_price="1000")
    conn.commit()

    ids = watchlist.get_watchlist_security_ids(conn)
    assert ids == {"sec-2330"}

    items = watchlist.list_watchlist(conn)
    assert len(items) == 1
    assert items[0]["security_id"] == "sec-2330"
    assert items[0]["code"] == "2330"
    assert items[0]["name"] == "台積電"
    assert items[0]["note"] == "重要權值股"
    assert items[0]["target_price"] == "1000"

    # Update item
    watchlist.add_to_watchlist(conn, "sec-2330", note="目標價調升", target_price="1100")
    conn.commit()

    items = watchlist.list_watchlist(conn)
    assert len(items) == 1
    assert items[0]["note"] == "目標價調升"
    assert items[0]["target_price"] == "1100"

    # Remove item
    removed = watchlist.remove_from_watchlist(conn, "sec-2330")
    conn.commit()
    assert removed is True
    assert watchlist.list_watchlist(conn) == []
    assert watchlist.get_watchlist_security_ids(conn) == set()

    # Remove nonexistent item
    assert watchlist.remove_from_watchlist(conn, "sec-2330") is False

    conn.close()


def test_watchlist_api_endpoints(test_setup):
    _, client = test_setup

    # Empty
    res = client.get("/api/v1/watchlist")
    assert res.status_code == 200
    assert res.json() == []

    # Add via POST
    res = client.post(
        "/api/v1/watchlist/sec-2330",
        json={"note": "長期持有", "target_price": "950"},
    )
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "security_id": "sec-2330"}

    # Query
    res = client.get("/api/v1/watchlist")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    assert items[0]["security_id"] == "sec-2330"
    assert items[0]["code"] == "2330"
    assert items[0]["name"] == "台積電"
    assert items[0]["note"] == "長期持有"
    assert items[0]["target_price"] == "950"

    # Delete via DELETE
    res = client.delete("/api/v1/watchlist/sec-2330")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "removed": True}

    # Verify deleted
    res = client.get("/api/v1/watchlist")
    assert res.status_code == 200
    assert res.json() == []


def test_watchlist_web_page_flow(test_setup):
    _, client = test_setup

    # Search on /securities (view=all with query q=2330)
    res = client.get(
        "/securities", params={"security_type": "stock", "view": "all", "q": "2330"}
    )
    assert res.status_code == 200
    assert "加入追蹤" in res.text

    # Add to watchlist via form POST
    add_res = client.post(
        "/watchlist/add",
        data={
            "security_id": "sec-2330",
            "return_to": "/securities?security_type=stock&view=watchlist",
        },
        follow_redirects=True,
    )
    assert add_res.status_code == 200
    assert "已加入追蹤清單" in add_res.text
    assert "台積電" in add_res.text
    assert "取消追蹤" in add_res.text

    # Check search with q=2330 shows 取消追蹤
    res_2330 = client.get(
        "/securities", params={"security_type": "stock", "view": "all", "q": "2330"}
    )
    assert res_2330.status_code == 200
    assert "取消追蹤" in res_2330.text

    # Check search with q=2317 shows 加入追蹤
    res_2317 = client.get(
        "/securities", params={"security_type": "stock", "view": "all", "q": "2317"}
    )
    assert res_2317.status_code == 200
    assert "加入追蹤" in res_2317.text

    # Remove from watchlist via form POST
    remove_res = client.post(
        "/watchlist/remove",
        data={
            "security_id": "sec-2330",
            "return_to": "/securities?security_type=stock&view=watchlist",
        },
        follow_redirects=True,
    )
    assert remove_res.status_code == 200
    assert "已從追蹤清單移除" in remove_res.text
    assert "目前這個類型的追蹤清單中沒有商品" in remove_res.text


def _save_details(client, target_price, note, security_id="sec-2330"):
    return client.post(
        "/watchlist/details",
        data={
            "security_id": security_id,
            "target_price": target_price,
            "note": note,
        },
        follow_redirects=False,
    )


def test_detail_page_edits_and_clears_target_price_and_note(test_setup):
    tmp_path, client = test_setup
    assert "追蹤設定" not in client.get("/securities/sec-2330").text
    client.post("/watchlist/add", data={"security_id": "sec-2330"})
    page = client.get("/securities/sec-2330").text
    assert 'id="watch-settings"' in page and 'href="#watch-settings"' in page

    saved = _save_details(client, " 1050.5 ", "  逢低加碼  ")
    assert saved.status_code == 303
    assert saved.headers["location"] == "/securities/sec-2330?watchlist_saved=1"
    page = client.get(saved.headers["location"]).text
    assert "追蹤設定已儲存。" in page
    assert 'value="1050.5"' in page and 'value="逢低加碼"' in page
    conn = get_connection(tmp_path)
    item = watchlist.get_watchlist_item(conn, "sec-2330")
    assert (item["target_price"], item["note"]) == ("1050.5", "逢低加碼")

    _save_details(client, "", "   ")
    item = watchlist.get_watchlist_item(conn, "sec-2330")
    conn.close()
    assert (item["target_price"], item["note"]) == (None, None)


@pytest.mark.parametrize(
    ("target_price", "note", "error", "message"),
    [
        ("0", "", "price", "目標價須為大於 0"),
        ("-1", "", "price", "目標價須為大於 0"),
        ("1.234", "", "price", "目標價須為大於 0"),
        ("abc", "", "price", "目標價須為大於 0"),
        ("NaN", "", "price", "目標價須為大於 0"),
        ("Infinity", "", "price", "目標價須為大於 0"),
        ("100", "字" * 201, "note", "備註最多 200 字"),
    ],
)
def test_invalid_watchlist_details_are_not_saved(
    test_setup, target_price, note, error, message
):
    tmp_path, client = test_setup
    client.post("/watchlist/add", data={"security_id": "sec-2330"})
    _save_details(client, "900", "原備註")

    response = _save_details(client, target_price, note)

    assert response.headers["location"] == (
        f"/securities/sec-2330?watchlist_error={error}"
    )
    assert message in client.get(response.headers["location"]).text
    conn = get_connection(tmp_path)
    item = watchlist.get_watchlist_item(conn, "sec-2330")
    conn.close()
    assert (item["target_price"], item["note"]) == ("900", "原備註")


def test_watchlist_details_never_start_tracking(test_setup):
    tmp_path, client = test_setup

    response = _save_details(client, "900", "不應寫入", security_id="sec-2317")

    assert response.headers["location"] == (
        "/securities/sec-2317?watchlist_error=untracked"
    )
    page = client.get(response.headers["location"]).text
    assert "此商品未在追蹤清單中" in page and "追蹤設定已儲存" not in page
    conn = get_connection(tmp_path)
    assert watchlist.get_watchlist_security_ids(conn) == set()
    conn.close()


def test_unknown_watchlist_error_code_is_not_echoed(test_setup):
    _, client = test_setup
    page = client.get(
        "/securities/sec-2330", params={"watchlist_error": "<script>"}
    ).text
    assert "<script>" not in page and 'class="notice error"' not in page
