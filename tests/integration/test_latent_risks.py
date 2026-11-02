"""Issue #115: the twelve latent data, sync and restore risks."""

import json
import sqlite3
import threading
import time
from contextlib import closing
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import sync
from taiwan_stock.infrastructure import backup, migrate, sync_jobs
from taiwan_stock.infrastructure.db import get_connection, write_transaction
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import corporate_actions, csv_trades, replay, trades


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as connection:
        migrate.apply_pending(connection)
        connection.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('acc','券商','active')"
        )
        connection.execute(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES('sec','TWSE','0050','元大台灣50','etf','active')"
        )
        yield connection


def _trade(conn, event_type, trade_date, quantity, **fields):
    return trades.create_trade(
        conn,
        {
            "broker_account_id": "acc",
            "security_id": "sec",
            "event_type": event_type,
            "trade_date": trade_date,
            "quantity": quantity,
            "unit_price": "100",
            **fields,
        },
    )


# 1, 11: schema versions and restore sources


def test_newer_schema_is_rejected_instead_of_silently_ignored(conn):
    conn.execute("INSERT INTO schema_migrations(version, checksum) VALUES (9999,'x')")
    with pytest.raises(RuntimeError, match="9999"):
        migrate.apply_pending(conn)


def test_restore_rejects_a_newer_backup_before_touching_live_data(conn, tmp_path):
    saved = backup.create_backup(tmp_path)
    with closing(sqlite3.connect(saved)) as raw:
        raw.execute(
            "INSERT INTO schema_migrations(version, checksum) VALUES (9999,'x')"
        )
        raw.commit()
    before = {p.name for p in (tmp_path / "backups").glob("*.db")}
    conn.close()
    with pytest.raises(backup.BackupError, match="9999"):
        backup.restore_backup(tmp_path, saved.name)
    # No pre-restore safety copy: nothing was started.
    assert {p.name for p in (tmp_path / "backups").glob("*.db")} == before


def test_restore_rejects_a_database_from_another_program(conn, tmp_path):
    foreign = tmp_path / "backups" / "taiwan_stock-20260101T000000000000Z.db"
    foreign.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(foreign)) as raw:
        raw.execute("CREATE TABLE notes(id INTEGER)")
        raw.commit()
    conn.close()
    with pytest.raises(backup.BackupError, match="不是本程式的備份"):
        backup.restore_backup(tmp_path, foreign.name)


# 2: official corporate actions


def _official(ratio_numerator, ratio_denominator=1000):
    return {
        "action_type": "stock_dividend",
        "ratio_numerator": ratio_numerator,
        "ratio_denominator": ratio_denominator,
        "ex_date": "2026-08-01",
    }


def test_official_data_overwrites_a_manual_ratio_but_keeps_it_in_the_note(conn):
    corporate_actions.create_corporate_action(
        conn,
        {"security_id": "sec", **_official(1100), "note": "暫填"},
    )
    sync.upsert_corporate_action(conn, "sec", _official(1050))
    row = conn.execute("SELECT * FROM corporate_actions").fetchone()
    assert (row["ratio_numerator"], row["source"]) == (1050, "official")
    assert row["note"] == "暫填；官方資料已更新比例（原手動 1100/1000）"


def test_official_data_with_an_impossible_ratio_is_not_written(conn):
    with pytest.raises(corporate_actions.CorporateActionValidationError):
        sync.upsert_corporate_action(conn, "sec", _official(900))
    assert not conn.execute("SELECT * FROM corporate_actions").fetchall()


def test_manual_entries_are_labelled_manual(conn):
    corporate_actions.create_corporate_action(
        conn, {"security_id": "sec", **_official(1100)}
    )
    assert conn.execute("SELECT source FROM corporate_actions").fetchone()[0] == (
        "manual"
    )


# 3: page rendering never takes the sync control lease


