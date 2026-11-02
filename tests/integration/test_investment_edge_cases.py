"""Design 032: regression tests against disposable databases and HTTP routes."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import (
    price_storage,
    sync_daily_prices,
    tpex_adapter,
    twse_adapter,
)
from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import csv_trades, pricing, simulate, trades


@pytest.fixture
def conn(tmp_path):
    c = get_connection(tmp_path)
    migrate.apply_pending(c)
    c.execute(
        "INSERT INTO broker_accounts(id,name,status) VALUES('a','Broker','active')"
    )
    c.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('s','TWSE','2330','TSMC','stock','active')"
    )
    yield c
    c.close()


def buy(conn, **overrides):
    return trades.create_trade(
        conn,
        {
            "broker_account_id": "a",
            "security_id": "s",
            "event_type": "buy",
            "trade_date": "2026-01-01",
            "quantity": 1000,
            "unit_price": "10",
            **overrides,
        },
    )


def csv_row(**changes):
    return {
        "broker_account_name": "Broker",
        "security_code": "2330",
        "market": "TWSE",
        "event_type": "buy",
        "trade_date": "2026-01-01",
        "quantity": "100",
        "unit_price": "10",
        **changes,
    }


def test_export_ids_distinguish_same_names_and_survive_rename(conn):
    conn.execute(
        "INSERT INTO broker_accounts(id,name,status) VALUES('b','Broker','disabled')"
    )
    buy(conn)
    buy(conn, broker_account_id="b")
    rows = csv_trades.parse_csv(csv_trades.export_trades_csv(conn))
    assert {r["broker_account_id"] for r in rows} == {"a", "b"}
    conn.execute("UPDATE broker_accounts SET name='Renamed' WHERE id='a'")
    assert csv_trades._plan(conn, rows).summary["unchanged"] == 2
    conn.execute("DELETE FROM trades")
    assert csv_trades.import_trades(conn, rows) == 2
    assert {r[0] for r in conn.execute("SELECT broker_account_id FROM trades")} == {
        "a",
        "b",
    }


def test_ambiguous_old_csv_and_unknown_id_roll_back_whole_batch(conn):
    conn.execute(
        "INSERT INTO broker_accounts(id,name,status) VALUES('b','Broker','disabled')"
    )
    for bad in [csv_row(), csv_row(broker_account_id="foreign-id")]:
        with pytest.raises(csv_trades.ImportError_) as error:
            csv_trades.import_trades(conn, [csv_row(broker_account_id="a"), bad])
        assert error.value.row_number == 3
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0
    conn.execute("UPDATE broker_accounts SET name='Other' WHERE id='b'")
    assert csv_trades.import_trades(conn, [csv_row(broker_account_id="")]) == 1


@pytest.mark.parametrize("value", ["abc", "NaN", "sNaN", "Infinity", "-Infinity", "-1"])
def test_simulation_rejects_bad_prices_in_domain_api_and_html(conn, tmp_path, value):
    buy(conn)
    before = [tuple(r) for r in conn.execute("SELECT * FROM trades")]
    with pytest.raises(simulate.SimulationError, match="價格"):
        simulate.simulate_partial_sale(
            trades.fetch_trades(conn, "a", "s"),
            [],
            1,
            pricing.default_fee_schedule("stock"),
            "2026-10-22",
            value,
        )
    with TestClient(create_app(data_dir=tmp_path)) as client:
        payload = {
            "broker_account_id": "a",
            "security_id": "s",
            "quantity": 1,
            "price": value,
        }
        response = client.post("/api/v1/sale-simulations", json=payload)
        assert response.status_code == 422
        page = client.get("/sale-simulation", params=payload)
        assert page.status_code == 200
        assert "試算價格必須" in page.text
        assert f'value="{value}"' in page.text
    assert [tuple(r) for r in conn.execute("SELECT * FROM trades")] == before


@pytest.mark.parametrize("value", [None, "", "  ", "0", "10.5"])
def test_simulation_accepts_optional_or_valid_price(conn, tmp_path, value):
    buy(conn)
    with TestClient(create_app(data_dir=tmp_path)) as client:
        response = client.post(
            "/api/v1/sale-simulations",
            json={
                "broker_account_id": "a",
                "security_id": "s",
                "quantity": 1,
                "price": value,
            },
        )
    assert response.status_code == 200
    assert response.json()["price"] == (value if value and value.strip() else None)


def test_patch_recalculation_is_explicit_and_manual_values_in_request_win(
    conn, tmp_path
):
    tid = buy(conn, commission="99", other_fees="8")
    with TestClient(create_app(data_dir=tmp_path)) as client:
        url = f"/api/v1/trades/{tid}"
        assert (
            client.patch(url, json={"quantity": 2000, "unit_price": "20"}).status_code
            == 200
        )
        row = dict(conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone())
        assert (row["executed_amount"], row["commission"]) == ("10000", "99")
        assert client.patch(url, json={"recalculate": True}).status_code == 200
        row = dict(conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone())
        assert (row["executed_amount"], row["commission"], row["other_fees"]) == (
            "40000",
            "57",
            "8",
        )
        assert (
            client.patch(
                url,
                json={
                    "recalculate": True,
                    "commission": "3",
                    "executed_amount": "39990",
                    "other_fees": None,
                },
            ).status_code
            == 200
        )
        row = dict(conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone())
        assert (row["executed_amount"], row["commission"], row["other_fees"]) == (
            "39990",
            "3",
            None,
        )
        assert (
            client.patch(
                url,
                json={"recalculate": True, "commission": None, "executed_amount": None},
            ).status_code
            == 200
        )
        assert (
            conn.execute("SELECT commission FROM trades WHERE id=?", (tid,)).fetchone()[
                0
            ]
            == "57"
        )


def test_recalculation_rejects_opening_or_missing_price_atomically(conn, tmp_path):
    ids = [buy(conn, event_type="opening"), buy(conn, unit_price=None)]
    before = [tuple(r) for r in conn.execute("SELECT * FROM trades")]
    with TestClient(create_app(data_dir=tmp_path)) as client:
        for tid in ids:
            assert (
                client.patch(
                    f"/api/v1/trades/{tid}", json={"recalculate": True, "quantity": 9}
                ).status_code
                == 422
            )
    assert [tuple(r) for r in conn.execute("SELECT * FROM trades")] == before


def test_sell_recalculation_uses_current_day_trade_tax_and_explicit_overrides(conn):
    buy(conn, quantity=5000)
    tid = buy(
        conn,
        event_type="sell",
        trade_date="2026-01-02",
        quantity=1000,
        unit_price="20",
        transaction_tax="99",
    )
    trades.update_trade(
        conn, tid, {"quantity": 2000, "is_day_trade": True}, recalculate=True
    )
    row = conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    assert (row["executed_amount"], row["commission"], row["transaction_tax"]) == (
        "40000",
        "57",
        "60",
    )
    trades.update_trade(conn, tid, {"transaction_tax": "7"}, recalculate=True)
    assert (
        conn.execute(
            "SELECT transaction_tax FROM trades WHERE id=?", (tid,)
        ).fetchone()[0]
        == "7"
    )


def store(conn, rows):
    return price_storage._store_daily_prices(
        conn,
        "TWSE",
        rows,
        coverage_date="2026-10-21",
        coverage_scope={"2330": "s"},
        source="official/date-report",
    )


def quote(close="10"):
    return {"code": "2330", "trade_date": "2026-10-21", "close": close, "volume": 100}


@pytest.mark.parametrize("adapter", [twse_adapter, tpex_adapter])
def test_explicit_withdrawal_retains_audit_and_invalidates_only_same_day(conn, adapter):
    store(conn, [quote()])
    conn.execute("INSERT INTO daily_prices VALUES('s','2026-10-20','9',50)")
    row = adapter._normalize_quote_row(
        {"code": "2330", "close": "--", "volume": "0"}, "2026-10-21"
    )
    assert row["status"] == "no_quote"
    store(conn, [row])
    assert price_storage._market_has_prices(conn, "TWSE", "2026-10-21")
    assert [r[0] for r in conn.execute("SELECT trade_date FROM daily_prices")] == [
        "2026-10-20"
    ]
    audit = dict(conn.execute("SELECT * FROM daily_price_invalidations").fetchone())
    assert {
        k: audit[k]
        for k in [
            "security_id",
            "market",
            "trade_date",
            "close",
            "volume",
            "source",
            "reason",
        ]
    } == {
        "security_id": "s",
        "market": "TWSE",
        "trade_date": "2026-10-21",
        "close": "10",
        "volume": 100,
        "source": "official/date-report",
        "reason": "explicit_no_quote",
    }
    assert audit["invalidated_at"]
    store(conn, [row])
    assert (
        conn.execute("SELECT count(*) FROM daily_price_invalidations").fetchone()[0]
        == 1
    )
    store(conn, [quote("11")])
    assert price_storage._market_has_prices(conn, "TWSE", "2026-10-21")
    assert (
        conn.execute(
            "SELECT close FROM daily_prices WHERE trade_date='2026-10-21'"
        ).fetchone()[0]
        == "11"
    )


@pytest.mark.parametrize("adapter", [twse_adapter, tpex_adapter])
@pytest.mark.parametrize("raw", ["bad", "", None, "NaN", "Infinity", "-1", "0"])
def test_missing_or_malformed_quote_is_unverified_and_retains_old_price(
    conn, adapter, raw
):
    store(conn, [quote()])
    row = adapter._normalize_quote_row(
        {"code": "2330", "close": raw, "volume": "0"}, "2026-10-21"
    )
    assert row["status"] == "unverified"
    for rows in [[row], [], [None]]:
        store(conn, rows)
        assert not price_storage._market_has_prices(conn, "TWSE", "2026-10-21")
        assert conn.execute("SELECT close FROM daily_prices").fetchone()[0] == "10"
        assert (
            conn.execute("SELECT count(*) FROM daily_price_invalidations").fetchone()[0]
            == 0
        )


def test_withdrawal_audit_price_and_coverage_roll_back_together(conn):
    store(conn, [quote()])
    before = tuple(conn.execute("SELECT * FROM daily_price_coverage").fetchone())
    conn.executescript(
        "CREATE TRIGGER reject_coverage BEFORE UPDATE ON daily_price_coverage "
        "BEGIN SELECT RAISE(ABORT,'reject'); END;"
    )
    row = twse_adapter._normalize_quote_row(
        {"code": "2330", "close": "--", "volume": "0"}, "2026-10-21"
    )
    with pytest.raises(sqlite3.IntegrityError, match="reject"):
        store(conn, [row])
    assert conn.execute("SELECT close FROM daily_prices").fetchone()[0] == "10"
    assert (
        conn.execute("SELECT count(*) FROM daily_price_invalidations").fetchone()[0]
        == 0
    )
    assert (
        tuple(conn.execute("SELECT * FROM daily_price_coverage").fetchone()) == before
    )


def test_openapi_and_inconsistent_duplicate_rows_never_withdraw(conn):
    store(conn, [quote()])
    missing = {**quote(None), "status": "no_quote"}
    price_storage._store_daily_prices(conn, "TWSE", [missing])
    assert conn.execute("SELECT close FROM daily_prices").fetchone()[0] == "10"
    store(conn, [quote(), missing])
    assert not price_storage._market_has_prices(conn, "TWSE", "2026-10-21")
    assert conn.execute("SELECT close FROM daily_prices").fetchone()[0] == "10"


def test_legacy_coverage_upgrade_and_down_preserve_audit(conn, rollback_sql):
    store(conn, [quote()])
    store(conn, [{**quote(None), "status": "no_quote"}])
    audit = [tuple(r) for r in conn.execute("SELECT * FROM daily_price_invalidations")]
    for name in (
        "rollback-etf-scale.sql",
        "rollback-etf-fund-identity.sql",
        "rollback-etf-ex-dividend-schedule.sql",
        "rollback-investment-consistency.sql",
        "rollback-latent-risks.sql",
        "rollback-investment-edge-cases.sql",
    ):
        conn.executescript(rollback_sql(name))
    # The old application can still insert its five-column coverage rows.
    conn.execute("INSERT INTO daily_prices VALUES('s','2026-10-20','9',50)")
    conn.execute("INSERT INTO daily_price_reports VALUES('TWSE','2026-10-20','old')")
    conn.execute(
        "INSERT INTO daily_price_coverage VALUES('TWSE','2026-10-20','s','9','old')"
    )
    conn.execute(
        "INSERT INTO daily_price_coverage VALUES('TWSE','2026-10-21','s',NULL,'old')"
    )
    assert migrate.apply_pending(conn) == [18, 19, 20, 21, 22, 23]
    assert [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT trade_date,status FROM daily_price_coverage ORDER BY trade_date"
        )
    ] == [("2026-10-20", "quoted"), ("2026-10-21", "unverified")]
    assert [
        tuple(r) for r in conn.execute("SELECT * FROM daily_price_invalidations")
    ] == audit
    assert (
        conn.execute(
            "SELECT close FROM daily_prices WHERE trade_date='2026-10-21'"
        ).fetchone()
        is None
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_incomplete_prices_mark_job_partial_and_retry_reruns_the_step(
    conn, tmp_path, monkeypatch
):
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        return {
            "results": {
                "twse": {
                    "synced": 0,
                    "incomplete_dates": ["2026-10-21"] if len(calls) == 1 else [],
                }
            },
            "errors": {},
        }

    monkeypatch.setattr(sync_daily_prices, "sync_daily_prices_both", fetch)

    def launcher(folder, job_id):
        sync_jobs.run_worker(folder, job_id, client=object())

    first = sync_jobs.submit(conn, tmp_path, "prices", launcher=launcher)
    assert first["status"] == "partial"
    assert first["result"]["twse"]["incomplete_dates"] == ["2026-10-21"]
    second = sync_jobs.retry(conn, tmp_path, first["id"], launcher=launcher)
    assert second["status"] == "success"
    assert len(calls) == 2
