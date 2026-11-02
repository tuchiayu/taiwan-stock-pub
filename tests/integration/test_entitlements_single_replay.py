"""Design 069: entitlements replay each holding once, with unchanged output."""

from contextlib import closing
from datetime import date, timedelta

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import distribution_entitlement, replay, trades


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(distribution_entitlement, "today_taipei", lambda: "2026-06-15")
    with closing(get_connection(tmp_path)) as connection:
        migrate.apply_pending(connection)
        for account in ("a", "b"):
            connection.execute(
                "INSERT INTO broker_accounts(id,name,status) VALUES(?,?,'active')",
                (account, account.upper()),
            )
        for code in ("0056", "00878"):
            connection.execute(
                "INSERT INTO securities(id,code,name,market,security_type,status) "
                "VALUES(?,?,?,'TWSE','etf','active')",
                (code, code, code),
            )
            for month in range(1, 13):
                ex = date(2026, month, 10)
                connection.execute(
                    "INSERT INTO distributions(id,security_id,ex_dividend_date,"
                    "pay_date,amount_per_share,amount_status) "
                    "VALUES(?,?,?,?,'0.75','final')",
                    (
                        f"{code}-{month}",
                        code,
                        ex.isoformat(),
                        (ex + timedelta(days=20)).isoformat(),
                    ),
                )
        connection.execute(
            "INSERT INTO corporate_actions(id,security_id,action_type,ex_date,"
            "ratio_numerator,ratio_denominator,source) "
            "VALUES('split','0056','split','2026-04-01',2,1,'manual')"
        )
        for account, code, kind, when, quantity in (
            ("a", "0056", "buy", "2026-01-05", 1000),
            ("a", "0056", "buy", "2026-03-09", 500),
            ("a", "0056", "sell", "2026-05-10", 700),
            ("b", "0056", "buy", "2026-02-10", 300),
            ("a", "00878", "buy", "2026-01-01", 2000),
            ("a", "00878", "sell", "2026-03-09", 2000),
        ):
            trades.create_trade(
                connection,
                {
                    "broker_account_id": account,
                    "security_id": code,
                    "event_type": kind,
                    "trade_date": when,
                    "quantity": quantity,
                    "unit_price": "30",
                },
            )
        yield connection


def per_distribution(conn):
    """The previous computation: one full replay per distribution and holder."""
    history = distribution_entitlement.HistoryBook(conn)
    result = []
    for dist in conn.execute("SELECT * FROM distributions"):
        as_of = (
            date.fromisoformat(dist["ex_dividend_date"]) - timedelta(days=1)
        ).isoformat()
        if dist["ex_dividend_date"] > "2026-06-15":
            as_of = "2026-06-15"
        for (account, security), rows in history.trades.items():
            if security != dist["security_id"]:
                continue
            quantity = replay.replay_trades(
                rows, history.actions.get(security, []), as_of_date=as_of
            ).remaining_quantity
            if quantity > 0:
                result.append((dist["id"], account, quantity))
    return sorted(result)


def test_output_matches_a_replay_per_distribution(conn, monkeypatch):
    calls = []
    original = replay.quantities_at
    monkeypatch.setattr(
        replay,
        "quantities_at",
        lambda *args: calls.append(args) or original(*args),
    )
    entries = distribution_entitlement.list_entitlements(conn)
    assert sorted(
        (e.distribution_id, e.broker_account_id, e.entitled_quantity) for e in entries
    ) == per_distribution(conn)
    # Three holdings, one replay each, however many distributions.
    assert len(calls) == 3
    # The split doubles 0056 from April; the sell leaves 00878 empty after it.
    by_id = {(e.distribution_id, e.broker_account_id): e for e in entries}
    assert by_id["0056-3", "a"].entitled_quantity == 1500
    assert by_id["0056-5", "a"].entitled_quantity == 3000
    assert by_id["0056-6", "a"].entitled_quantity == 2300
    assert by_id["00878-2", "a"].entitled_quantity == 2000
    assert ("00878-3", "a") not in by_id