def test_pages_render_while_another_process_holds_the_control_lease(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        with sync_jobs.lease(tmp_path, ".sync-control.db"):
            started = time.monotonic()
            assert client.get("/").status_code == 200
            assert time.monotonic() - started < 5


# 4: worker start-up


class _AliveProcess:
    def poll(self):
        return None


def test_submit_waits_outside_the_control_lease(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(sync_jobs, "WORKER_START_SECONDS", 1)
    results = {}

    def slow():
        results["slow"] = sync_jobs.submit(
            conn, tmp_path, "prices", launcher=lambda *_: _AliveProcess()
        )

    thread = threading.Thread(target=slow)
    thread.start()
    time.sleep(0.2)
    # Another admission succeeds while the first submitter is still waiting.
    with closing(get_connection(tmp_path)) as other:
        with sync_jobs.lease(tmp_path, ".sync-control.db", timeout=0.5):
            pass
        assert other.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 1
    thread.join(5)
    assert results["slow"]["status"] == "failed"


def test_fresh_queued_job_survives_recovery_until_the_grace_period_ends(conn, tmp_path):
    def died(*_):
        raise SystemExit()

    with pytest.raises(SystemExit):
        sync_jobs.submit(conn, tmp_path, "prices", launcher=died)
    job_id = conn.execute("SELECT id FROM sync_jobs").fetchone()[0]
    sync_jobs.recover(conn, tmp_path)
    assert sync_jobs.get(conn, job_id)["status"] == "queued"


# 5: unserialisable results


def test_decimal_results_are_stored(conn, tmp_path):
    job = sync_jobs.submit(
        conn,
        tmp_path,
        "prices",
        launcher=lambda d, j: sync_jobs.run_worker(
            d, j, executor=lambda *_: {"amount": Decimal("1.5")}
        ),
    )
    assert job["status"] == "success"
    assert job["result"] == {"amount": "1.5"}


def test_a_result_that_cannot_be_stored_fails_the_job_with_its_reason(conn, tmp_path):
    circular = {}
    circular["self"] = circular
    job = sync_jobs.submit(
        conn,
        tmp_path,
        "prices",
        launcher=lambda d, j: sync_jobs.run_worker(d, j, executor=lambda *_: circular),
    )
    assert job["status"] == "failed"
    assert "同步結果無法儲存" in job["error"]
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()


# 6: lease files and logs


def test_old_finished_job_artifacts_are_pruned_and_active_ones_kept(conn, tmp_path):
    (tmp_path / "sync-logs").mkdir()
    for job_id, status, finished in (
        ("old", "success", "2000-01-01T00:00:00.000Z"),
        ("live", "running", None),
    ):
        conn.execute(
            "INSERT INTO sync_jobs(id,operation,origin,request_key,parameters,status,"
            "submitted_at,finished_at,owner_token) "
            "VALUES(?,'prices','web',?,'{}',?,'2000-01-01T00:00:00.000Z',?,'t')",
            (job_id, job_id, status, finished),
        )
        (tmp_path / f".sync-job-{job_id}.db").touch()
        (tmp_path / "sync-logs" / f"{job_id}.log").touch()
    sync_jobs.prune_artifacts(conn, tmp_path)
    assert not (tmp_path / ".sync-job-old.db").exists()
    assert not (tmp_path / "sync-logs" / "old.log").exists()
    assert (tmp_path / ".sync-job-live.db").exists()
    assert (tmp_path / "sync-logs" / "live.log").exists()
    assert sync_jobs.get(conn, "old")["status"] == "success"


# 7: CSV formula escaping


def test_formula_like_notes_are_escaped_and_round_trip_unchanged(conn):
    _trade(conn, "buy", "2026-01-02", 1000, note="=1+1")
    exported = csv_trades.export_trades_csv(conn)
    assert ",'=1+1," in exported
    plan = csv_trades.plan_import(conn, exported)
    assert plan.summary == {"created": 0, "updated": 0, "unchanged": 1, "duplicates": 0}


# 8: inconsistent history on read paths


def _break_history(conn):
    _trade(conn, "buy", "2026-01-02", 1000)
    _trade(conn, "sell", "2026-03-02", 800)
    # Bypasses validation to reproduce legacy data, like the homepage test.
    conn.execute(
        "INSERT INTO corporate_actions(id,security_id,action_type,ratio_numerator,"
        "ratio_denominator,ex_date) VALUES('ca','sec','split',1,4,'2026-02-01')"
    )


def test_inconsistent_pair_is_skipped_and_named_on_every_page(conn, tmp_path):
    _break_history(conn)
    # A manual-review action on the same pair must not blank the homepage.
    corporate_actions.create_corporate_action(
        conn,
        {
            "security_id": "sec",
            "action_type": "cash_capital_reduction",
            "ex_date": "2026-04-01",
        },
    )
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('ok','TWSE','2330','台積電','stock','active')"
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": "acc",
            "security_id": "ok",
            "event_type": "buy",
            "trade_date": "2026-01-02",
            "quantity": 1000,
            "unit_price": "500",
        },
    )
    positions = trades.list_position_overview(conn)
    assert [p["security_id"] for p in positions] == ["ok"]
    assert corporate_actions.list_unhandled_actions_for_held_securities(conn) == []
    with TestClient(create_app(tmp_path)) as client:
        for url in ("/", "/income-calendar", "/securities/sec"):
            response = client.get(url)
            assert response.status_code == 200, url
            assert "交易歷史不一致" in response.text, url


