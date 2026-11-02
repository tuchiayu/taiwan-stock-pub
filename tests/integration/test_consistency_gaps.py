"""Issue #117: contracts spanning rules, snapshots, official corrections and jobs."""

import json
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import operations, sync, sync_stock_dividends
from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import (
    get_connection,
    read_transaction,
    write_transaction,
)
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import (
    action_revisions,
    corporate_actions,
    distribution_entitlement,
    income_calendar,
    pricing,
    settings,
    trades,
)
from taiwan_stock.portfolio.trading_rules import (
    ETF_TICK_SCHEDULE,
    resolve_trading_rules,
)


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        conn.execute(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES('s','TWSE','00679B','債券','etf','active')"
        )
        yield conn


def fields(**overrides):
    return dict(
        broker_account_id="a",
        security_id="s",
        event_type="sell",
        trade_date="2026-10-01",
        quantity=1000,
        unit_price="20",
        **overrides,
    )


@pytest.mark.parametrize(
    "code,kind,market,day,daytrade,expected",
    [
        ("00679B", "etf", "TWSE", "2016-12-31", False, "20"),
        ("00679B", "etf", "TWSE", "2017-01-01", False, "0"),
        ("00679B", "etf", "TPEx", "2026-12-31", False, "0"),
        ("00679B", "etf", "TWSE", "2027-01-01", False, "20"),
        ("00680C", "etf", "TWSE", "2026-10-01", False, "0"),
        ("00982D", "etf", "TWSE", "2026-10-01", False, "20"),
        ("00631L", "etf", "TWSE", "2026-10-01", True, "20"),
        ("00632R", "etf", "TWSE", "2026-10-01", True, "20"),
        ("00980T", "etf", "TWSE", "2026-10-01", False, "20"),
        ("2330", "stock", "TWSE", "2017-04-27", True, "60"),
        ("2330", "stock", "TWSE", "2017-04-28", True, "30"),
        ("2330", "stock", "TWSE", "2027-12-31", True, "30"),
        ("2330", "stock", "TWSE", "2028-01-01", True, "60"),
    ],
)
def test_dated_rules_and_manual_override(
    conn, code, kind, market, day, daytrade, expected
):
    conn.execute(
        "UPDATE securities SET code=?,security_type=?,market=?", (code, kind, market)
    )
    value = fields()
    value.update(trade_date=day, is_day_trade=daytrade)
    assert trades.prepare(conn, value)["transaction_tax"] == expected
    value["transaction_tax"] = "17"
    assert trades.prepare(conn, value)["transaction_tax"] == "17"


def test_unknown_classification_and_global_zero_are_visible(conn):
    conn.execute("UPDATE securities SET code='NOT-A-CODE'")
    settings.save_settings(conn, {"tax_rate_etf": "0"})
    security = conn.execute("SELECT * FROM securities").fetchone()
    rules = resolve_trading_rules(conn, security, "2026-01-01")
    assert len(rules.warnings) == 2
    assert rules.fees.tax_rate == 0


def test_etf_tick_is_independent_of_tax_and_checks_first_legal_price():
    fees = pricing.default_fee_schedule("etf")
    result = pricing.find_breakeven_price(
        Decimal(1000), Decimal(100000), fees, tick_schedule=ETF_TICK_SCHEDULE
    )
    assert result == Decimal("100.25")
    assert fees.net_proceeds(result - Decimal(".05"), Decimal(1000)) < 100000
    for cost in (Decimal("49900"), Decimal("50000"), Decimal("50100")):
        actual = pricing.find_breakeven_price(
            Decimal(1000), cost, fees, tick_schedule=ETF_TICK_SCHEDULE
        )
        legal = [Decimal(i) / 100 for i in range(4900, 5000)] + [
            Decimal(i) / 100 for i in range(5000, 5101, 5)
        ]
        assert actual == next(
            p for p in legal if fees.net_proceeds(p, Decimal(1000)) >= cost
        )


