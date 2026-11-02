"""Regression cases for design 031; all writes use disposable databases."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import price_storage, sync_daily_prices
from taiwan_stock.infrastructure import backup, migrate
from taiwan_stock.infrastructure.db import get_connection, write_transaction
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import csv_trades, replay, trade_query, trades

HEADER = (
    "broker_account_name,security_code,market,event_type,"
    "trade_date,quantity,unit_price\n"
)
BUY_CSV = HEADER + "Broker,a,TWSE,buy,2026-01-01,100,10\n"


@pytest.fixture
def conn(tmp_path):
    c = get_connection(tmp_path)
    migrate.apply_pending(c)
    c.execute(
        "INSERT INTO broker_accounts(id,name,status) VALUES('acct','Broker','active')"
    )
    c.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('a','TWSE','a','A','stock','active')"
    )
    yield c
    c.close()


def fields(**changes):
    return {
        "broker_account_id": "acct",
        "security_id": "a",
        "event_type": "buy",
        "trade_date": "2026-01-01",
        "quantity": 100,
        "unit_price": "10",
        **changes,
    }


@pytest.mark.parametrize("editing", [False, True])
def test_overlapping_writes_validate_after_the_writer_owns_the_lock(
    conn, tmp_path, monkeypatch, editing
):
    trades.create_trade(conn, fields())
    ids = []
    if editing:
        for day in ("2026-01-02", "2026-01-03"):
            ids.append(
                trades.create_trade(
                    conn, fields(event_type="sell", quantity=30, trade_date=day)
                )
            )
    first_validated, release, second_attempted, second_validating = (
        threading.Event() for _ in range(4)
    )
    original = trades._validate

    def validate(c, candidate, **kwargs):
        if candidate["note"] == "second":
            second_validating.set()
        original(c, candidate, **kwargs)
        if candidate["note"] == "first":
            first_validated.set()
            assert release.wait(10)

    monkeypatch.setattr(trades, "_validate", validate)

    def sell(index):
        c = get_connection(tmp_path)
        try:
            if index == 1:
                second_attempted.set()
            values = {
                "quantity": 70 if editing else 80,
                "note": ("first", "second")[index],
            }
            try:
                if editing:
                    trades.update_trade(c, ids[index], values)
                else:
                    trades.create_trade(
                        c, fields(event_type="sell", trade_date="2026-01-02", **values)
                    )
                return "success"
            except replay.OversellError:
                return "oversell"
        finally:
            c.close()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(sell, 0)
        try:
            assert first_validated.wait(10)
            second = pool.submit(sell, 1)
            assert second_attempted.wait(10)
            # Event.wait is intentionally unaffected by the suite's sleep stub.
            assert not second_validating.wait(0.15)
        finally:
            release.set()
        assert [first.result(10), second.result(10)] == ["success", "oversell"]
    assert trades.compute_position(
        conn, "acct", "a", "2026-10-21"
    ).remaining_quantity == (0 if editing else 20)


def test_concurrent_confirmations_of_one_preview_import_once(
    conn, tmp_path, monkeypatch
):
    digest = csv_trades.preview_import(conn, BUY_CSV).digest
    barrier, done = threading.Barrier(2), threading.Event()
    local = threading.local()
    original = csv_trades.parse_csv

    def parse(content):
        rows = original(content)
        barrier.wait(10)
        if local.index == 1:
            assert done.wait(10)
        return rows

    monkeypatch.setattr(csv_trades, "parse_csv", parse)

    def upload(index):
        local.index = index
        c = get_connection(tmp_path)
        try:
            try:
                return csv_trades.confirm_import(c, BUY_CSV, digest, ()).created
            except csv_trades.StalePreviewError:
                # Re-planned under the lock, the row is now a duplicate.
                return "stale"
        finally:
            c.close()
            if index == 0:
                done.set()

    with ThreadPoolExecutor(2) as pool:
        pending = [pool.submit(upload, i) for i in range(2)]
        assert [f.result(20) for f in pending] == [1, "stale"]
    assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1


def test_a_late_write_failure_rolls_back_the_whole_import(conn):
    conn.executescript("""
        CREATE TRIGGER reject_second_row BEFORE INSERT ON trades
        WHEN NEW.note = 'boom'
        BEGIN SELECT RAISE(ABORT, 'write failed'); END;
    """)
    content = (
        "broker_account_name,security_code,market,event_type,"
        "trade_date,quantity,unit_price,note\n"
        "Broker,a,TWSE,buy,2026-01-01,100,10,\n"
        "Broker,a,TWSE,buy,2026-01-02,100,10,boom\n"
    )
    digest = csv_trades.plan_import(conn, content).digest
    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        csv_trades.confirm_import(conn, content, digest, ())
    assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_nested_writes_do_not_commit_the_outer_transaction(conn):
    with pytest.raises(RuntimeError):
        with write_transaction(conn):
            csv_trades.import_trades(conn, csv_trades.parse_csv(BUY_CSV))
            raise RuntimeError("cancel batch")
    assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM app_settings").fetchone()[0] == 0


@pytest.mark.parametrize("value", ["2026-02-30", "20260101", "2026-W01-1", "", None])
def test_invalid_dates_rejected_in_domain_and_api(conn, tmp_path, value):
    with pytest.raises(trades.TradeValidationError, match="trade_date"):
        trades.create_trade(conn, fields(trade_date=value))
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/api/v1/trades", json=fields(trade_date=value))
    assert response.status_code == 422
    assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_invalid_csv_date_is_reported_with_row_and_rolls_back(conn):
    with pytest.raises(csv_trades.ImportError_) as error:
        csv_trades.preview_import(
            conn, BUY_CSV + "Broker,a,TWSE,buy,2026-02-30,50,10\n"
        )
    assert error.value.row_number == 3
    assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_invalid_event_returns_422(conn, tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/api/v1/trades", json=fields(event_type="invalid"))
    assert response.status_code == 422


def test_patch_clears_nullable_fields_and_recalculates_only_cleared_fees(
    conn, tmp_path
):
    trade_id = trades.create_trade(
        conn, fields(note="old", commission="99", other_fees="5")
    )
    with TestClient(create_app(tmp_path)) as client:
        response = client.patch(
            f"/api/v1/trades/{trade_id}", json={"note": None, "commission": None}
        )
        assert response.status_code == 200
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        assert row["note"] is None
        assert row["commission"] == "20"
        assert row["other_fees"] == "5"
        assert row["executed_amount"] == "1000"
        assert (
            client.patch(f"/api/v1/trades/{trade_id}", json={"note": "new"}).status_code
            == 200
        )
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        assert row["commission"] == "20" and row["note"] == "new"


@pytest.mark.parametrize("key", ["quantity", "trade_date", "event_type"])
def test_patch_required_null_returns_422(conn, tmp_path, key):
    trade_id = trades.create_trade(conn, fields())
    with TestClient(create_app(tmp_path)) as client:
        assert (
            client.patch(f"/api/v1/trades/{trade_id}", json={key: None}).status_code
            == 422
        )
    assert conn.execute("SELECT quantity FROM trades").fetchone()[0] == 100


def test_same_millisecond_csv_round_trip_preserves_fifo_cost(conn, monkeypatch):
    content = (
        BUY_CSV
        + "Broker,a,TWSE,buy,2026-01-01,100,20\nBroker,a,TWSE,sell,2026-01-01,150,30\n"
    )
    monkeypatch.setattr(trades, "new_id", iter(["z-buy", "y-buy", "a-sell"]).__next__)
    monkeypatch.setattr(trades, "now_utc_iso", lambda: "2026-10-21T14:00:00.000Z")
    csv_trades.import_trades(conn, csv_trades.parse_csv(content))
    rows = trades.fetch_trades(conn, "acct", "a")
    before = replay.replay_trades(list(reversed(rows)), [], "2026-10-21")
    assert before.remaining_quantity == 50 and before.remaining_cost == Decimal(
        "1010.00"
    )
    exported = csv_trades.export_trades_csv(conn)
    assert [r["event_type"] for r in csv_trades.parse_csv(exported)] == [
        "buy",
        "buy",
        "sell",
    ]
    assert [r["id"] for r in trade_query.browse(conn, {})["transactions"]] == [
        "a-sell",
        "y-buy",
        "z-buy",
    ]
    conn.execute("DELETE FROM trades")
    monkeypatch.setattr(trades, "new_id", iter(["x-buy", "w-buy", "b-sell"]).__next__)
    csv_trades.import_trades(conn, csv_trades.parse_csv(exported))
    after = trades.compute_position(conn, "acct", "a", "2026-10-21")
    assert (before.remaining_quantity, before.remaining_cost) == (
        after.remaining_quantity,
        after.remaining_cost,
    )


def test_order_migration_backup_and_upgrade_preserve_legacy_fifo(tmp_path, monkeypatch):
    c = get_connection(tmp_path)
    migrations = migrate.discover_migrations()
    with monkeypatch.context() as m:
        m.setattr(
            migrate,
            "discover_migrations",
            lambda: [m for m in migrations if m.version <= 16],
        )
        migrate.apply_pending(c)
    c.execute(
        "INSERT INTO broker_accounts(id,name,status) VALUES('acct','Broker','active')"
    )
    c.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('a','TWSE','a','A','stock','active')"
    )
    for tid, kind, qty, cost in [
        ("z", "buy", 100, "1000"),
        ("y", "buy", 100, "2000"),
        ("a", "sell", 150, "4500"),
    ]:
        c.execute(
            "INSERT INTO trades(id,broker_account_id,security_id,event_type,"
            "trade_date,quantity,executed_amount,created_at) "
            "VALUES(?,'acct','a',?,'2026-01-01',?,?, '2026-10-21T14:00:00.000Z')",
            (tid, kind, qty, cost),
        )
    # The legacy schema predates the live_trades view, so replay raw rows.
    old = replay.replay_trades(
        c.execute("SELECT * FROM trades").fetchall(), [], as_of_date="2026-10-21"
    )
    # The backup was created by the compatible old application before upgrade.
    with monkeypatch.context() as old_program:
        old_program.setattr(
            migrate,
            "discover_migrations",
            lambda: [m for m in migrations if m.version <= 16],
        )
        saved = backup.create_backup(tmp_path)
    assert migrate.apply_pending(c) == [17, 18, 19, 20, 21, 22, 23]
    assert migrate.apply_pending(c) == []
    current = trades.compute_position(c, "acct", "a", "2026-10-21")
    assert current.remaining_cost == old.remaining_cost == 1000
    assert [
        r[0] for r in c.execute("SELECT trade_order FROM trades ORDER BY trade_order")
    ] == [1, 2, 3]
    c.close()
    # The restore path upgrades a disposable copy of the legacy backup.
    backup.restore_backup(tmp_path, saved.name)
    c = get_connection(tmp_path)
    assert migrate.apply_pending(c) == []
    assert trades.compute_position(c, "acct", "a", "2026-10-21").remaining_cost == 1000
    trades.update_trade(c, "y", {"note": "edited"})
    assert c.execute("SELECT trade_order FROM trades WHERE id='y'").fetchone()[0] == 2
    c.close()


def test_migration_can_be_reversed_and_reapplied_without_losing_trades(
    conn, rollback_sql
):
    tid = trades.create_trade(conn, fields(commission="3"))
    before = trades.compute_position(conn, "acct", "a", "2026-10-21")
    for later in (
        "rollback-etf-scale.sql",
        "rollback-etf-fund-identity.sql",
        "rollback-etf-ex-dividend-schedule.sql",
        "rollback-investment-consistency.sql",
        "rollback-latent-risks.sql",
        "rollback-investment-edge-cases.sql",
    ):
        conn.executescript(rollback_sql(later))
    conn.executescript(rollback_sql("rollback-investment-data-integrity.sql"))
    assert conn.execute("SELECT id FROM trades").fetchone()[0] == tid
    assert migrate.apply_pending(conn) == [17, 18, 19, 20, 21, 22, 23]
    after = trades.compute_position(conn, "acct", "a", "2026-10-21")
    assert (before.remaining_quantity, before.remaining_cost) == (
        after.remaining_quantity,
        after.remaining_cost,
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def run_prices(conn, fetch):
    return sync_daily_prices.sync_daily_prices_by_date(
        conn,
        None,
        SimpleNamespace(MARKET="TWSE", fetch_daily_prices_on=fetch),
        holidays=set(),
        today="2026-10-21",
        sleep=lambda _: None,
    )


def quote(code, day):
    return {"code": code, "trade_date": day, "close": "10", "volume": 100}


def test_partial_legacy_quotes_are_rechecked_and_new_codes_are_backfilled(conn):
    for day in ("2026-10-20", "2026-10-21"):
        conn.execute("INSERT INTO daily_prices VALUES('a',?,'10',100)", (day,))
    calls = []

    def fetch(_, day):
        calls.append(day)
        return [quote("a", day), quote("b", day)]

    run_prices(conn, fetch)
    assert len(calls) == 2  # Legacy prices alone are not coverage.
    run_prices(conn, fetch)
    assert len(calls) == 2
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('b','TWSE','b','B','stock','active')"
    )
    run_prices(conn, fetch)
    assert len(calls) == 4
    assert (
        conn.execute(
            "SELECT count(*) FROM daily_prices WHERE security_id='b'"
        ).fetchone()[0]
        == 2
    )
    run_prices(conn, fetch)
    assert len(calls) == 4
    conn.execute(
        "DELETE FROM daily_prices WHERE security_id='b' AND trade_date='2026-10-20'"
    )
    run_prices(conn, fetch)
    assert len(calls) == 5


def test_checked_no_quote_does_not_refetch_forever(conn):
    calls = []

    def fetch(_, day):
        calls.append(day)
        return [{**quote("a", day), "close": None, "status": "no_quote"}]

    run_prices(conn, fetch)
    run_prices(conn, fetch)
    assert len(calls) == 2
    assert conn.execute("SELECT count(*) FROM daily_prices").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT count(*) FROM daily_price_coverage WHERE close IS NULL"
        ).fetchone()[0]
        == 2
    )


def test_catalog_change_during_fetch_is_not_marked_covered(conn):
    def fetch(_, day):
        conn.execute(
            "INSERT OR IGNORE INTO securities"
            "(id,market,code,name,security_type,status) "
            "VALUES('b','TWSE','b','B','stock','active')"
        )
        return [quote("a", day)]

    run_prices(conn, fetch)
    assert not price_storage._market_has_prices(conn, "TWSE", "2026-10-21")


def test_fallback_quote_does_not_create_date_report_coverage(conn):
    price_storage._store_daily_prices(conn, "TWSE", [quote("a", "2026-10-21")])
    assert not price_storage._market_has_prices(conn, "TWSE", "2026-10-21")


def test_price_and_coverage_failure_roll_back_together(conn):
    conn.executescript("""
        CREATE TRIGGER reject_coverage BEFORE INSERT ON daily_price_coverage
        BEGIN SELECT RAISE(ABORT, 'coverage failed'); END;
    """)
    with pytest.raises(sqlite3.IntegrityError, match="coverage failed"):
        run_prices(conn, lambda _, day: [quote("a", day)])
    assert conn.execute("SELECT count(*) FROM daily_prices").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM daily_price_reports").fetchone()[0] == 0
