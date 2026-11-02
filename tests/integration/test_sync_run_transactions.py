"""Design 073: a sync run rolls back only the transaction it left open."""

from contextlib import closing

import pytest

from taiwan_stock.catalog import sync_history
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection, write_transaction


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as connection:
        migrate.apply_pending(connection)
        yield connection


def run_status(conn):
    return conn.execute("SELECT status FROM catalog_sync_runs").fetchone()[0]


def test_a_failed_run_keeps_the_callers_transaction(conn):
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        with pytest.raises(ValueError):
            with sync_history.run(conn, "investment_trust"):
                raise ValueError("source changed")
        assert conn.in_transaction
    assert conn.execute("SELECT count(*) FROM broker_accounts").fetchone()[0] == 1
    assert run_status(conn) == "interrupted"


def test_a_transaction_left_open_by_the_block_is_still_rolled_back(conn):
    with pytest.raises(ValueError):
        with sync_history.run(conn, "investment_trust"):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO broker_accounts(id,name,status) "
                "VALUES('a','券商','active')"
            )
            raise ValueError("source changed")
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM broker_accounts").fetchone()[0] == 0
    assert run_status(conn) == "interrupted"
