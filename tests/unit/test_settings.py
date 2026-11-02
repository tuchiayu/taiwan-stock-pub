"""Unit tests for validated fee/display preferences (portfolio/settings.py)."""

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import settings


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


def test_read_settings_returns_defaults_when_nothing_saved(conn):
    assert settings.read_settings(conn) == settings.DEFAULTS


def test_save_settings_persists_and_read_settings_reflects_it(conn):
    result = settings.save_settings(conn, {"commission_discount": "0.6"})

    assert result["commission_discount"] == "0.6"
    assert settings.read_settings(conn)["commission_discount"] == "0.6"
    # Untouched keys keep their default.
    assert (
        settings.read_settings(conn)["min_commission"]
        == settings.DEFAULTS["min_commission"]
    )


def test_save_settings_rejects_unknown_key(conn):
    with pytest.raises(ValueError, match="未知設定"):
        settings.save_settings(conn, {"sync_enabled": "true"})


def test_save_settings_rejects_negative_or_non_finite_amounts(conn):
    with pytest.raises(ValueError):
        settings.save_settings(conn, {"min_commission": "-1"})
    with pytest.raises(ValueError):
        settings.save_settings(conn, {"min_commission": "NaN"})


def test_save_settings_rejects_a_value_that_is_not_a_number_at_all(conn):
    with pytest.raises(ValueError, match="設定值無效"):
        settings.save_settings(conn, {"min_commission": "abc"})


@pytest.mark.parametrize("key", ["commission_rate", "tax_rate_stock", "tax_rate_etf"])
def test_save_settings_rejects_rates_of_1_or_more(conn, key):
    with pytest.raises(ValueError):
        settings.save_settings(conn, {key: "1"})


def test_save_settings_allows_commission_discount_of_exactly_1(conn):
    result = settings.save_settings(conn, {"commission_discount": "1"})
    assert result["commission_discount"] == "1"


def test_save_settings_rejects_commission_discount_over_1(conn):
    with pytest.raises(ValueError):
        settings.save_settings(conn, {"commission_discount": "1.5"})


def test_save_settings_only_accepts_shares_or_lots_for_quantity_display(conn):
    settings.save_settings(conn, {"quantity_display": "lots"})
    assert settings.read_settings(conn)["quantity_display"] == "lots"

    with pytest.raises(ValueError):
        settings.save_settings(conn, {"quantity_display": "boxes"})


def test_save_settings_upserts_on_repeated_saves(conn):
    settings.save_settings(conn, {"min_commission": "1"})
    settings.save_settings(conn, {"min_commission": "5"})

    assert settings.read_settings(conn)["min_commission"] == "5"
