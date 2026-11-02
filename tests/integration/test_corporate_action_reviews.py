"""Review decisions change warnings and provenance, never investment inputs."""

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import corporate_action_reviews as reviews
from taiwan_stock.portfolio import corporate_actions, pricing, trades


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(reviews, "today_taipei", lambda: "2026-10-15")
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    connection.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) VALUES('s',"
        "'TWSE','3481','測試股票','stock','active')"
    )
    for account in ("a", "b"):
        connection.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES(?,?,'active')",
            (account, "券商" + account),
        )
    connection.execute(
        "INSERT INTO corporate_actions(id,security_id,action_type,ex_date,note) "
        "VALUES('ca','s','cash_capital_reduction','2024-08-26','人工補齊成本')"
    )
    _trade(connection, "opening", "2024-08-26", 1565, "29713")
    _trade(connection, "buy", "2025-01-06", 435, "6090", commission="20")
    _trade(connection, "opening", "2024-08-26", 100, "1000", account="b")
    yield connection
    connection.close()


def _trade(conn, event, day, quantity, amount=None, *, commission=None, account="a"):
    return trades.create_trade(
        conn,
        {
            "broker_account_id": account,
            "security_id": "s",
            "event_type": event,
            "trade_date": day,
            "quantity": quantity,
            "executed_amount": amount,
            "commission": commission,
        },
    )


def _record(conn, decision="accepted_estimate", note="歷史交易均價推估", account="a"):
    token = reviews.preview(conn, "ca", account)["review_token"]
    reviews.record(conn, "ca", account, decision, note, token)
    return token


def _protected(conn):
    names = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN "
            "('corporate_action_reviews','sqlite_sequence')"
        )
    ]
    result = {}
    for name in names:
        where = " WHERE version < 15" if name == "schema_migrations" else ""
        result[name] = [
            tuple(r)
            for r in conn.execute(f'SELECT * FROM "{name}"{where} ORDER BY rowid')
        ]
    return result


def test_accept_estimate_keeps_values_and_only_resolves_one_account(conn):
    before = _protected(conn)
    overview = trades.list_position_overview(conn)
    preview = reviews.preview(conn, "ca", "a")
    assert preview["position"].remaining_quantity == 2000
    assert preview["position"].remaining_cost == 35823
    assert preview["breakeven_price"] == Decimal("18.00")
    _record(conn)
    assert _protected(conn) == before
    assert trades.list_position_overview(conn) == overview
    unhandled = corporate_actions.list_unhandled_actions_for_held_securities(conn)
    assert [r["broker_account_id"] for r in unhandled] == ["b"]
    book = reviews.ReviewBook(conn)
    assert book.quality("a", "s")[0]["note"] == "歷史交易均價推估"
    assert book.quality("b", "s") == []
    history, more = reviews.history_page(conn, "ca", "a")
    assert not more
    assert history[0]["total_cost"] == "35823"
    assert history[0]["decision"] == "accepted_estimate"
    assert history[0]["reviewed_at"].endswith("Z")


def test_confirm_reopen_reconfirm_preserves_history_and_estimate(conn):
    _record(conn)
    _record(conn, "reopened", "取得新資料，重新核對")
    assert reviews.ReviewBook(conn).quality("a", "s")
    assert len(corporate_actions.list_unhandled_actions_for_held_securities(conn)) == 2
    _record(conn, "confirmed", "已對照交易憑證")
    assert reviews.ReviewBook(conn).quality("a", "s") == []
    assert len(corporate_actions.list_unhandled_actions_for_held_securities(conn)) == 1
    assert [r["decision"] for r in reviews.history_page(conn, "ca", "a")[0]] == [
        "confirmed",
        "reopened",
        "accepted_estimate",
    ]


def test_confirm_exchange_cost_keeps_two_thousand_shares(conn):
    conn.execute("DELETE FROM trades WHERE broker_account_id='a'")
    _trade(conn, "opening", "2026-03-30", 2000, "320720")
    conn.execute(
        "UPDATE corporate_actions SET action_type='other',ex_date='2026-03-30'"
    )
    current = reviews.preview(conn, "ca", "a")
    assert current["average_cost"] == Decimal("160.36")
    assert current["breakeven_price"] == Decimal("161.5")
    _record(conn, "confirmed", "沿用換股前成本，加計補買零股")
    assert trades.compute_position(conn, "a", "s").remaining_cost == 320720
    assert reviews.ReviewBook(conn).quality("a", "s") == []
    assert len(corporate_actions.list_unhandled_actions_for_held_securities(conn)) == 1