# 9: one transaction helper


def test_sync_job_writes_nest_inside_an_outer_transaction(conn):
    with write_transaction(conn):
        sync_jobs.recover_restored(conn)
        assert conn.in_transaction
    assert not conn.in_transaction


# 10: voiding trades


def test_void_excludes_a_trade_everywhere_and_unvoid_restores_it(conn):
    _trade(conn, "buy", "2026-01-02", 1000)
    extra = _trade(conn, "buy", "2026-01-03", 500)
    trades.void_trade(conn, extra)
    assert trades.compute_position(conn, "acc", "sec").remaining_quantity == 1000
    assert extra not in csv_trades.export_trades_csv(conn)
    trades.unvoid_trade(conn, extra)
    assert trades.compute_position(conn, "acc", "sec").remaining_quantity == 1500


def test_voiding_a_buy_that_a_sell_needs_is_rejected(conn):
    buy = _trade(conn, "buy", "2026-01-02", 1000)
    _trade(conn, "sell", "2026-02-02", 600)
    with pytest.raises(replay.OversellError):
        trades.void_trade(conn, buy)
    assert (
        conn.execute("SELECT voided_at FROM trades WHERE id=?", (buy,)).fetchone()[0]
        is None
    )


def test_unvoiding_a_sell_that_would_oversell_is_rejected(conn):
    _trade(conn, "buy", "2026-01-02", 1000)
    sell = _trade(conn, "sell", "2026-02-02", 600)
    trades.void_trade(conn, sell)
    _trade(conn, "sell", "2026-03-02", 1000)
    with pytest.raises(replay.OversellError):
        trades.unvoid_trade(conn, sell)


def test_voided_trades_cannot_be_edited_or_updated_by_csv(conn):
    trade_id = _trade(conn, "buy", "2026-01-02", 1000)
    exported = csv_trades.export_trades_csv(conn)
    trades.void_trade(conn, trade_id)
    with pytest.raises(trades.TradeValidationError, match="已作廢"):
        trades.update_trade(conn, trade_id, {"note": "x"})
    with pytest.raises(csv_trades.ImportError_, match="已作廢"):
        csv_trades.plan_import(conn, exported)


