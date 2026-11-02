"""Integration tests for `/securities/{security_id}` (design 002 §4.3.3)."""

import re

from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app


def test_unknown_security_returns_404(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/securities/does-not-exist").status_code == 404


def test_stock_detail_page_shows_basic_info_and_no_holdings(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities "
            "(id, market, code, name, security_type, industry, "
            " transfer_agent_name, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', "
            " '半導體業', '中國信託商業銀行', 'active')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-2330")

        assert response.status_code == 200
        assert "台積電" in response.text
        assert "半導體業" in response.text
        assert "中國信託商業銀行" in response.text
        assert "尚未持有這檔商品" in response.text
        assert "加入追蹤" in response.text
        assert "取消追蹤" not in response.text


def test_etf_detail_page_shows_profile_fields(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0056', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
        )
        conn.execute(
            "INSERT INTO etf_profiles "
            "(security_id, custodian_bank, distribution_frequency, benchmark) "
            "VALUES ('sec-0056', '玉山銀行', 'quarterly', '台灣高股息指數')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-0056")

        assert response.status_code == 200
        assert "玉山銀行" in response.text
        assert "季配" in response.text
        assert "台灣高股息指數" in response.text


def test_detail_page_shows_latest_close_and_change(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
        )
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close) "
            "VALUES ('sec-2330', '2026-09-09', '1000.00')"
        )
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close) "
            "VALUES ('sec-2330', '2026-09-10', '1050.00')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-2330")

        assert response.status_code == 200
        assert "1,050" in response.text
        assert "+50" in response.text


def test_detail_page_toggles_watchlist_and_shows_tracked_state(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
        )
        conn.commit()
        conn.close()

        add_response = client.post(
            "/watchlist/add",
            data={"security_id": "sec-2330", "return_to": "/securities/sec-2330"},
            follow_redirects=False,
        )
        assert add_response.status_code == 303
        assert add_response.headers["location"].startswith("/securities/sec-2330")

        page = client.get("/securities/sec-2330")
        assert '<button type="submit">取消追蹤</button>' in page.text
        assert '<button type="submit">加入追蹤</button>' not in page.text


def test_detail_page_shows_position_breakeven_and_cost_yield(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
        )
        conn.execute(
            "INSERT INTO broker_accounts (id, name, status) "
            "VALUES ('acc-1', '測試證券', 'active')"
        )
        conn.execute(
            """
            INSERT INTO trades
                (id, broker_account_id, security_id, event_type, trade_date,
                 quantity, unit_price, executed_amount, commission, created_at)
            VALUES
                ('t-1', 'acc-1', 'sec-2330', 'buy', '2026-09-01',
                 1000, '1000', '1000000', '1425', '2026-09-01T00:00:00Z')
            """
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-2330")

        assert response.status_code == 200
        assert "測試證券" in response.text
        assert "1,000" in response.text  # remaining_quantity


def test_etf_detail_page_shows_distribution_history_with_all_dates(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-00878', 'TWSE', '00878', '國泰永續高股息', 'etf', 'active')"
        )
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, record_date, pay_date, "
            " amount_per_share, amount_status, source) "
            "VALUES ('d1', 'sec-00878', '2026-08-18', '2026-08-24', "
            " '2026-09-11', '1.01', 'final', 'twse_etf_distribution_list')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-00878")

        assert response.status_code == 200
        assert "2026-08-18" in response.text
        assert "2026-08-24" in response.text
        assert "2026-09-11" in response.text
        assert "1.01" in response.text
        assert "已公告" in response.text


def test_distribution_history_shows_pending_pay_date_label_not_ex_date(tmp_path):
    # Design 004 §4.7.3: an announced ex-dividend date with no pay date
    # yet must show "發放月份待確認", never fall back to the ex-dividend
    # date/month as a stand-in for the pay date.
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-00918', 'TWSE', '00918', '大華優利高填息30', 'etf', "
            " 'active')"
        )
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, record_date, pay_date, "
            " amount_per_share, amount_status, source) "
            "VALUES ('d1', 'sec-00918', '2026-09-18', '2026-09-26', NULL, "
            " NULL, 'unknown', 'twse_etf_distribution_list')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-00918")

        assert response.status_code == 200
        assert "2026-09-18" in response.text
        assert "發放月份待確認" in response.text
        assert "尚無可信來源" in response.text  # amount


