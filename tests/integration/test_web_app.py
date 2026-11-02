"""End-to-end tests through the ASGI app: form pages and the JSON API.

Each test builds its own isolated app via `create_app(tmp_path)` (see
main.py's docstring) so no test can see another's data, and none of
them can touch a real machine's default data directory.
"""

import re

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as test_client:
        # Seed one security directly — catalog sync is a separate
        # concern (Stage A), these tests only need something to trade.
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
        )
        conn.close()
        yield test_client


def test_homepage_shows_empty_state_with_no_trades(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "還沒有持股紀錄" in response.text


def test_shared_stylesheet_is_served_at_static_app_css(client):
    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "--canvas" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/static/app.css",
        "/static/workspace.css",
        "/static/income-calendar.css",
        "/static/holdings.css",
        "/static/holdings-table.js",
    ],
)
def test_indicators_are_drawn_shapes_not_text_glyphs(client, path):
    """Design 035: font fallback made these glyphs differ in size and weight."""
    text = client.get(path).text

    for glyph in "⌄›▸▾▲▼":
        assert glyph not in text


def test_shared_stylesheet_defines_indicator_shapes(client):
    css = client.get("/static/app.css").text

    assert "--icon-chevron:" in css
    assert "--icon-sort:" in css
    assert "summary::-webkit-details-marker { display: none; }" in css


def test_only_the_active_sort_column_draws_an_arrow(client):
    css = client.get("/static/app.css").text

    assert "th[aria-sort] .sort-indicator" in css
    assert "th:not([aria-sort])" not in css
    assert "--icon-sort-both" not in css


def test_include_delisted_link_shares_the_filter_buttons_line(client):
    """#163: centered against the labelled row it sat ~3px above the button."""
    css = client.get("/static/workspace.css").text
    rule = re.search(r"\.securities-filters > a \{([^}]*)\}", css).group(1)

    assert "min-height: 44px" in rule
    assert "align-items: center" in rule
    assert "align-self" not in rule and "margin-top" not in rule


def test_securities_sort_heading_marks_direction_without_glyph(client):
    page = client.get("/securities?security_type=stock&sort=code&direction=desc").text

    assert re.search(
        r'aria-sort="descending"[^>]*>\s*<a class="sort-heading"[^>]*>代碼'
        r'<span class="sort-indicator" aria-hidden="true"></span></a>',
        page,
    )
    assert "▲" not in page and "▼" not in page


def test_transactions_page_prompts_for_a_broker_account_first(client):
    response = client.get("/transactions")

    assert response.status_code == 200
    assert "還沒有券商帳戶" in response.text


def test_full_form_flow_create_account_then_trade_then_see_position(client):
    account_response = client.post(
        "/broker-accounts", data={"name": "測試證券"}, follow_redirects=False
    )
    assert account_response.status_code == 303

    trade_response = client.post(
        "/transactions",
        data={
            "broker_account_id": _first_account_id(client),
            "security_code": "2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": "1000",
            "unit_price": "50",
            "commission": "50",
        },
        follow_redirects=False,
    )
    assert trade_response.status_code == 303

    transactions_page = client.get("/transactions")
    assert "2330" in transactions_page.text
    assert "買進" in transactions_page.text

    homepage = client.get("/")
    assert "1000" in homepage.text
    assert "50,050" in homepage.text


def test_form_rejects_unknown_security_code_without_writing_a_trade(client):
    client.post("/broker-accounts", data={"name": "測試證券"}, follow_redirects=False)

    response = client.post(
        "/transactions",
        data={
            "broker_account_id": _first_account_id(client),
            "security_code": "9999",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": "100",
        },
    )

    assert response.status_code == 422
    assert "找不到商品代碼" in response.text
    assert 'id="new-trade" open' in response.text
    assert 'value="9999" required' in response.text


def test_form_rejects_oversell_and_shows_the_error(client):
    client.post("/broker-accounts", data={"name": "測試證券"}, follow_redirects=False)
    account_id = _first_account_id(client)
    client.post(
        "/transactions",
        data={
            "broker_account_id": account_id,
            "security_code": "2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": "100",
        },
        follow_redirects=False,
    )

    response = client.post(
        "/transactions",
        data={
            "broker_account_id": account_id,
            "security_code": "2330",
            "event_type": "sell",
            "trade_date": "2024-02-01",
            "quantity": "200",
        },
    )

    assert response.status_code == 409
    assert "only" in response.text  # OversellError message