def test_void_api_and_forms(conn, tmp_path):
    buy = _trade(conn, "buy", "2026-01-02", 1000)
    _trade(conn, "sell", "2026-02-02", 600)
    extra = _trade(conn, "buy", "2026-01-03", 100)
    with TestClient(create_app(tmp_path)) as client:
        assert client.post(f"/api/v1/trades/{buy}/void").status_code == 409
        assert client.post("/api/v1/trades/missing/void").status_code == 404
        voided = client.post(f"/api/v1/trades/{extra}/void")
        assert voided.status_code == 200 and voided.json()["voided_at"]
        assert extra not in client.get("/transactions").text
        assert "已作廢" in client.get("/transactions?include_voided=1").text
        page = client.post(
            f"/transactions/{extra}/unvoid",
            data={"return_to": "/transactions"},
            follow_redirects=False,
        )
        assert page.status_code == 303
    assert (
        conn.execute("SELECT voided_at FROM trades WHERE id=?", (extra,)).fetchone()[0]
        is None
    )


def test_upgrade_keeps_every_existing_trade_live(tmp_path):
    with closing(get_connection(tmp_path)) as connection:
        migrations = migrate.discover_migrations()
        original = migrate.discover_migrations
        migrate.discover_migrations = lambda: [m for m in migrations if m.version < 19]
        try:
            migrate.apply_pending(connection)
        finally:
            migrate.discover_migrations = original
        connection.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','b','active')"
        )
        connection.execute(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES('s','TWSE','1','x','etf','active')"
        )
        connection.execute(
            "INSERT INTO trades(id,broker_account_id,security_id,event_type,"
            "trade_date,quantity,created_at) "
            "VALUES('t','a','s','buy','2026-01-01',10,'2026-01-01T00:00:00.000Z')"
        )
        assert migrate.apply_pending(connection) == [19, 20, 21, 22, 23]
        assert connection.execute("SELECT count(*) FROM live_trades").fetchone()[0] == 1


LATER_ROLLBACKS = (
    "rollback-etf-scale.sql",
    "rollback-etf-fund-identity.sql",
    "rollback-etf-ex-dividend-schedule.sql",
    "rollback-investment-consistency.sql",
)


def test_0019_rollback_then_upgrade_keeps_trades_and_actions(conn, rollback_sql):
    _trade(conn, "buy", "2026-01-02", 1000)
    corporate_actions.create_corporate_action(
        conn, {"security_id": "sec", **_official(1100)}
    )
    before = trades.compute_position(conn, "acc", "sec")
    for name in LATER_ROLLBACKS:
        conn.executescript(rollback_sql(name))
    conn.executescript(rollback_sql("rollback-latent-risks.sql"))
    columns = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    assert "voided_at" not in columns
    assert migrate.apply_pending(conn) == [19, 20, 21, 22, 23]
    after = trades.compute_position(conn, "acc", "sec")
    assert (after.remaining_quantity, after.remaining_cost) == (
        before.remaining_quantity,
        before.remaining_cost,
    )
    assert conn.execute("SELECT count(*) FROM corporate_actions").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0019_rollback_refuses_while_a_trade_is_voided(conn, rollback_sql):
    trades.void_trade(conn, _trade(conn, "buy", "2026-01-02", 1000))
    for name in LATER_ROLLBACKS:
        conn.executescript(rollback_sql(name))
    with pytest.raises(sqlite3.IntegrityError):
        conn.executescript(rollback_sql("rollback-latent-risks.sql"))
    conn.rollback()
    assert "voided_at" in {r[1] for r in conn.execute("PRAGMA table_info(trades)")}


# 12: Sec-Fetch-Site


def test_cross_site_writes_are_rejected_and_same_origin_allowed(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        body = {"name": "新帳戶"}
        rejected = client.post(
            "/api/v1/broker-accounts",
            json=body,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert rejected.status_code == 403
        allowed = client.post(
            "/api/v1/broker-accounts",
            json=body,
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert allowed.status_code == 201
        assert json.loads(allowed.text)["id"]