def test_detail_page_falls_back_to_fetched_at_without_sync_history(tmp_path):
    # Design 023 §4.4 point 5: with no confirmed year in the sync history,
    # `fetched_at` only says when the rows arrived — never that a year was
    # fully synced — and is shown as a Taipei date (03:00Z is 11:00 Taipei).
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-00878', 'TWSE', '00878', '國泰永續高股息', 'etf', 'active')"
        )
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, amount_per_share, amount_status, "
            " source, fetched_at) "
            "VALUES ('d1', 'sec-00878', '2026-08-18', '1.01', 'final', "
            " 'twse_etf_distribution_list', '2026-09-12T03:00:00.000Z')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-00878")

        assert response.status_code == 200
        assert "紀錄取得於 2026/09/12" in response.text
        assert "最近成功同步時間" not in response.text


def test_detail_page_shows_never_synced_when_no_fetched_at(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0056', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-0056")

        assert response.status_code == 200
        assert "尚未同步配息資料" in response.text


def test_detail_page_with_no_distributions_shows_empty_state(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0056', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-0056")

        assert response.status_code == 200
        assert "尚無已知配息事件" in response.text


def test_stock_detail_page_known_and_estimated_yield_unavailable_without_data(
    tmp_path,
):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-2330")

        assert response.status_code == 200
        assert '<p class="stat-note">資料不足</p>' in response.text
        assert "配息頻率未知，無法推估" in response.text


def test_etf_detail_page_shows_unknown_bank_and_frequency_when_unset(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0056', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
        )
        conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('sec-0056')")
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-0056")

        assert response.status_code == 200
        assert "尚無可信來源" in response.text
        assert "tier-unknown" in response.text
        assert "手動填入" in response.text


def test_custodian_bank_override_takes_priority_and_can_be_cleared(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0056', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
        )
        conn.execute(
            "INSERT INTO etf_profiles (security_id, custodian_bank) "
            "VALUES ('sec-0056', '中國信託商業銀行')"
        )
        conn.commit()
        conn.close()

        set_response = client.post(
            "/securities/sec-0056/custodian-bank-override",
            data={"custodian_bank_user_override": "玉山銀行"},
            follow_redirects=False,
        )
        assert set_response.status_code == 303

        page = client.get("/securities/sec-0056")
        assert "使用者自訂" in page.text
        assert "tier-user_override" in page.text
        # A different official value already exists — must prompt to
        # switch rather than silently hide it forever (design 004
        # §4.5.1's `custodian_bank_user_override` row).
        assert "系統已找到官方資料" in page.text
        assert "中國信託商業銀行" in page.text

        clear_response = client.post(
            "/securities/sec-0056/custodian-bank-override",
            data={"custodian_bank_user_override": ""},
            follow_redirects=False,
        )
        assert clear_response.status_code == 303

        cleared_page = client.get("/securities/sec-0056")
        assert "tier-official" in cleared_page.text


def test_custodian_bank_override_rejects_value_outside_bank_dictionary(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0056', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
        )
        conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('sec-0056')")
        conn.commit()
        conn.close()

        response = client.post(
            "/securities/sec-0056/custodian-bank-override",
            data={"custodian_bank_user_override": "不存在的銀行"},
        )

        assert response.status_code == 400


def _seed_holding(conn, *, security_id="sec-2330", broker_account_id="acc-1"):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        f"VALUES ('{security_id}', 'TWSE', '2330', '台積電', 'stock', 'active')"
    )
    conn.execute(
        "INSERT INTO broker_accounts (id, name, dividend_bank_name, status) "
        f"VALUES ('{broker_account_id}', '測試證券', '台新國際商業銀行', 'active')"
    )
    conn.execute(
        f"""
        INSERT INTO trades
            (id, broker_account_id, security_id, event_type, trade_date,
             quantity, unit_price, executed_amount, commission, created_at)
        VALUES
            ('t-1', '{broker_account_id}', '{security_id}', 'buy', '2026-09-01',
             1000, '1000', '1000000', '1425', '2026-09-01T00:00:00Z')
        """
    )


def test_holding_shows_account_default_bank_when_no_preference_is_set(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        _seed_holding(conn)
        conn.commit()
        conn.close()

        page = client.get("/securities/sec-2330")

        assert "台新國際商業銀行（帳戶預設）" in page.text
        assert "（自訂）" not in page.text


def test_setting_dividend_bank_preference_overrides_the_account_default(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        _seed_holding(conn)
        conn.commit()
        conn.close()

        set_response = client.post(
            "/securities/sec-2330/broker-accounts/acc-1/dividend-bank",
            data={"dividend_bank": "玉山銀行"},
            follow_redirects=False,
        )
        assert set_response.status_code == 303

        page = client.get("/securities/sec-2330")
        assert "玉山銀行（自訂）" in page.text

        clear_response = client.post(
            "/securities/sec-2330/broker-accounts/acc-1/dividend-bank",
            data={"dividend_bank": ""},
            follow_redirects=False,
        )
        assert clear_response.status_code == 303

        cleared_page = client.get("/securities/sec-2330")
        assert "台新國際商業銀行（帳戶預設）" in cleared_page.text


def test_dividend_bank_preference_rejects_value_outside_bank_dictionary(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        _seed_holding(conn)
        conn.commit()
        conn.close()

        response = client.post(
            "/securities/sec-2330/broker-accounts/acc-1/dividend-bank",
            data={"dividend_bank": "不存在的銀行"},
        )

        assert response.status_code == 400


def test_distribution_frequency_override_shows_as_user_override(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0056', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
        )
        conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('sec-0056')")
        conn.commit()
        conn.close()

        client.post(
            "/securities/sec-0056/distribution-frequency-override",
            data={"distribution_frequency_user_override": "monthly"},
            follow_redirects=False,
        )

        page = client.get("/securities/sec-0056")
        assert "月配" in page.text
        assert "tier-user_override" in page.text


def test_etf_overrides_persist_when_etf_profiles_row_does_not_exist_yet(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-00940', 'TWSE', '00940', '元大台灣價值高息', 'etf', 'active')"
        )
        conn.commit()
        conn.close()

        bank_resp = client.post(
            "/securities/sec-00940/custodian-bank-override",
            data={"custodian_bank_user_override": "華南銀行"},
            follow_redirects=False,
        )
        freq_resp = client.post(
            "/securities/sec-00940/distribution-frequency-override",
            data={"distribution_frequency_user_override": "monthly"},
            follow_redirects=False,
        )

        assert bank_resp.status_code == 303
        assert freq_resp.status_code == 303
        page = client.get("/securities/sec-00940")
        assert "華南銀行" in page.text
        assert "月配" in page.text
        assert page.text.count("tier-user_override") >= 2

        # Clearing overrides on an existing row also works cleanly.
        client.post(
            "/securities/sec-00940/custodian-bank-override",
            data={"custodian_bank_user_override": ""},
            follow_redirects=False,
        )
        client.post(
            "/securities/sec-00940/distribution-frequency-override",
            data={"distribution_frequency_user_override": ""},
            follow_redirects=False,
        )
        conn = get_connection(tmp_path)
        row = conn.execute(
            "SELECT custodian_bank_user_override, distribution_frequency_user_override "
            "FROM etf_profiles WHERE security_id = 'sec-00940'"
        ).fetchone()
        conn.close()
        assert row["custodian_bank_user_override"] is None
        assert row["distribution_frequency_user_override"] is None


def test_official_ex_dividend_months_are_shown_not_evaluation_months(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0919', 'TWSE', '00919', '群益台灣精選高息', 'etf', 'active')"
        )
        conn.execute(
            "INSERT INTO etf_profiles "
            "(security_id, distribution_frequency, distribution_month_pattern, "
            "distribution_ex_dividend_schedule) "
            "VALUES ('sec-0919', 'quarterly', '[2, 5, 8, 11]', "
            '\'{"frequency":"quarterly","months":[3,6,9,12]}\')'
        )
        conn.commit()
        conn.close()

        response = client.get("/securities/sec-0919")

        assert response.status_code == 200
        assert "3、6、9、12 月" in response.text
        assert "2、5、8、11 月" not in response.text


def test_inferred_frequency_shows_basis_dates_when_no_official_source(tmp_path):
    # Design 004 §4.4.1 Phase F: with no 投信 adapter frequency for this
    # ETF, the local two-event interval inference fills the "系統推測"
    # tier — the two dates it's based on must be visible so the user
    # can judge whether to trust it.
    from taiwan_stock.catalog import frequency_inference, sync

    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-0050', 'TWSE', '0050', '元大台灣50', 'etf', 'active')"
        )
        conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('sec-0050')")
        conn.commit()
        sync.upsert_distribution(
            conn,
            "sec-0050",
            {
                "ex_dividend_date": "2026-04-15",
                "amount_per_share": "1.0",
                "amount_status": "final",
            },
        )
        sync.upsert_distribution(
            conn,
            "sec-0050",
            {
                "ex_dividend_date": "2026-07-15",
                "amount_per_share": "1.0",
                "amount_status": "final",
            },
        )
        conn.commit()
        frequency_inference.recompute_all(conn)
        conn.close()

        response = client.get("/securities/sec-0050")

        assert response.status_code == 200
        assert "季配" in response.text
        assert "tier-inferred" in response.text
        assert "2026-04-15" in response.text
        assert "2026-07-15" in response.text
        assert "1、4、7、10 月" in response.text


def _stock_with_dividends(tmp_path, *, trade_date=None, watched=False):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2103', 'TWSE', '2103', '台橡', 'stock', 'active')"
    )
    for i, ex_date in enumerate(("2021-07-20", "2022-02-24", "2022-07-19")):
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, amount_per_share, amount_status, "
            " source, fetched_at) "
            "VALUES (?, 'sec-2103', ?, '1.00', 'final', 'twse_ex_rights_result', "
            " '2026-10-31T01:46:59.006Z')",
            (f"d{i}", ex_date),
        )
    if trade_date:
        conn.execute(
            "INSERT INTO broker_accounts (id, name, status) "
            "VALUES ('acc-1', '測試證券', 'active')"
        )
        conn.execute(
            "INSERT INTO trades (id, broker_account_id, security_id, event_type, "
            " trade_date, quantity, unit_price, executed_amount, commission, "
            " created_at) "
            "VALUES ('t-1', 'acc-1', 'sec-2103', 'buy', ?, 1000, '30', '30000', "
            " '42', '2022-02-24T00:00:00Z')",
            (trade_date,),
        )
    if watched:
        conn.execute("INSERT INTO watchlist (security_id) VALUES ('sec-2103')")
    conn.commit()
    conn.close()


def test_distribution_history_marks_rows_before_first_acquisition(tmp_path):
    """Design 050 §4.2.3: earlier distributions stay, muted and labeled."""
    with TestClient(create_app(tmp_path)) as client:
        _stock_with_dividends(tmp_path, trade_date="2022-02-24")
        page = client.get("/securities/sec-2103").text

    assert page.count('class="before-acquired"') == 1
    assert '2021-07-20<span class="cell-note">買進前</span>' in page
    # An ex-date on the acquisition day itself is not "before".
    assert '2022-02-24<span class="cell-note">' not in page
    assert "配息更新至 2026/10/31。含 2022/02/24 第一次買進以來的配息。" in page
    assert "2026-10-31T01:46:59" not in page
    assert "前兩個曆年" not in page


def test_distribution_history_scope_note_without_trades(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _stock_with_dividends(tmp_path, watched=True)
        page = client.get("/securities/sec-2103").text

    assert 'class="before-acquired"' not in page
    assert "配息更新至 2026/10/31。近 2 年的配息。" in page


def test_etf_distribution_scope_note_without_trades(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-00878', 'TWSE', '00878', '國泰永續高股息', 'etf', 'active')"
        )
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, amount_per_share, amount_status, "
            " source, fetched_at) "
            "VALUES ('d1', 'sec-00878', '2026-08-18', '1.01', 'final', "
            " 'twse_etf_distribution_list', '2026-09-12T03:00:00.000Z')"
        )
        conn.commit()
        conn.close()
        page = client.get("/securities/sec-00878").text

    assert "紀錄取得於 2026/09/12。本年及前兩年的配息。" in page


def _etf_with_custodian(tmp_path, raw):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-00712', 'TWSE', '00712', '復華富時不動產', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id, custodian_bank, "
        " custodian_institution_raw) VALUES ('sec-00712', '玉山銀行', ?)",
        (raw,),
    )
    conn.commit()
    conn.close()


def test_same_raw_custodian_folds_into_the_source_tag(tmp_path):
    """Design 051 §4.2.1: one line, no duplicate row for the same bank."""
    with TestClient(create_app(tmp_path)) as client:
        _etf_with_custodian(tmp_path, "玉山商業銀行股份有限公司")
        page = client.get("/securities/sec-00712").text

    assert "來源原始保管機構" not in page
    assert re.search(
        r'<span class="profile-value">玉山銀行</span>\s*'
        r'<span class="profile-source tier-official" '
        r'title="官方公告資料；交易所原始名稱：玉山商業銀行股份有限公司">',
        page,
    )


def test_different_raw_custodian_keeps_its_row(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _etf_with_custodian(tmp_path, "臺灣集中保管結算所股份有限公司")
        page = client.get("/securities/sec-00712").text

    assert "來源原始保管機構" in page
    assert "交易所原始名稱" not in page
