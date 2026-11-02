"""Integration tests for the shared entitlement module (design 009 §4.6)."""

from decimal import Decimal

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import distribution_entitlement, trades
from taiwan_stock.shared.ids import new_id


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


@pytest.fixture
def seeded(conn, monkeypatch):
    monkeypatch.setattr(
        "taiwan_stock.portfolio.distribution_entitlement.today_taipei",
        lambda: "2026-09-17",
    )
    security_id = new_id()
    account_id = new_id()
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', '2330', 'TSMC', 'stock', 'active')",
        (security_id,),
    )
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) VALUES (?, 'Test', 'active')",
        (account_id,),
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": account_id,
            "security_id": security_id,
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    return {"security_id": security_id, "account_id": account_id}


def _distribution(conn, security_id, **overrides):
    fields = {
        "id": new_id(),
        "security_id": security_id,
        "ex_dividend_date": "2026-08-01",
        "pay_date": "2026-08-20",
        "amount_per_share": "2",
        "amount_status": "final",
        "reconciliation_status": None,
    }
    fields.update(overrides)
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_per_share, "
        " amount_status, reconciliation_status) "
        "VALUES (:id, :security_id, :ex_dividend_date, :pay_date, "
        " :amount_per_share, :amount_status, :reconciliation_status)",
        fields,
    )
    return fields["id"]


def test_confirmed_income_counts_final_paid_undisputed_distributions(conn, seeded):
    _distribution(conn, seeded["security_id"])

    totals = distribution_entitlement.confirmed_income_by_pair(conn)

    key = (seeded["account_id"], seeded["security_id"])
    assert totals[key] == Decimal(2000)


def test_expected_gross_drops_cents_per_entry(conn, seeded):
    # 1000 × 1.2345 = 1234.5, paid as 1234 (issue #75).
    _distribution(conn, seeded["security_id"], amount_per_share="1.2345")

    [entry] = distribution_entitlement.list_entitlements(conn)
    totals = distribution_entitlement.confirmed_income_by_pair(conn)

    assert entry.expected_gross == Decimal(1234)
    assert totals[(seeded["account_id"], seeded["security_id"])] == Decimal(1234)


def test_confirmed_income_excludes_future_ex_dividend_date(conn, seeded):
    _distribution(conn, seeded["security_id"], ex_dividend_date="2099-01-01")

    totals = distribution_entitlement.confirmed_income_by_pair(conn)

    assert totals == {}


def test_confirmed_income_excludes_unknown_amount(conn, seeded):
    _distribution(
        conn,
        seeded["security_id"],
        amount_per_share=None,
        amount_status="unknown",
    )

    totals = distribution_entitlement.confirmed_income_by_pair(conn)

    assert totals == {}


def test_confirmed_income_excludes_estimated_amount(conn, seeded):
    _distribution(conn, seeded["security_id"], amount_status="estimated")

    totals = distribution_entitlement.confirmed_income_by_pair(conn)

    assert totals == {}


def test_confirmed_income_excludes_pay_date_not_yet_arrived(conn, seeded):
    _distribution(conn, seeded["security_id"], pay_date="2026-12-31")

    totals = distribution_entitlement.confirmed_income_by_pair(conn)

    assert totals == {}


def test_confirmed_income_excludes_missing_pay_date(conn, seeded):
    _distribution(conn, seeded["security_id"], pay_date=None)

    totals = distribution_entitlement.confirmed_income_by_pair(conn)

    assert totals == {}


def test_confirmed_income_excludes_distributions_flagged_needs_review(conn, seeded):
    # Design 009 §4.4: a source row awaiting reconciliation must not be
    # counted as settled income.
    _distribution(conn, seeded["security_id"], reconciliation_status="needs_review")

    totals = distribution_entitlement.confirmed_income_by_pair(conn)

    assert totals == {}


def test_entitlement_history_reads_do_not_grow_with_distribution_count(conn, seeded):
    """Many pay events for one holding should reuse the same history reads."""
    for month in range(1, 13):
        _distribution(
            conn,
            seeded["security_id"],
            ex_dividend_date=f"2025-{month:02d}-01",
            pay_date=f"2025-{month:02d}-20",
        )

    statements = []
    conn.set_trace_callback(statements.append)
    try:
        entries = distribution_entitlement.list_entitlements(conn)
    finally:
        conn.set_trace_callback(None)

    assert len(entries) == 12
    assert all(entry.expected_gross == Decimal(2000) for entry in entries)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 4
