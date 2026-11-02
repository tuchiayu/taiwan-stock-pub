"""Integration tests for the schema migration runner.

Covers DEVELOPMENT.md §2.2.4 (migrations apply atomically, are
idempotent, and a changed checksum on an already-applied migration is
rejected) and design 002 §4.6 (the 11 core tables, their constraints,
and the trades.created_at / updated_at trigger behaviour).
"""

import sqlite3

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.shared.ids import new_id


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    yield connection
    connection.close()


EXPECTED_TABLES = {
    "market_calendar_events",
    "market_calendar_coverage",
    "securities",
    "etf_profiles",
    "daily_prices",
    "distributions",
    "corporate_actions",
    "corporate_action_reviews",
    "corporate_action_revisions",
    "broker_accounts",
    "trades",
    "dividend_receipts",
    "watchlist",
    "app_settings",
    "schema_migrations",
    "sync_jobs",
    "sync_job_resources",
    "sync_job_steps",
    "security_delistings",
    "dividend_bank_preferences",
}


def test_apply_pending_creates_all_core_tables(conn):
    applied = migrate.apply_pending(conn)

    assert applied == list(range(1, 24))
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert EXPECTED_TABLES <= tables


def test_apply_pending_is_idempotent(conn):
    first = migrate.apply_pending(conn)
    second = migrate.apply_pending(conn)

    assert first == list(range(1, 24))
    assert second == []
    version_count = conn.execute(
        "SELECT COUNT(*) AS n FROM schema_migrations"
    ).fetchone()
    assert version_count["n"] == 23


def test_ex_dividend_schedule_migration_can_revert_and_reapply(conn, rollback_sql):
    migrate.apply_pending(conn)
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('etf','TWSE','00927','群益半導體收益','etf','active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles(security_id,distribution_month_pattern,"
        "distribution_ex_dividend_schedule) VALUES('etf','[3,6,9,12]',"
        '\'{"frequency":"quarterly","months":[1,4,7,10]}\')'
    )
    conn.commit()
    for name in (
        "rollback-etf-scale.sql",
        "rollback-etf-fund-identity.sql",
        "rollback-etf-ex-dividend-schedule.sql",
    ):
        conn.executescript(rollback_sql(name))
    assert (
        conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 20
    )
    assert (
        conn.execute("SELECT distribution_month_pattern FROM etf_profiles").fetchone()[
            0
        ]
        == "[3,6,9,12]"
    )
    assert migrate.apply_pending(conn) == [21, 22, 23]
    assert (
        conn.execute(
            "SELECT distribution_ex_dividend_schedule FROM etf_profiles"
        ).fetchone()[0]
        is None
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reapplying_with_changed_checksum_raises(conn, monkeypatch):
    migrate.apply_pending(conn)

    # Simulate the migration file changing on disk after it was applied.
    monkeypatch.setattr(
        migrate.Migration, "checksum", property(lambda self: "deadbeef")
    )

    with pytest.raises(RuntimeError, match="must not be edited"):
        migrate.apply_pending(conn)


def test_upgrading_a_pre_004_database_preserves_data_and_relations(conn, monkeypatch):
    """Design 004 §4.5: simulates a real upgrade — a database that only
    ever saw migration 1 (i.e. predates the custodian-bank-raw and
    distribution-source-tracking migrations), carrying the exact kind
    of data those migrations exist to fix (a `custodian_bank` value
    misused from the raw API field, per §2.1/§4.3.1) plus references
    from other tables that must survive untouched (`watchlist`,
    `dividend_receipts.distribution_id`).
    """
    all_migrations = migrate.discover_migrations()
    monkeypatch.setattr(migrate, "discover_migrations", lambda: [all_migrations[0]])
    assert migrate.apply_pending(conn) == [1]

    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '00878', '國泰永續高股息', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id, custodian_bank) "
        "VALUES ('e1', '臺灣集中保管結算所股份有限公司')"
    )
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES ('d1', 'e1', '2026-08-18', '1.01', 'final')"
    )
    conn.execute("INSERT INTO watchlist (security_id) VALUES ('e1')")
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acc-1', '測試證券', 'active')"
    )
    conn.execute(
        "INSERT INTO dividend_receipts "
        "(id, broker_account_id, security_id, distribution_id, received_date, "
        " net_amount) "
        "VALUES ('r1', 'acc-1', 'e1', 'd1', '2026-09-11', '1000')"
    )
    conn.commit()

    monkeypatch.undo()  # restore the real discover_migrations
    assert migrate.apply_pending(conn) == [
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
    ]

    profile = conn.execute(
        "SELECT custodian_bank, custodian_institution_raw FROM etf_profiles "
        "WHERE security_id = 'e1'"
    ).fetchone()
    # Migration 0002: the old misused value moves to the raw column and
    # is no longer treated as a confirmed custodian bank.
    assert profile["custodian_bank"] is None
    assert profile["custodian_institution_raw"] == "臺灣集中保管結算所股份有限公司"

    distribution = conn.execute(
        "SELECT ex_dividend_date, amount_per_share, record_date, source, "
        "fetched_at FROM distributions WHERE id = 'd1'"
    ).fetchone()
    assert distribution["ex_dividend_date"] == "2026-08-18"
    assert distribution["amount_per_share"] == "1.01"
    # Migration 0003's new columns: NULL for pre-existing rows means
    # "not confirmed by either source", per design 004 §4.5 — not
    # backfilled with a guessed value.
    assert distribution["record_date"] is None
    assert distribution["source"] is None
    assert distribution["fetched_at"] is None

    # Relations into other tables survive untouched — the security_id
    # and distribution_id foreign keys were never rewritten.
    assert (
        conn.execute(
            "SELECT security_id FROM watchlist WHERE security_id = 'e1'"
        ).fetchone()
        is not None
    )
    receipt = conn.execute(
        "SELECT net_amount FROM dividend_receipts WHERE id = 'r1'"
    ).fetchone()
    assert receipt["net_amount"] == "1000"