@pytest.mark.parametrize("change", ["cost", "backdate", "delete", "action", "split"])
def test_historical_changes_restore_warning_without_losing_estimate(conn, change):
    _record(conn)
    if change == "cost":
        conn.execute(
            "UPDATE trades SET executed_amount='30000' WHERE broker_account_id='a' "
            "AND event_type='opening'"
        )
    elif change == "backdate":
        _trade(conn, "buy", "2025-02-01", 10, "100")
    elif change == "delete":
        conn.execute(
            "DELETE FROM trades WHERE broker_account_id='a' AND event_type='buy'"
        )
    elif change == "action":
        conn.execute("UPDATE corporate_actions SET ex_date='2024-08-27' WHERE id='ca'")
    else:
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": "s",
                "action_type": "split",
                "ex_date": "2025-06-01",
                "ratio_numerator": 2,
                "ratio_denominator": 1,
            },
        )
    book = reviews.ReviewBook(conn)
    assert book.state("ca", "a") == "stale"
    assert book.quality("a", "s")
    assert len(corporate_actions.list_unhandled_actions_for_held_securities(conn)) == 2


def test_changed_confirmed_history_does_not_certify_previous_estimate(conn):
    _record(conn)
    _record(conn, "confirmed", "已核對")
    conn.execute(
        "UPDATE trades SET executed_amount='31000' WHERE event_type='opening' AND "
        "broker_account_id='a'"
    )
    assert reviews.ReviewBook(conn).quality("a", "s")


def test_notes_quotes_and_other_accounts_do_not_invalidate_review(conn):
    _record(conn)
    conn.execute("UPDATE trades SET note='純備註'")
    conn.execute("UPDATE corporate_actions SET note='補充說明'")
    conn.execute("UPDATE trades SET executed_amount='900' WHERE broker_account_id='b'")
    conn.execute(
        "INSERT INTO daily_prices(security_id,trade_date,close) VALUES('s',"
        "'2026-10-15','20')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO app_settings(key,value) VALUES('min_commission','10')"
    )
    assert reviews.ReviewBook(conn).state("ca", "a") == "accepted_estimate"


def test_future_trades_partial_sales_and_repurchase(conn, monkeypatch):
    _record(conn)
    _trade(conn, "sell", "2026-10-16", 1000)
    monkeypatch.setattr(reviews, "today_taipei", lambda: "2026-10-16")
    book = reviews.ReviewBook(conn)
    assert book.state("ca", "a") == "accepted_estimate"
    assert book.position("a", "s").remaining_quantity == 1000
    assert book.quality("a", "s")
    _trade(conn, "sell", "2026-10-17", 1000)
    _trade(conn, "buy", "2026-10-18", 200, "4000")
    monkeypatch.setattr(reviews, "today_taipei", lambda: "2026-10-18")
    book = reviews.ReviewBook(conn)
    assert book.state("ca", "a") == "accepted_estimate"
    assert book.position("a", "s").remaining_quantity == 200
    assert book.quality("a", "s") == []


def test_confirming_one_action_does_not_resolve_other_estimate(conn):
    _record(conn)
    other = corporate_actions.create_corporate_action(
        conn, {"security_id": "s", "action_type": "other", "ex_date": "2025-01-01"}
    )
    token = reviews.preview(conn, other, "a")["review_token"]
    reviews.record(conn, other, "a", "confirmed", "僅核對此事件", token)
    assert reviews.ReviewBook(conn).quality("a", "s")
    assert {
        r["id"]
        for r in corporate_actions.list_unhandled_actions_for_held_securities(conn)
        if r["broker_account_id"] == "a"
    } == set()


@pytest.mark.parametrize("change", ["unknown", "no_position", "future", "auto"])
def test_ineligible_position_cannot_be_confirmed(conn, change):
    if change == "unknown":
        conn.execute(
            "UPDATE trades SET executed_amount=NULL WHERE event_type='opening' AND "
            "broker_account_id='a'"
        )
    elif change == "no_position":
        _trade(conn, "sell", "2025-02-01", 2000)
    elif change == "future":
        conn.execute("UPDATE corporate_actions SET ex_date='2099-01-01'")
    else:
        conn.execute(
            "UPDATE corporate_actions SET action_type='split',ratio_numerator=2,"
            "ratio_denominator=1"
        )
    current = reviews.preview(conn, "ca", "a")
    assert current["problem"]
    with pytest.raises(reviews.ReviewValidationError):
        _record(conn)
    assert reviews.history_page(conn, "ca", "a")[0] == []