def test_json_api_create_trade_and_read_position(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]

    created = client.post(
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
    assert created.status_code == 201

    position = client.get(
        "/api/v1/positions",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    )
    assert position.status_code == 200
    body = position.json()
    assert body["remaining_quantity"] == "1000"
    # 50000 (auto-filled executed_amount) + 71 (auto-filled commission,
    # floor(50000*0.001425)=71, above the NT$20 floor)
    assert body["remaining_cost"] == "50071"


def test_json_api_sell_with_day_trade_flag_gets_discounted_tax(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]
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

    sold = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "sell",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
            "is_day_trade": True,
        },
    )
    assert sold.status_code == 201

    items = client.get(
        "/api/v1/trades",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    ).json()["items"]
    sell_row = next(item for item in items if item["event_type"] == "sell")
    # floor(50000*0.0015) = 75, half the normal 0.3% stock rate.
    assert sell_row["transaction_tax"] == "75"
    assert sell_row["is_day_trade"] == 1


def test_json_api_list_and_update_trade(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]
    created = client.post(
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
    trade_id = created.json()["id"]

    listed = client.get(
        "/api/v1/trades",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    )
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    updated = client.patch(f"/api/v1/trades/{trade_id}", json={"quantity": 500})
    assert updated.status_code == 200

    position = client.get(
        "/api/v1/positions",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    )
    assert position.json()["remaining_quantity"] == "500"


def test_json_api_update_unknown_trade_returns_404(client):
    response = client.patch("/api/v1/trades/does-not-exist", json={"quantity": 1})

    assert response.status_code == 404


def test_json_api_positions_returns_409_on_inconsistent_stored_data(client, tmp_path):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]
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
    client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "sell",
            "trade_date": "2024-02-01",
            "quantity": 800,
        },
    )
    # Bypasses corporate_actions.create_corporate_action's validation
    # on purpose, to reproduce data written before it existed.
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO corporate_actions "
        "(id, security_id, action_type, ratio_numerator, "
        " ratio_denominator, ex_date) "
        "VALUES ('ca-1', 'sec-2330', 'split', 1, 4, '2024-01-15')"
    )
    conn.commit()
    conn.close()

    response = client.get(
        "/api/v1/positions",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    )

    assert response.status_code == 409


def test_json_api_oversell_returns_409(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]

    response = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "sell",
            "trade_date": "2024-01-01",
            "quantity": 100,
        },
    )

    assert response.status_code == 409


def test_json_api_securities_search(client):
    response = client.get("/api/v1/securities", params={"q": "2330"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["code"] == "2330"


def test_json_api_sale_simulation_does_not_create_a_trade(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]
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

    response = client.post(
        "/api/v1/sale-simulations",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "quantity": 800,
            "price": "55",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["quantity"] == 800
    # Lot cost is 50071 (50000 auto-filled executed_amount + 71
    # auto-filled commission); consuming 800/1000 of it is 40056.80. A
    # FIFO cost split always goes through Decimal division, so the
    # result is quantized to cent precision rather than leaving
    # whatever exponent the division happened to produce (see
    # replay.py's _COST_QUANTUM).
    assert body["cost_consumed"] == "40056.80"
    assert body["net_proceeds"] is not None
    assert body["realized_pnl"] is not None

    trades_after = client.get(
        "/api/v1/trades",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    ).json()["items"]
    assert len(trades_after) == 1  # only the original buy — no simulated row written

    position = client.get(
        "/api/v1/positions",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    ).json()
    assert position["remaining_quantity"] == "1000"  # untouched by the simulation


def test_json_api_sale_simulation_oversell_returns_422(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]

    response = client.post(
        "/api/v1/sale-simulations",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "quantity": 100,
        },
    )

    assert response.status_code == 422


def test_sale_simulation_page_shows_breakeven_without_writing_a_trade(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]
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

    response = client.get(
        "/sale-simulation",
        params={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "quantity": 800,
        },
    )

    assert response.status_code == 200
    assert "回本價" in response.text
    # cost consumed by the simulated 800 shares: 50071 (50000
    # auto-filled executed_amount + 71 auto-filled commission) * 0.8
    # = 40,056.8, shown as whole NTD (issue #75)
    assert "40,057" in response.text
    assert "40,056.8" not in response.text

    trades_after = client.get(
        "/api/v1/trades",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    ).json()["items"]
    assert len(trades_after) == 1


def test_sale_simulation_page_degrades_gracefully_on_inconsistent_stored_data(
    client, tmp_path
):
    account = client.post("/api/v1/broker-accounts", json={"name": "API 券商"})
    account_id = account.json()["id"]
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
    client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "sell",
            "trade_date": "2024-02-01",
            "quantity": 800,
        },
    )
    # Bypasses corporate_actions.create_corporate_action's validation
    # on purpose, to reproduce data written before it existed.
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO corporate_actions "
        "(id, security_id, action_type, ratio_numerator, "
        " ratio_denominator, ex_date) "
        "VALUES ('ca-1', 'sec-2330', 'split', 1, 4, '2024-01-15')"
    )
    conn.commit()
    conn.close()

    response = client.get(
        "/sale-simulation",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    )

    assert response.status_code == 200
    assert "持股資料不一致" in response.text