def test_foreign_keys_are_enforced(conn):
    migrate.apply_pending(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO trades "
            "(id, broker_account_id, security_id, event_type, trade_date, quantity) "
            "VALUES (?, ?, ?, 'buy', '2026-01-01', 100)",
            (new_id(), "missing-account", "missing-security"),
        )


def test_trades_quantity_must_be_positive(conn):
    migrate.apply_pending(conn)
    security_id, account_id = _seed_security_and_account(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO trades "
            "(id, broker_account_id, security_id, event_type, trade_date, quantity) "
            "VALUES (?, ?, ?, 'sell', '2026-01-01', 0)",
            (new_id(), account_id, security_id),
        )


def test_distributions_unique_per_security_and_ex_date(conn):
    migrate.apply_pending(conn)
    security_id, _ = _seed_security_and_account(conn)

    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES (?, ?, '2026-06-01', '1.5', 'final')",
        (new_id(), security_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
            "VALUES (?, ?, '2026-06-01', '1.5', 'final')",
            (new_id(), security_id),
        )


def test_trades_created_at_is_immutable_but_updated_at_changes(conn):
    migrate.apply_pending(conn)
    security_id, account_id = _seed_security_and_account(conn)
    trade_id = new_id()

    conn.execute(
        "INSERT INTO trades "
        "(id, broker_account_id, security_id, event_type, trade_date, quantity) "
        "VALUES (?, ?, ?, 'buy', '2026-01-01', 1000)",
        (trade_id, account_id, security_id),
    )
    before = conn.execute(
        "SELECT created_at, updated_at FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()

    conn.execute("UPDATE trades SET quantity = 500 WHERE id = ?", (trade_id,))
    after = conn.execute(
        "SELECT created_at, updated_at FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()

    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] >= before["updated_at"]


def _seed_security_and_account(conn: sqlite3.Connection) -> tuple[str, str]:
    security_id = new_id()
    account_id = new_id()
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', '2330', 'TSMC', 'stock', 'active')",
        (security_id,),
    )
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES (?, 'Test Broker', 'active')",
        (account_id,),
    )
    return security_id, account_id


def test_bimonthly_upgrade_preserves_every_profile_field(conn, monkeypatch):
    all_migrations = migrate.discover_migrations()
    with monkeypatch.context() as patch:
        patch.setattr(
            migrate,
            "discover_migrations",
            lambda: [m for m in all_migrations if m.version <= 13],
        )
        migrate.apply_pending(conn)
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('etf','TWSE','00907','永豐優息存股','etf','active')"
    )
    conn.execute("""
        INSERT INTO etf_profiles(
        security_id,custodian_bank,custodian_bank_user_override,
        distribution_frequency,distribution_frequency_user_override,distribution_month_pattern,
        distribution_policy,distribution_policy_source,issuer_name,basic_info_source,updated_at)
        VALUES('etf','華南商業銀行','彰化商業銀行','annual','quarterly','[12]',
        'distributing','{"url":"https://example.org/policy"}','永豐投信',
        '{"benchmark":{"data_period":"202610"}}','2026-01-01T00:00:00.000Z')""")
    before = dict(conn.execute("SELECT * FROM etf_profiles").fetchone())
    assert migrate.apply_pending(conn) == [14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    after = dict(conn.execute("SELECT * FROM etf_profiles").fetchone())
    assert after.pop("distribution_ex_dividend_schedule") is None
    assert after.pop("fund_tax_id") is None
    assert after.pop("fund_type") is None
    assert after == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute(
        "UPDATE etf_profiles SET distribution_frequency='bimonthly' "
        "WHERE security_id='etf'"
    )
    row = conn.execute("SELECT * FROM etf_profiles").fetchone()
    assert row["distribution_frequency"] == "bimonthly"
    assert row["distribution_frequency_user_override"] == "quarterly"
    assert row["updated_at"] != before["updated_at"]