def test_quote_is_readonly_and_preserves_explicit_fees(conn, tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        before = list(conn.iterdump())
        payload = {
            "brokerAccountId": "a",
            "securityId": "s",
            "eventType": "sell",
            "tradeDate": "2026-10-01",
            "quantity": 1000,
            "unitPrice": "20",
            "commission": "7",
            "transactionTax": "3",
            "otherFees": "2",
        }
        response = client.post("/api/v2/trade-fee-quotes", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["netAmount"] == "19988"
        assert response.json()["transactionTax"] == "3"
        assert "id" not in response.json()
        assert list(conn.iterdump()) == before
        payload["transactionTax"] = None
        assert (
            client.post("/api/v2/trade-fee-quotes", json=payload).json()[
                "transactionTax"
            ]
            == "0"
        )
        payload["tradeDate"] = "invalid"
        assert client.post("/api/v2/trade-fee-quotes", json=payload).status_code == 422


def buy(conn, quantity=1000, account="a"):
    value = fields()
    value.update(
        event_type="buy",
        trade_date="2026-01-01",
        quantity=quantity,
        broker_account_id=account,
    )
    return trades.create_trade(conn, value)


def add_distribution(conn):
    sync.upsert_distribution(
        conn,
        "s",
        {
            "ex_dividend_date": "2026-02-01",
            "pay_date": "2026-03-01",
            "amount_per_share": "1",
            "amount_status": "final",
        },
    )


def test_entitlements_hold_one_wal_snapshot_while_writer_commits(conn, tmp_path):
    trade = buy(conn)
    add_distribution(conn)
    with closing(get_connection(tmp_path)) as writer:
        changed = False

        def interleave(sql):
            nonlocal changed
            if not changed and "SELECT d.*" in sql:
                changed = True
                writer.execute(
                    "UPDATE trades SET voided_at='2026-10-01' WHERE id=?", (trade,)
                )

        conn.set_trace_callback(interleave)
        try:
            entries = distribution_entitlement.list_entitlements(conn)
        finally:
            conn.set_trace_callback(None)
        assert changed and len(entries) == 1
        assert not conn.in_transaction
        assert distribution_entitlement.list_entitlements(conn) == []


def test_nested_read_does_not_complete_callers_transaction(conn):
    with write_transaction(conn):
        conn.execute("UPDATE securities SET name='pending'")
        with read_transaction(conn):
            with read_transaction(conn):
                assert conn.in_transaction
        assert conn.in_transaction
    assert conn.execute("SELECT name FROM securities").fetchone()[0] == "pending"
    with pytest.raises(ValueError), read_transaction(conn):
        raise ValueError("release")
    assert not conn.in_transaction


def test_settings_batch_rolls_back_as_one_unit(conn):
    conn.execute(
        "CREATE TRIGGER reject_setting BEFORE INSERT ON app_settings "
        "WHEN new.key='tax_rate_etf' BEGIN SELECT RAISE(ABORT,'test'); END"
    )
    with pytest.raises(Exception, match="test"):
        settings.save_settings(conn, {"min_commission": "1", "tax_rate_etf": ".002"})
    assert settings.read_settings(conn)["min_commission"] == "20"


def conflict(conn):
    buy(conn)
    add_distribution(conn)
    action = {
        "security_id": "s",
        "action_type": "stock_dividend",
        "ex_date": "2026-04-01",
        "ratio_numerator": 1100,
        "ratio_denominator": 1000,
    }
    action_id = corporate_actions.create_corporate_action(conn, action)
    value = fields()
    value.update(trade_date="2026-05-01", quantity=1080)
    sale = trades.create_trade(conn, value)
    sync.upsert_corporate_action(conn, "s", {**action, "ratio_numerator": 1050})
    return action_id, sale, action


def test_revision_audit_and_current_completeness_survive_resolution(conn, tmp_path):
    action_id, sale, action = conflict(conn)
    revisions = action_revisions.list_revisions(conn)
    assert len(revisions) == 1 and revisions[0]["action_id"] == action_id
    assert revisions[0]["impacts"][0]["before"] is None
    assert revisions[0]["impacts"][0]["after"]["shortfall"] == "30"
    sync.upsert_corporate_action(conn, "s", {**action, "ratio_numerator": 1050})
    assert len(action_revisions.list_revisions(conn)) == 1
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/v1/income-calendar").status_code == 422
        result = client.get("/api/v2/income-calendar").json()
        assert result["items"] == [] and result["dataComplete"] is False
        assert result["excludedPositions"][0]["tradeId"] == sale
        page = client.get("/").text
        assert "合計不完整" in page and "還沒有持股紀錄" not in page
        assert client.get("/corporate-actions").status_code == 200
    trades.update_trade(conn, sale, {"quantity": 1000})
    result = income_calendar.calendar_result(conn)
    assert result["data_complete"] and len(result["items"]) == 1
    assert len(action_revisions.list_revisions(conn)) == 1


def test_revision_rolls_back_official_change_if_audit_fails(conn, monkeypatch):
    _, _, action = conflict(conn)

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(action_revisions, "record", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        sync.upsert_corporate_action(conn, "s", {**action, "ratio_numerator": 1060})
    assert (
        conn.execute("SELECT ratio_numerator FROM corporate_actions").fetchone()[0]
        == 1050
    )
    assert len(action_revisions.list_revisions(conn)) == 1


def test_validation_failure_keeps_cash_and_worker_is_partial(conn, tmp_path):
    adapter = SimpleNamespace(
        MARKET="TWSE",
        fetch_ex_dividend_notices=lambda client: [{}],
        normalize_ex_dividend_notice=lambda row: (
            {
                "code": "00679B",
                "ex_dividend_date": "2026-04-01",
                "amount_per_share": "1",
                "amount_status": "final",
            },
            {
                "code": "00679B",
                "action_type": "stock_dividend",
                "ex_date": "2026-04-01",
                "ratio_numerator": 900,
                "ratio_denominator": 1000,
            },
        ),
    )

    def execute(c, client, *_):
        return operations.step(
            c,
            "TWSE 除權息公告",
            lambda: sync_stock_dividends.sync_ex_dividend_notices(c, client, adapter),
        )

    job = sync_jobs.submit(
        conn,
        tmp_path,
        "stock-announcements",
        launcher=lambda d, j: sync_jobs.run_worker(d, j, executor=execute),
    )
    assert job["status"] == "partial"
    assert job["result"]["skipped_invalid_ratio"] == 1
    assert job["result"]["validation_errors"][0]["code"] == "00679B"
    assert conn.execute("SELECT count(*) FROM distributions").fetchone()[0] == 1
    assert not conn.execute("SELECT * FROM corporate_actions").fetchall()
    assert json.loads(conn.execute("SELECT result FROM sync_job_steps").fetchone()[0])[
        "validation_errors"
    ]


def test_retry_only_failed_market_and_accepts_old_success_result(
    conn, tmp_path, monkeypatch
):
    calls = []
    valid = False

    def notices(c, client, adapter=None):
        market = adapter.MARKET if adapter else "TWSE"
        calls.append(market)
        return {
            "distributions": 1,
            "corporate_actions": 0,
            "skipped_unknown_security": 0,
            "skipped_invalid_ratio": int(market == "TWSE" and not valid),
        }

    monkeypatch.setattr(sync_stock_dividends, "sync_ex_dividend_notices", notices)
    monkeypatch.setattr(
        sync_stock_dividends, "sync_stock_dividend_history", lambda *_: {}
    )
    monkeypatch.setattr(
        sync_stock_dividends, "sync_stock_dividend_pay_dates", lambda *_: {}
    )

    def launcher(d, j):
        return sync_jobs.run_worker(d, j, executor=operations.execute)

    first = sync_jobs.submit(conn, tmp_path, "stock-announcements", launcher=launcher)
    assert first["status"] == "partial"
    assert calls == ["TWSE", "TPEx"]
    valid = True
    second = sync_jobs.retry(conn, tmp_path, first["id"], launcher=launcher)
    assert second["status"] == "success"
    assert calls == ["TWSE", "TPEx", "TWSE"]
    assert second["result"]["distributions"] == 2
    assert second["result"]["validation_errors"] == []
    assert second["result"]["reconciliation_warnings"] == []


def test_scoped_completeness_excludes_unrelated_account_and_bank(conn, tmp_path):
    conflict(conn)
    conn.execute(
        "UPDATE broker_accounts SET dividend_bank_name='第一銀行' WHERE id='a'"
    )
    conn.execute(
        "INSERT INTO broker_accounts(id,name,status,dividend_bank_name) "
        "VALUES('b','其他','active','臺灣銀行')"
    )
    buy(conn, account="b")
    with TestClient(create_app(tmp_path)) as client:
        for params in [
            {"scope": "account", "account_id": "b"},
            {"scope": "bank", "bank": "臺灣銀行"},
        ]:
            result = client.get("/api/v2/income-calendar", params=params).json()
            assert result["dataComplete"] is True and result["excludedPositions"] == []
            assert len(result["items"]) == 1
        page = client.get("/income-calendar", params={"filter": "account:b"}).text
        assert "交易歷史不一致" not in page
        assert "合計不完整" not in page


@pytest.mark.parametrize("resolution", ["csv", "void", "unvoid"])
def test_resolution_paths_keep_audit_but_clear_current_conflict(conn, resolution):
    if resolution == "unvoid":
        buy(conn, quantity=100)
        extra = conn.execute("SELECT id FROM trades").fetchone()[0]
        trades.void_trade(conn, extra)
    _, sale, _ = conflict(conn)
    if resolution == "csv":
        # CSV's confirmed plan delegates to the same atomic batch operation.
        original = dict(
            conn.execute("SELECT * FROM trades WHERE id=?", (sale,)).fetchone()
        )
        prepared = trades.prepare(conn, {**original, "quantity": 1000})
        trades.apply_batch(conn, [], {sale: prepared})
    elif resolution == "void":
        trades.void_trade(conn, sale)
    else:
        trades.unvoid_trade(conn, extra)
    assert distribution_entitlement.history_warnings(conn) == []
    assert len(action_revisions.list_revisions(conn)) == 1


def test_official_to_official_audit_includes_future_conflict_and_ignores_voided(conn):
    buy(conn)
    action = {
        "action_type": "stock_dividend",
        "ex_date": "2026-04-01",
        "ratio_numerator": 1100,
        "ratio_denominator": 1000,
    }
    sync.upsert_corporate_action(conn, "s", action)
    conn.execute(
        "INSERT INTO broker_accounts(id,name,status) VALUES('b','另一券商','active')"
    )
    buy(conn, account="b")
    for account in ["a", "b"]:
        value = fields()
        value.update(trade_date="2099-01-01", quantity=1080, broker_account_id=account)
        sale = trades.create_trade(conn, value)
        if account == "b":
            trades.void_trade(conn, sale)
    sync.upsert_corporate_action(conn, "s", {**action, "ratio_numerator": 1050})
    revision = action_revisions.list_revisions(conn)[0]
    assert revision["before"]["source"] == "official"
    impacted = [i for i in revision["impacts"] if i["after"] is not None]
    assert [i["broker_account_id"] for i in impacted] == ["a"]
    assert impacted[0]["after"]["shortfall"] == "30"


def test_migration_20_down_up_and_refuses_to_discard_nonempty_audit(conn, rollback_sql):
    sql = rollback_sql("rollback-investment-consistency.sql")
    schedule_sql = rollback_sql("rollback-etf-ex-dividend-schedule.sql")
    identity_sql = rollback_sql("rollback-etf-fund-identity.sql")
    scale_sql = rollback_sql("rollback-etf-scale.sql")
    conn.executescript(scale_sql)
    conn.executescript(identity_sql)
    conn.executescript(schedule_sql)
    conn.executescript(sql)
    assert migrate.apply_pending(conn) == [20, 21, 22, 23]
    conflict(conn)
    conn.executescript(scale_sql)
    conn.executescript(identity_sql)
    conn.executescript(schedule_sql)
    with pytest.raises(Exception, match="CHECK constraint"):
        conn.executescript(sql)
    conn.rollback()
    assert len(action_revisions.list_revisions(conn)) == 1
    assert migrate.apply_pending(conn) == [21, 22, 23]
    migrate.validate_runtime_schema(conn)


def test_patch_date_recalculation_and_explicit_override(conn):
    buy(conn)
    sale = trades.create_trade(conn, fields())
    trades.update_trade(conn, sale, {"trade_date": "2027-01-01"})
    assert (
        conn.execute(
            "SELECT transaction_tax FROM trades WHERE id=?", (sale,)
        ).fetchone()[0]
        == "0"
    )
    trades.update_trade(conn, sale, {}, recalculate=True)
    assert (
        conn.execute(
            "SELECT transaction_tax FROM trades WHERE id=?", (sale,)
        ).fetchone()[0]
        == "20"
    )
    trades.update_trade(
        conn,
        sale,
        {"trade_date": "2026-10-01", "transaction_tax": "9"},
        recalculate=True,
    )
    assert (
        conn.execute(
            "SELECT transaction_tax FROM trades WHERE id=?", (sale,)
        ).fetchone()[0]
        == "9"
    )


def test_html_snapshot_finishes_before_template_render(conn, tmp_path, monkeypatch):
    from taiwan_stock.web.deps import get_db
    from taiwan_stock.web.templates import templates

    buy(conn)
    add_distribution(conn)
    app = create_app(tmp_path)
    app.dependency_overrides[get_db] = lambda: conn
    render = templates.TemplateResponse

    def checked_render(*args, **kwargs):
        assert not conn.in_transaction
        return render(*args, **kwargs)

    monkeypatch.setattr(templates, "TemplateResponse", checked_render)
    with TestClient(app) as client:
        for path in [
            "/",
            "/securities/s",
            "/income-calendar",
            "/sale-simulation?broker_account_id=a&security_id=s&quantity=100",
            "/corporate-actions",
        ]:
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)


def test_calendar_bank_and_distribution_inputs_share_snapshot(conn, tmp_path):
    buy(conn)
    add_distribution(conn)
    conn.execute("UPDATE broker_accounts SET dividend_bank_name='第一銀行'")
    with closing(get_connection(tmp_path)) as writer:
        changed = False

        def interleave(sql):
            nonlocal changed
            if not changed and "SELECT * FROM dividend_bank_preferences" in sql:
                changed = True
                with write_transaction(writer):
                    writer.execute(
                        "UPDATE broker_accounts SET dividend_bank_name='臺灣銀行'"
                    )
                    writer.execute("UPDATE distributions SET amount_per_share='2'")

        conn.set_trace_callback(interleave)
        try:
            result = income_calendar.calendar_result(conn)
        finally:
            conn.set_trace_callback(None)
        assert changed
        assert result["items"][0]["bank"] == "第一銀行"
        assert result["items"][0]["expected_gross"] == 1000
        current = income_calendar.calendar_result(conn)
        assert current["items"][0]["bank"] == "臺灣銀行"
        assert current["items"][0]["expected_gross"] == 2000


def test_position_fee_and_action_changes_cannot_mix_with_old_trades(conn, tmp_path):
    buy(conn)
    with closing(get_connection(tmp_path)) as writer:
        changed = False

        def interleave(sql):
            nonlocal changed
            if not changed and "SELECT key, value FROM app_settings" in sql:
                changed = True
                with write_transaction(writer):
                    writer.execute(
                        "INSERT INTO app_settings VALUES('min_commission','100')"
                    )
                    writer.execute("UPDATE trades SET quantity=2000")

        conn.set_trace_callback(interleave)
        try:
            positions = trades.list_position_overview(conn)
        finally:
            conn.set_trace_callback(None)
        assert changed
        assert positions[0]["position"].remaining_quantity == 1000
        current = trades.list_position_overview(conn)
        assert current[0]["position"].remaining_quantity == 2000
        assert current[0]["breakeven_price"] != positions[0]["breakeven_price"]


def test_csv_dates_explicit_tax_and_stale_rule_inputs(conn, tmp_path, monkeypatch):
    from taiwan_stock.portfolio import csv_trades, trade_imports, trading_rules

    buy(conn, quantity=4000)
    content = (
        "broker_account_name,security_code,market,event_type,trade_date,quantity,unit_price,transaction_tax\n"
        "券商,00679B,TWSE,sell,2026-12-31,1000,20,\n"
        "券商,00679B,TWSE,sell,2027-01-01,1000,20,\n"
        "券商,00679B,TWSE,sell,2027-01-02,1000,20,7\n"
    )
    preview_id, plan = trade_imports.create_preview(conn, tmp_path, content)
    assert [r.values["transaction_tax"] for r in plan.rows] == ["0", "20", "7"]
    monkeypatch.setattr(
        trading_rules,
        "TAX_EXEMPT_PERIODS",
        (
            trading_rules.RulePeriod(
                "passive-bond-etf", "2017-01-01", "2027-12-31", "被動式債券 ETF", ""
            ),
        ),
    )
    with pytest.raises(csv_trades.StalePreviewError):
        trade_imports.confirm_preview(conn, tmp_path, preview_id, plan.digest, [])
    assert (
        conn.execute("SELECT count(*) FROM trades WHERE event_type='sell'").fetchone()[
            0
        ]
        == 0
    )
    assert not list((tmp_path / "backups").glob("*.db"))


def test_etf_all_breakeven_entrypoints_use_same_schedule(conn, tmp_path):
    from taiwan_stock.portfolio import corporate_action_reviews

    conn.execute("UPDATE securities SET code='0050'")
    value = fields()
    value.update(
        event_type="opening",
        trade_date="2026-01-01",
        unit_price=None,
        executed_amount="100000",
    )
    trades.create_trade(conn, value)
    action = corporate_actions.create_corporate_action(
        conn,
        {
            "security_id": "s",
            "action_type": "other",
            "ex_date": "2026-04-01",
            "note": "核對成本",
        },
    )
    expected = Decimal("100.25")
    overview = trades.list_position_overview(conn)[0]
    assert overview["breakeven_price"] == expected
    assert overview["income_breakeven"] == expected
    assert overview["lot_breakevens"][0]["breakeven_price"] == expected
    assert (
        corporate_action_reviews.preview(conn, action, "a")["breakeven_price"]
        == expected
    )
    with TestClient(create_app(tmp_path)) as client:
        position = client.get(
            "/api/v1/positions", params={"broker_account_id": "a", "security_id": "s"}
        ).json()
        assert Decimal(position["breakeven_price"]) == expected
        result = client.post(
            "/api/v1/sale-simulations",
            json={"broker_account_id": "a", "security_id": "s", "quantity": 1000},
        ).json()
        assert Decimal(result["breakeven_price"]) == expected
        page = client.get(
            "/sale-simulation",
            params={"broker_account_id": "a", "security_id": "s", "quantity": 1000},
        )
        assert page.context["result"].breakeven_price == expected