def test_corporate_action_form_split_immediately_affects_homepage(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "測試券商"})
    account_id = account.json()["id"]
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

    response = client.post(
        "/corporate-actions",
        data={
            "security_code": "2330",
            "action_type": "split",
            "ex_date": "2024-03-01",
            "from_shares": "1",
            "to_shares": "4",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    list_page = client.get("/corporate-actions")
    assert "4/1" in list_page.text
    assert "分割" in list_page.text

    homepage = client.get("/")
    assert "4000" in homepage.text  # 1000 shares x4 after the split


def test_corporate_action_cash_reduction_shows_as_unhandled_on_homepage(client):
    account = client.post("/api/v1/broker-accounts", json={"name": "測試券商"})
    account_id = account.json()["id"]
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

    client.post(
        "/corporate-actions",
        data={
            "security_code": "2330",
            "action_type": "cash_capital_reduction",
            "ex_date": "2024-03-01",
            "note": "減資 20%",
        },
        follow_redirects=False,
    )

    homepage = client.get("/")
    assert "有未處理的股票異動" in homepage.text
    assert "減資 20%" in homepage.text
    # And the split-adjusted quantity should NOT have changed — a
    # cash_capital_reduction is never auto-applied.
    assert "1000" in homepage.text


def test_corporate_action_form_rejects_unknown_security_code(client):
    response = client.post(
        "/corporate-actions",
        data={
            "security_code": "9999",
            "action_type": "split",
            "ex_date": "2024-03-01",
            "from_shares": "1",
            "to_shares": "4",
        },
    )

    assert response.status_code == 422
    assert "找不到商品代碼" in response.text
    assert 'id="new-action" open' in response.text
    assert 'value="9999" required' in response.text


def test_corporate_action_form_rejects_missing_ratio_without_losing_input(client):
    response = client.post(
        "/corporate-actions",
        data={
            "security_code": "2330",
            "action_type": "split",
            "ex_date": "2024-03-01",
            "from_shares": "1",
            "to_shares": "",
            "note": "需要補上換股股數",
        },
    )

    assert response.status_code == 422
    assert "股數與配股數請填入正整數" in response.text
    assert 'id="new-action" open' in response.text
    assert 'value="需要補上換股股數"' in response.text


def test_corporate_action_form_rejects_duplicate_same_day(client):
    payload = {
        "security_code": "2330",
        "action_type": "split",
        "ex_date": "2024-03-01",
        "from_shares": "1",
        "to_shares": "4",
    }
    client.post("/corporate-actions", data=payload, follow_redirects=False)

    response = client.post("/corporate-actions", data=payload)

    assert response.status_code == 409


def test_income_calendar_empty_state(client):
    response = client.get("/income-calendar")

    assert response.status_code == 200
    assert "還沒有資料" in response.text


def test_income_calendar_shows_entitlement_without_manual_receipt_entry(
    client, tmp_path
):
    # Design 009: the calendar no longer has a manual receipt form/entry
    # point — it only shows the auto-computed announced entitlement.
    from taiwan_stock.infrastructure.db import get_connection
    from taiwan_stock.shared.ids import new_id

    account = client.post("/api/v1/broker-accounts", json={"name": "測試券商"})
    account_id = account.json()["id"]
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

    conn = get_connection(tmp_path)
    dist_id = new_id()
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES (?, 'sec-2330', '2024-06-01', '5', 'final')",
        (dist_id,),
    )
    conn.close()

    page = client.get("/income-calendar")
    assert page.status_code == 200
    assert "5,000" in page.text  # 1000 shares x 5/share expected gross
    assert "已確定" in page.text
    assert "登記實收" not in page.text
    assert "/income-calendar/receipts" not in page.text

    receipt_response = client.post(
        "/income-calendar/receipts",
        data={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "distribution_id": dist_id,
            "received_date": "2024-06-10",
            "net_amount": "4900",
            "note": "扣了匯費",
        },
    )
    assert receipt_response.status_code == 404


CSV_HEADER = (
    "broker_account_name,security_code,market,event_type,trade_date,"
    "quantity,unit_price,executed_amount,commission,transaction_tax,"
    "other_fees,note\n"
)


def form_fields(html):
    return dict(re.findall(r'name="(preview_id|plan_digest)" value="([^"]+)"', html))


def confirm_body(preview):
    return {"preview_id": preview["preview_id"], "plan_digest": preview["plan_digest"]}


def api_import(client, content, include_rows=()):
    preview = client.post(
        "/api/v1/trade-imports/previews",
        files={"file": ("trades.csv", content, "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    return client.post(
        "/api/v1/trade-imports",
        json={
            "preview_id": body["preview_id"],
            "plan_digest": body["plan_digest"],
            "include_rows": list(include_rows),
        },
    )


def test_json_api_csv_preview_confirm_then_export_round_trip(client, tmp_path):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    content = CSV_HEADER + "CSV券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"

    preview = client.post(
        "/api/v1/trade-imports/previews",
        files={"file": ("trades.csv", content, "text/csv")},
    ).json()
    assert preview["summary"] == {
        "created": 1,
        "updated": 0,
        "unchanged": 0,
        "duplicates": 0,
    }
    assert conn_trade_count(client) == 0

    confirmed = client.post(
        "/api/v1/trade-imports",
        json=confirm_body(preview),
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert (body["created"], body["updated"]) == (1, 0)
    assert (tmp_path / "backups" / body["backup_filename"]).exists()
    assert not list((tmp_path / "imports").glob("*.csv"))

    exported = client.get("/api/v1/exports/trades")
    assert exported.status_code == 200
    assert "CSV券商" in exported.text and ",trade_id" in exported.text

    again = client.post(
        "/api/v1/trade-imports/previews",
        files={"file": ("trades.csv", exported.text, "text/csv")},
    ).json()
    assert again["preview_id"] is None
    assert again["summary"]["unchanged"] == 1


def test_json_api_csv_preview_rejects_bad_row_and_writes_nothing(client):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    content = CSV_HEADER + "不存在券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"

    response = client.post(
        "/api/v1/trade-imports/previews",
        files={"file": ("trades.csv", content, "text/csv")},
    )

    assert response.status_code == 422
    assert conn_trade_count(client) == 0


def test_json_api_confirm_rejects_stale_or_unknown_previews(client):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    content = CSV_HEADER + "CSV券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"
    preview = client.post(
        "/api/v1/trade-imports/previews",
        files={"file": ("trades.csv", content, "text/csv")},
    ).json()
    assert api_import(client, content).status_code == 200

    stale = client.post(
        "/api/v1/trade-imports",
        json=confirm_body(preview),
    )
    assert stale.status_code == 409
    for preview_id in ("0" * 32, "../trades", ""):
        missing = client.post(
            "/api/v1/trade-imports",
            json={"preview_id": preview_id, "plan_digest": preview["plan_digest"]},
        )
        assert missing.status_code == 404
    assert conn_trade_count(client) == 1


def test_transactions_page_import_previews_then_confirms(client, tmp_path):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    content = CSV_HEADER + "CSV券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"

    preview = client.post(
        "/transactions/import",
        files={"file": ("trades.csv", content, "text/csv")},
    )
    assert preview.status_code == 200
    assert "匯入預覽" in preview.text
    assert "確認匯入（新增 1、修改 0）" in preview.text
    assert conn_trade_count(client) == 0

    form = form_fields(preview.text)
    response = client.post("/transactions/import/confirm", data=form)

    assert response.status_code == 200
    assert "已新增 1 筆、修改 0 筆交易" in response.text
    assert conn_trade_count(client) == 1
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_transactions_page_reupload_flags_duplicates_and_skips_them(client, tmp_path):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    content = CSV_HEADER + "CSV券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"
    assert api_import(client, content).status_code == 200

    preview = client.post(
        "/transactions/import",
        files={"file": ("trades.csv", content, "text/csv")},
    )
    assert "疑似重複（1）" in preview.text
    assert 'name="include_rows" value="2"' in preview.text

    form = form_fields(preview.text)
    response = client.post("/transactions/import/confirm", data=form)

    assert "沒有勾選任何疑似重複的列" in response.text
    assert conn_trade_count(client) == 1
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_transactions_page_cancel_discards_the_preview(client):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    content = CSV_HEADER + "CSV券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"
    preview = client.post(
        "/transactions/import",
        files={"file": ("trades.csv", content, "text/csv")},
    )
    form = form_fields(preview.text)

    cancelled = client.post(
        "/transactions/import/cancel",
        data={"preview_id": form["preview_id"]},
        follow_redirects=False,
    )
    assert cancelled.status_code == 303
    response = client.post("/transactions/import/confirm", data=form)
    assert response.status_code == 404
    assert "找不到這次匯入的預覽" in response.text
    assert conn_trade_count(client) == 0


def test_transactions_page_export_downloads_csv(client):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    api_import(client, CSV_HEADER + "CSV券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n")

    response = client.get("/transactions/export")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "2330" in response.text


def conn_trade_count(client: TestClient) -> int:
    account_id = _first_account_id(client)
    return len(
        client.get(
            "/api/v1/trades",
            params={"broker_account_id": account_id, "security_id": "sec-2330"},
        ).json()["items"]
    )


def test_settings_page_create_and_restore_backup(client):
    client.post("/api/v1/broker-accounts", json={"name": "備份測試"})

    create_response = client.post("/settings/backups", follow_redirects=False)
    assert create_response.status_code == 303
    location = create_response.headers["location"]
    assert location.startswith("/settings?created=")

    page = client.get(location)
    assert page.status_code == 200
    assert "已建立備份" in page.text

    filename = location.split("created=")[1].split("#")[0]
    client.post("/api/v1/broker-accounts", json={"name": "備份後才建立的帳戶"})
    assert len(client.get("/api/v1/broker-accounts").json()["items"]) == 2

    restore_response = client.post(
        "/settings/backups/restore",
        data={"filename": filename},
        follow_redirects=False,
    )
    assert restore_response.status_code == 303
    assert "restored=" in restore_response.headers["location"]

    assert len(client.get("/api/v1/broker-accounts").json()["items"]) == 1


def test_settings_page_shows_database_size_and_runs_vacuum(client):
    client.post("/api/v1/broker-accounts", json={"name": "VACUUM 測試"})

    page = client.get("/settings")
    assert page.status_code == 200
    assert "目前資料庫檔案大小" in page.text
    assert "立即執行 VACUUM" in page.text

    vacuum_response = client.post("/settings/vacuum", follow_redirects=False)
    assert vacuum_response.status_code == 303
    assert "vacuumed=" in vacuum_response.headers["location"]

    page = client.get(vacuum_response.headers["location"])
    assert page.status_code == 200
    assert "已回收" in page.text
    # Data survives VACUUM.
    assert len(client.get("/api/v1/broker-accounts").json()["items"]) == 1


def test_settings_page_restore_unknown_backup_shows_error(client):
    response = client.post(
        "/settings/backups/restore",
        data={"filename": "taiwan_stock-nope.db"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "找不到備份" in page.text


def _settings_section(html: str, section_id: str) -> str:
    start = html.index(f'id="{section_id}"')
    return html[start : html.index("</section>", start)]


def test_settings_actions_return_to_their_section_with_notice(client):
    created = client.post("/settings/backups", follow_redirects=False)
    assert created.headers["location"].endswith("#backups")
    filename = created.headers["location"].split("created=")[1].split("#")[0]
    page = client.get(created.headers["location"]).text
    assert "已建立備份" in _settings_section(page, "backups")

    restored = client.post(
        "/settings/backups/restore", data={"filename": filename}, follow_redirects=False
    )
    assert restored.headers["location"].endswith("#backups")
    assert "已還原備份" in _settings_section(
        client.get(restored.headers["location"]).text, "backups"
    )

    missing = client.post(
        "/settings/backups/restore",
        data={"filename": "taiwan_stock-nope.db"},
        follow_redirects=False,
    )
    assert missing.headers["location"].endswith("#backups")
    assert "找不到備份" in _settings_section(
        client.get(missing.headers["location"]).text, "backups"
    )

    saved = client.post(
        "/settings/preferences",
        data={
            "form_format": "display",
            "commission_rate": "0.1425",
            "commission_discount": "6",
            "min_commission": "20",
            "tax_rate_stock": "0.3",
            "tax_rate_etf": "0.1",
        },
        follow_redirects=False,
    )
    assert saved.headers["location"] == "/settings?saved=preferences#preferences"
    page = client.get(saved.headers["location"]).text
    assert "已儲存費用設定" in _settings_section(page, "preferences")
    assert "已儲存費用設定" not in _settings_section(page, "backups")

    account = client.post(
        "/settings/accounts/new", data={"name": "新帳戶"}, follow_redirects=False
    )
    assert account.headers["location"] == "/settings?saved=account#accounts"
    assert "已儲存券商帳戶" in _settings_section(
        client.get(account.headers["location"]).text, "accounts"
    )

    vacuumed = client.post("/settings/vacuum", follow_redirects=False)
    assert vacuumed.headers["location"].endswith("#maintenance")
    maintenance = _settings_section(
        client.get(vacuumed.headers["location"]).text, "maintenance"
    )
    assert "已回收" in maintenance
    assert '<details class="page-tools workspace-maintenance" open>' in maintenance


def test_settings_vacuum_error_shows_in_open_maintenance(client, monkeypatch):
    from taiwan_stock.infrastructure import backup

    def fail(data_dir):
        raise backup.BackupError("資料庫忙碌中")

    monkeypatch.setattr(backup, "vacuum", fail)
    response = client.post("/settings/vacuum", follow_redirects=False)

    assert response.headers["location"].startswith("/settings?vacuum_error=")
    assert response.headers["location"].endswith("#maintenance")
    page = client.get(response.headers["location"]).text
    maintenance = _settings_section(page, "maintenance")
    assert "資料庫忙碌中" in maintenance
    assert '<details class="page-tools workspace-maintenance" open>' in maintenance
    assert "資料庫忙碌中" not in _settings_section(page, "backups")


def test_settings_form_errors_show_in_their_section(client):
    preferences = client.post(
        "/settings/preferences",
        data={
            "form_format": "display",
            "commission_rate": "abc",
            "commission_discount": "6",
            "min_commission": "20",
            "tax_rate_stock": "0.3",
            "tax_rate_etf": "0.1",
        },
    )
    assert preferences.status_code == 422
    assert "請修正費用設定後再儲存" in _settings_section(
        preferences.text, "preferences"
    )
    assert "請修正費用設定後再儲存" not in _settings_section(
        preferences.text, "backups"
    )

    account = client.post("/settings/accounts/new", data={"name": " "})
    assert account.status_code == 422
    assert "請修正帳戶欄位後再儲存" in _settings_section(account.text, "accounts")
    # Nothing was saved, so the maintenance card stays collapsed.
    assert '<details class="page-tools workspace-maintenance">' in account.text


@pytest.mark.parametrize("path", ["/settings", "/securities/sec-2330"])
def test_section_shortcuts_sit_outside_controls_and_load_script(client, path):
    page = client.get(path).text

    controls = page[page.index('class="workspace-controls"') :]
    controls = controls[: controls.index("</section>")]
    assert "workspace-shortcuts" not in controls
    assert re.search(
        r'<nav class="workspace-shortcuts"[^>]* data-section-shortcuts>', page
    )
    assert '<script src="/static/section-shortcuts.js?v=1" defer></script>' in page
    assert client.get("/static/section-shortcuts.js").status_code == 200


def _first_account_id(client: TestClient) -> str:
    accounts = client.get("/api/v1/broker-accounts").json()["items"]
    return accounts[0]["id"]


@pytest.mark.parametrize(
    "field",
    ["unit_price", "executed_amount", "commission", "transaction_tax", "other_fees"],
)
@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity", "abc", "-1"])
def test_trade_api_rejects_invalid_money_without_breaking_homepage(
    client, field, value
):
    account_id = client.post("/api/v1/broker-accounts", json={"name": "Test"}).json()[
        "id"
    ]
    response = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 100,
            "unit_price": "50",
            field: value,
        },
    )
    assert response.status_code == 422
    assert field in response.json()["detail"]
    assert conn_trade_count(client) == 0
    assert client.get("/").status_code == 200


def test_invalid_money_update_keeps_original_trade(client):
    account_id = client.post("/api/v1/broker-accounts", json={"name": "Test"}).json()[
        "id"
    ]
    trade_id = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 100,
            "unit_price": "50",
        },
    ).json()["id"]
    response = client.patch(f"/api/v1/trades/{trade_id}", json={"unit_price": "NaN"})
    assert response.status_code == 422
    position = client.get(
        "/api/v1/positions",
        params={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
        },
    )
    # 5000 (auto-filled executed_amount) + 20 (auto-filled commission,
    # floor(5000*0.001425)=7 is below the NT$20 floor, so 20 applies)
    assert position.json()["remaining_cost"] == "5020"
    assert client.get("/").status_code == 200


def test_trade_form_rejects_nonfinite_money(client):
    account_id = client.post("/api/v1/broker-accounts", json={"name": "Test"}).json()[
        "id"
    ]
    response = client.post(
        "/transactions",
        data={
            "broker_account_id": account_id,
            "security_code": "2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 100,
            "unit_price": "NaN",
        },
    )
    assert response.status_code == 422
    assert "unit_price" in response.text
    assert conn_trade_count(client) == 0


def test_transactions_page_form_auto_fills_commission_and_executed_amount(client):
    account_id = client.post("/api/v1/broker-accounts", json={"name": "Test"}).json()[
        "id"
    ]

    response = client.post(
        "/transactions",
        data={
            "broker_account_id": account_id,
            "security_code": "2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    items = client.get(
        "/api/v1/trades",
        params={"broker_account_id": account_id, "security_id": "sec-2330"},
    ).json()["items"]
    assert len(items) == 1
    # Left blank on the form, filled by the same backend formula the
    # JSON API path uses — no separate/duplicated logic per entry point.
    assert items[0]["executed_amount"] == "50000"
    assert items[0]["commission"] == "71"


@pytest.mark.parametrize(
    "endpoint", ["/api/v1/trade-imports/previews", "/transactions/import"]
)
def test_csv_invalid_money_rolls_back_previous_rows(client, endpoint):
    client.post("/api/v1/broker-accounts", json={"name": "Test"})
    content = (
        CSV_HEADER
        + "Test,2330,TWSE,buy,2024-01-01,100,50,,,,,\n"
        + "Test,2330,TWSE,buy,2024-01-02,100,NaN,,,,,\n"
    )
    response = client.post(
        endpoint, files={"file": ("trades.csv", content, "text/csv")}
    )
    assert response.status_code == 422
    assert "第 3 列" in response.text
    assert conn_trade_count(client) == 0


def test_future_oversell_rejected_before_it_can_break_homepage(client, monkeypatch):
    monkeypatch.setattr(
        "taiwan_stock.portfolio.trades.today_taipei", lambda: "2026-09-11"
    )
    account_id = client.post("/api/v1/broker-accounts", json={"name": "Test"}).json()[
        "id"
    ]
    response = client.post(
        "/api/v1/trades",
        json={
            "broker_account_id": account_id,
            "security_id": "sec-2330",
            "event_type": "sell",
            "trade_date": "2026-09-12",
            "quantity": 100,
        },
    )
    assert response.status_code == 409
    assert conn_trade_count(client) == 0
    monkeypatch.setattr(
        "taiwan_stock.portfolio.trades.today_taipei", lambda: "2026-09-12"
    )
    assert client.get("/").status_code == 200


def test_income_calendar_defaults_to_current_month_and_keeps_future_payments(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "taiwan_stock.web.pages_income.today_taipei", lambda: "2026-09-17"
    )
    monkeypatch.setattr(
        "taiwan_stock.portfolio.distribution_entitlement.today_taipei",
        lambda: "2026-09-17",
    )
    account_id = client.post(
        "/api/v1/broker-accounts", json={"name": "月曆測試"}
    ).json()["id"]
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
    conn = get_connection(tmp_path)
    for dist_id, ex_date, pay_date in [
        ("early", "2026-08-20", "2026-09-05"),
        ("late", "2026-09-21", "2026-09-30"),
        ("next", "2026-09-22", "2026-10-05"),
    ]:
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, pay_date, amount_per_share, "
            "amount_status) VALUES (?, 'sec-2330', ?, ?, '2', 'final')",
            (dist_id, ex_date, pay_date),
        )
    conn.close()

    page = client.get("/income-calendar")
    assert page.status_code == 200
    assert page.context["month"] == "2026-09"
    assert page.context["overview"]["gross"] == 4000
    assert 'data-income-dialog="payments-2026-09-30"' in page.text
    assert '<dialog class="income-dialog" id="payments-2026-09-30"' in page.text
    assert 'id="payments-2026-10-05"' not in page.text
    assert 'aria-current="date"' in page.text

    assert client.get("/income-calendar?view=all").text.count("2026-10-05") == 1

    preview_redirect = client.get(
        "/income-calendar?preview=true", follow_redirects=False
    )
    assert preview_redirect.status_code == 303
    assert preview_redirect.headers["location"] == (
        "/income-calendar?view=all&notice=preview-removed"
    )
    redirected = client.get("/income-calendar?preview=true")
    assert redirected.status_code == 200
    assert "推估功能已下線" in redirected.text


@pytest.mark.parametrize("month", ["2026-13", "202609", "invalid"])
def test_income_calendar_invalid_month_shows_error(client, month):
    page = client.get("/income-calendar", params={"month": month})
    assert page.status_code == 200
    assert "月份格式必須是 YYYY-MM" in page.text


@pytest.mark.parametrize(
    "month, previous, following",
    [
        ("2026-12", "2026-11", "2027-01"),
        ("2027-01", "2026-12", "2027-02"),
    ],
)
def test_income_calendar_month_navigation_across_year(
    client, month, previous, following
):
    page = client.get("/income-calendar", params={"month": month})
    assert page.context["previous_month"] == previous
    assert page.context["next_month"] == following


def test_income_calendar_loads_dedicated_layout_and_dialog_assets(client):
    import re

    page = client.get("/income-calendar")
    assert 'class="income-month-page"' in page.text
    for asset in ("css", "js"):
        url = re.search(r"/static/income-calendar\." + asset + r'\?v=[^" ]+', page.text)
        assert url is not None
        response = client.get(url.group())
        assert response.status_code == 200
        if asset == "css":
            assert "grid-template-columns: repeat(7, minmax(0, 1fr))" in response.text
        else:
            assert "dialog.showModal()" in response.text


def test_transactions_page_stale_confirm_shows_a_fresh_preview(client):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    content = CSV_HEADER + "CSV券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"
    preview = client.post(
        "/transactions/import",
        files={"file": ("trades.csv", content, "text/csv")},
    )
    assert api_import(client, content).status_code == 200

    response = client.post(
        "/transactions/import/confirm", data=form_fields(preview.text)
    )

    assert response.status_code == 409
    assert "預覽後交易資料已變更" in response.text
    assert "疑似重複（1）" in response.text
    assert conn_trade_count(client) == 1


def test_json_api_confirm_rejects_rows_that_are_not_duplicates(client):
    client.post("/api/v1/broker-accounts", json={"name": "CSV券商"})
    content = CSV_HEADER + "CSV券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"

    response = api_import(client, content, include_rows=[2])

    assert response.status_code == 422
    assert "只能勾選疑似重複的列" in response.json()["detail"]
    assert conn_trade_count(client) == 0