def test_legitimate_known_zero_is_not_unknown(conn):
    conn.execute(
        "UPDATE trades SET executed_amount='0',commission='0' WHERE "
        "broker_account_id='a'"
    )
    _record(conn, "confirmed", "合法零成本，有憑證")
    assert reviews.ReviewBook(conn).state("ca", "a") == "confirmed"


@pytest.mark.parametrize(
    "decision,note",
    [("", "依據"), ("bogus", "依據"), ("confirmed", " "), ("confirmed", "字" * 2001)],
)
def test_invalid_form_never_appends(conn, decision, note):
    with pytest.raises(reviews.ReviewValidationError):
        _record(conn, decision, note)
    assert not conn.in_transaction
    assert not reviews.history_page(conn, "ca", "a")[0]


def test_reopen_requires_previous_review(conn):
    with pytest.raises(reviews.ReviewValidationError):
        _record(conn, "reopened", "重新核對")


def test_duplicate_and_changed_preview_are_rejected(conn):
    token = _record(conn)
    with pytest.raises(reviews.StaleReviewError):
        reviews.record(conn, "ca", "a", "accepted_estimate", "再送一次", token)
    token = reviews.preview(conn, "ca", "a")["review_token"]
    _trade(conn, "buy", "2025-02-01", 1, "20")
    with pytest.raises(reviews.StaleReviewError):
        reviews.record(conn, "ca", "a", "confirmed", "過期確認", token)
    assert len(reviews.history_page(conn, "ca", "a")[0]) == 1


def test_parallel_submissions_have_one_winner(conn, tmp_path):
    token = reviews.preview(conn, "ca", "a")["review_token"]

    def submit(_):
        local = get_connection(tmp_path)
        try:
            reviews.record(local, "ca", "a", "confirmed", "已核對", token)
            return "saved"
        except reviews.StaleReviewError:
            return "stale"
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == ["saved", "stale"]
    assert len(reviews.history_page(conn, "ca", "a")[0]) == 1


