"""Integration test: load_fee_schedule falls back to statutory
defaults, but a saved app_settings row overrides them."""

from contextlib import closing
from decimal import Decimal

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import pricing


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as connection:
        migrate.apply_pending(connection)
        yield connection


def test_load_fee_schedule_uses_defaults_when_nothing_saved(conn):

    schedule = pricing.load_fee_schedule(conn, "stock")

    assert schedule == pricing.default_fee_schedule("stock")


def test_load_fee_schedule_applies_saved_discount_and_min_commission(conn):
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('commission_discount', '0.6')"
    )
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('min_commission', '1')")

    schedule = pricing.load_fee_schedule(conn, "stock")

    assert schedule.commission_discount == Decimal("0.6")
    assert schedule.min_commission == Decimal("1")
    assert schedule.commission_rate == pricing.STATUTORY_COMMISSION_RATE


def test_load_fee_schedule_uses_etf_specific_tax_rate(conn):

    stock_schedule = pricing.load_fee_schedule(conn, "stock")
    etf_schedule = pricing.load_fee_schedule(conn, "etf")

    assert stock_schedule.tax_rate == pricing.STATUTORY_TAX_RATE_STOCK
    assert etf_schedule.tax_rate == pricing.STATUTORY_TAX_RATE_ETF