def test_failed_append_rolls_back(conn):
    conn.execute(
        "CREATE TRIGGER reject_review BEFORE INSERT ON corporate_action_reviews "
        "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        _record(conn)
    assert not conn.in_transaction
    assert not reviews.history_page(conn, "ca", "a")[0]


def test_history_is_paginated_without_discarding_old_records(conn):
    for _ in range(26):
        _record(conn)
        _record(conn, "reopened", "重新檢查")
    first, more = reviews.history_page(conn, "ca", "a")
    second, more_second = reviews.history_page(conn, "ca", "a", 2)
    assert len(first) == 50 and more
    assert len(second) == 2 and not more_second
    assert first[-1]["id"] > second[0]["id"]


@pytest.fixture
def client(conn, tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def _form_token(response):
    return re.search(r'name="review_token" value="([^"]+)"', response.text)[1]


URL = "/corporate-actions/ca/review?broker_account_id=a"


@pytest.mark.parametrize(
    "count,requested", [(0, 1), (1, 1), (101, 1), (101, 2), (101, 3), (101, 999)]
)
def test_history_navigation_counts_only_current_event_and_account(
    client, conn, count, requested
):
    _record(conn, "confirmed", "紀錄 000")
    conn.executemany(
        "INSERT INTO corporate_action_reviews(action_id,broker_account_id,decision,"
        "reviewed_at,as_of_date,quantity,total_cost,basis_snapshot,"
        "basis_fingerprint,note) "
        "SELECT action_id,broker_account_id,decision,reviewed_at,as_of_date,quantity,"
        "total_cost,basis_snapshot,basis_fingerprint,? "
        "FROM corporate_action_reviews WHERE id=1",
        [(f"紀錄 {i:03}",) for i in range(1, count)],
    )
    if not count:
        conn.execute("DELETE FROM corporate_action_reviews")
    conn.execute(
        "INSERT INTO corporate_actions(id,security_id,action_type,ex_date) "
        "VALUES('other-ca','s','other','2025-01-01')"
    )
    conn.execute(
        "INSERT INTO corporate_action_reviews(action_id,broker_account_id,decision,"
        "reviewed_at,as_of_date,quantity,total_cost,basis_snapshot,"
        "basis_fingerprint,note) "
        "VALUES('ca','b','confirmed','2026-01-01T00:00:00Z','2026-01-01','100','1000','{}','x','其他帳戶'),"
        "('other-ca','a','confirmed','2026-01-01T00:00:00Z','2026-01-01','100','1000','{}','x','其他事件')"
    )
    response = client.get(URL, params={"broker_account_id": "a", "page": requested})
    assert response.status_code == 200
    pages = 3 if count == 101 else 1
    current = min(pages, requested)
    nav = re.search(r'<nav class="pagination".*?</nav>', response.text, re.S)[0]
    assert 'aria-label="處理歷程分頁"' in nav
    assert f"第 {current} 頁／共 {pages} 頁" in nav
    disabled = re.findall(r'<span aria-disabled="true">([^<]+)</span>', nav)
    assert disabled == (
        (["第一頁", "上一頁"] if current == 1 else [])
        + (["下一頁", "最後一頁"] if current == pages else [])
    )
    expected = {}
    if current > 1:
        expected.update({"第一頁": 1, "上一頁": current - 1})
    if current < pages:
        expected.update({"下一頁": current + 1, "最後一頁": pages})
    links = {
        label: unescape(href)
        for href, label in re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', nav)
    }
    assert links.keys() == expected.keys()
    for label, target in expected.items():
        parsed = urlsplit(links[label])
        assert parsed.path == "/corporate-actions/ca/review"
        assert parse_qs(parsed.query) == {
            "broker_account_id": ["a"],
            "page": [str(target)],
        }
        landed = client.get(links[label])
        assert landed.status_code == 200
        assert f"第 {target} 頁／共 {pages} 頁" in landed.text
        notes = re.findall(r'<p class="review-note">([^<]+)</p>', landed.text)
        assert notes == [
            f"紀錄 {i:03}"
            for i in range(
                count - 50 * (target - 1) - 1, max(-1, count - 50 * target - 1), -1
            )
        ]
    assert "其他帳戶" not in response.text and "其他事件" not in response.text
    if not count:
        assert "尚無人工確認紀錄" in response.text


def test_html_flow_removes_warning_and_keeps_estimate_everywhere(client, conn):
    page = client.get(URL)
    assert page.status_code == 200
    assert "35,823" in page.text and "17.91" in page.text
    assert " checked" not in page.text
    token = _form_token(page)
    response = client.post(
        "/corporate-actions/ca/review",
        data={
            "broker_account_id": "a",
            "decision": "accepted_estimate",
            "note": "歷史均價推估\n保留依據",
            "review_token": token,
        },
    )
    assert response.status_code == 200
    assert "暫時使用估算成本" in response.text and "（台北時間）" in response.text
    home = client.get("/").text
    assert "未處理的股票異動（1 筆）" in home
    assert "持股摘要含暫估成本" in home
    for path in (
        "/",
        "/securities/s",
        "/sale-simulation?broker_account_id=a&security_id=s&quantity=100&price=20",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "成本暫估" in response.text and "查看依據" in response.text
    assert "券商a · 暫時使用估算成本" in client.get("/corporate-actions").text
    response = client.post(
        "/corporate-actions/ca/reopen",
        data={
            "broker_account_id": "a",
            "note": "重新取得憑證",
            "review_token": _form_token(response=client.get(URL)),
        },
    )
    assert response.status_code == 200
    assert "重新取得憑證" in response.text
    assert "未處理的股票異動（2 筆）" in client.get("/").text
    assert reviews.ReviewBook(conn).quality("a", "s")


def test_adopting_reconstructed_cost_ends_provisional_warning_everywhere(client, conn):
    _record(conn)
    before = _protected(conn)
    position_before = trades.compute_position(conn, "a", "s")
    basis = (
        "歷史資料已用盡，確認採用推算成本：期初 1,565 股為 29,713 元，"
        "加計 435 股成本 6,110 元，合計 2,000 股、35,823 元，"
        "作為日後損益與回本計算基準。"
    )
    page = client.get(URL)
    assert "確認採用此成本" in page.text
    assert "歷史資料已用盡" in page.text
    response = client.post(
        "/corporate-actions/ca/review",
        data={
            "broker_account_id": "a",
            "decision": "confirmed",
            "note": basis,
            "review_token": _form_token(page),
        },
    )
    assert response.status_code == 200
    assert "已採用成本基準" in response.text
    assert basis in response.text
    assert "也不代表券商核實" in response.text
    assert _protected(conn) == before
    assert trades.compute_position(conn, "a", "s") == position_before
    assert position_before.remaining_quantity == 2000
    assert position_before.remaining_cost == 35823
    home = client.get("/").text
    assert "持股摘要含暫估成本" not in home
    # Account B still needs its own review; adopting A never hides B's event.
    assert "未處理的股票異動（1 筆）" in home
    for path in (
        "/",
        "/securities/s",
        "/sale-simulation?broker_account_id=a&security_id=s&quantity=100&price=20",
    ):
        page = client.get(path)
        assert page.status_code == 200
        assert "成本暫估" not in page.text
    assert "券商a · 已採用成本基準" in client.get("/corporate-actions").text
    history, more = reviews.history_page(conn, "ca", "a")
    assert not more
    assert [row["decision"] for row in history] == ["confirmed", "accepted_estimate"]
    assert history[0]["note"] == basis


def test_form_errors_preserve_input_escape_html_and_reject_cross_origin(client):
    token = _form_token(client.get(URL))
    data = {
        "broker_account_id": "a",
        "decision": "confirmed",
        "note": "<script>alert(1)</script>",
        "review_token": "old",
    }
    response = client.post("/corporate-actions/ca/review", data=data)
    assert response.status_code == 409
    assert "&lt;script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert 'value="confirmed" required checked' in response.text
    data["review_token"] = token
    response = client.post(
        "/corporate-actions/ca/review",
        data=data,
        headers={"Origin": "https://example.org"},
    )
    assert response.status_code == 403
    data["decision"] = ""
    assert client.post("/corporate-actions/ca/review", data=data).status_code == 422
    data["decision"] = "reopened"
    assert client.post("/corporate-actions/ca/review", data=data).status_code == 422


def test_unknown_cost_form_explains_repair_and_cannot_be_submitted(client, conn):
    conn.execute(
        "UPDATE trades SET executed_amount=NULL "
        "WHERE broker_account_id='a' AND event_type='opening'"
    )
    page = client.get(URL)
    assert "成本未知，請先補齊交易成本再確認" in page.text
    assert 'name="decision"' not in page.text
    current = reviews.preview(conn, "ca", "a")
    response = client.post(
        "/corporate-actions/ca/review",
        data={
            "broker_account_id": "a",
            "decision": "confirmed",
            "note": "仍不可確認",
            "review_token": current["review_token"],
        },
    )
    assert response.status_code == 422
    assert not reviews.history_page(conn, "ca", "a")[0]


def test_reopen_form_requires_a_review_and_has_same_origin_protection(client):
    data = {
        "broker_account_id": "a",
        "note": "重新核對",
        "review_token": _form_token(client.get(URL)),
    }
    assert client.post("/corporate-actions/ca/reopen", data=data).status_code == 422
    assert (
        client.post(
            "/corporate-actions/ca/reopen",
            data=data,
            headers={"Origin": "https://example.org"},
        ).status_code
        == 403
    )


def test_broken_position_returns_actionable_error_instead_of_stack_trace(client, conn):
    conn.execute(
        "INSERT INTO trades(id,broker_account_id,security_id,event_type,"
        "trade_date,quantity) "
        "VALUES('bad-sell','a','s','sell','2025-05-01',3000)"
    )
    assert client.get(URL).status_code == 422
    response = client.post(
        "/corporate-actions/ca/review",
        data={
            "broker_account_id": "a",
            "decision": "confirmed",
            "note": "核對",
            "review_token": "x",
        },
    )
    assert response.status_code == 422
    assert "超賣" in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize("action,account", [("missing", "a"), ("ca", "missing")])
def test_missing_entities_are_404(client, action, account):
    assert (
        client.get(
            f"/corporate-actions/{action}/review?broker_account_id={account}"
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/corporate-actions/{action}/review",
            data={
                "broker_account_id": account,
                "decision": "confirmed",
                "note": "依據",
                "review_token": "x",
            },
        ).status_code
        == 404
    )


def test_confirmation_amounts_round_to_whole_ntd_without_truncating_snapshot(
    client, conn
):
    conn.execute(
        "UPDATE trades SET executed_amount='29713.50' WHERE event_type='opening' AND "
        "broker_account_id='a'"
    )
    page = client.get(URL)
    assert "35,824" in page.text
    _record(conn)
    assert reviews.history_page(conn, "ca", "a")[0][0]["total_cost"] == "35823.50"


def test_upgrade_down_upgrade_preserves_existing_data(conn, rollback_sql):
    before = _protected(conn)
    position = trades.compute_position(conn, "a", "s")
    expected_breakeven = pricing.compute_breakeven(
        position, pricing.default_fee_schedule("stock")
    )
    _record(conn)
    conn.executescript(rollback_sql("rollback-corporate-action-reviews.sql"))
    assert 15 not in migrate.get_applied_versions(conn)
    assert migrate.apply_pending(conn) == [15]
    assert _protected(conn) == before
    assert not reviews.history_page(conn, "ca", "a")[0]
    after = trades.compute_position(conn, "a", "s")
    assert after == position
    assert (
        pricing.compute_breakeven(after, pricing.default_fee_schedule("stock"))
        == expected_breakeven
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
