"""Integration tests for the income calendar (design 002 §4.10)."""

from decimal import Decimal

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import income_calendar, trades
from taiwan_stock.shared.ids import new_id


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


@pytest.fixture
def seeded(conn):
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
    return {"security_id": security_id, "account_id": account_id}


def _add_distribution(conn, security_id, ex_date, amount="5", status="final"):
    dist_id = new_id()
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (dist_id, security_id, ex_date, amount, status),
    )
    return dist_id


def test_no_trades_means_empty_calendar(conn):
    assert income_calendar.list_income_calendar(conn) == []


def test_held_before_past_ex_date_is_entitled(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    _add_distribution(conn, seeded["security_id"], "2024-06-01", amount="5")

    entries = income_calendar.list_income_calendar(conn)

    assert len(entries) == 1
    assert entries[0].entitled_quantity == Decimal(1000)
    assert entries[0].expected_gross == Decimal(5000)
    assert entries[0].is_future is False
    assert entries[0].reconciliation_status is None


def test_bought_on_ex_date_is_not_entitled(conn, seeded):
    _add_distribution(conn, seeded["security_id"], "2024-06-01", amount="5")
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-06-01",  # same day as ex-date, too late
            "quantity": 1000,
            "unit_price": "50",
        },
    )

    assert income_calendar.list_income_calendar(conn) == []


def test_sold_after_ex_date_keeps_the_already_earned_entitlement(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    _add_distribution(conn, seeded["security_id"], "2024-06-01", amount="5")
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "sell",
            "trade_date": "2024-07-01",  # sold after the ex-date
            "quantity": 1000,
        },
    )

    entries = income_calendar.list_income_calendar(conn)

    assert len(entries) == 1
    assert entries[0].entitled_quantity == Decimal(1000)


def test_future_ex_date_projects_using_current_holdings(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    _add_distribution(conn, seeded["security_id"], "2099-01-01", amount="5")

    entries = income_calendar.list_income_calendar(conn)

    assert len(entries) == 1
    assert entries[0].is_future is True
    assert entries[0].entitled_quantity == Decimal(1000)


def test_unknown_amount_still_lists_with_no_expected_gross(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES (?, ?, '2024-06-01', NULL, 'unknown')",
        (new_id(), seeded["security_id"]),
    )

    entries = income_calendar.list_income_calendar(conn)

    assert len(entries) == 1
    assert entries[0].expected_gross is None
    assert entries[0].amount_status == "unknown"


def test_entitlement_flags_reconciliation_from_the_distribution_row(conn, seeded):
    # Design 009 §4.4: the ETF reconciliation flag lives on `distributions`
    # and must surface through to calendar entries, not be dropped.
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    dist_id = _add_distribution(conn, seeded["security_id"], "2024-06-01", amount="5")
    conn.execute(
        "UPDATE distributions SET reconciliation_status='needs_review', "
        "reconciliation_note='待覆核' WHERE id=?",
        (dist_id,),
    )

    entries = income_calendar.list_income_calendar(conn)

    assert entries[0].reconciliation_status == "needs_review"
    assert entries[0].reconciliation_note == "待覆核"


def test_calendar_view_rejects_a_malformed_month(conn):
    with pytest.raises(ValueError, match="月份格式"):
        income_calendar.calendar_view(conn, month="2024/06")


def test_calendar_view_month_filter_only_keeps_matching_pay_dates(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    dist_id = new_id()
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_per_share, "
        " amount_status) "
        "VALUES (?, ?, '2024-06-01', '2024-06-20', '5', 'final')",
        (dist_id, seeded["security_id"]),
    )

    matching = income_calendar.calendar_view(conn, month="2024-06")
    other_month = income_calendar.calendar_view(conn, month="2024-07")

    assert len(matching) == 1
    assert matching[0]["pay_date"] == "2024-06-20"
    assert other_month == []


def test_calendar_view_only_returns_entries_backed_by_real_distributions(conn, seeded):
    # Design 009 §4.5: the historical-frequency 12-month projection is
    # gone — every entry must trace back to a real `distributions` row.
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2020-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    dist_id = new_id()
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_per_share, "
        " amount_status, frequency_label) "
        "VALUES (?, ?, '2025-03-01', '2025-03-01', '1', 'final', 'quarterly')",
        (dist_id, seeded["security_id"]),
    )

    entries = income_calendar.calendar_view(conn)

    assert len(entries) == 1
    assert entries[0]["distribution_id"] == dist_id
    assert "estimated" not in entries[0]
    assert "projected_month" not in entries[0]


def test_monthly_overview_includes_full_announced_month_and_separates_unknowns(
    conn, seeded, monkeypatch
):
    monkeypatch.setattr(
        "taiwan_stock.portfolio.distribution_entitlement.today_taipei",
        lambda: "2026-09-17",
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    # Paid earlier this month, payable after today, unknown amount, another
    # month, and an announcement without a payment date.
    ids = []
    for ex_date, pay_date, amount in [
        ("2026-08-20", "2026-09-05", "1.2345"),
        ("2026-09-21", "2026-09-30", "2"),
        ("2026-09-22", "2026-09-30", None),
        ("2026-09-23", "2026-10-05", "9"),
        ("2026-09-24", None, "8"),
    ]:
        dist_id = _add_distribution(
            conn,
            seeded["security_id"],
            ex_date,
            amount=amount,
            status="final" if amount is not None else "unknown",
        )
        conn.execute(
            "UPDATE distributions SET pay_date=? WHERE id=?", (pay_date, dist_id)
        )
        ids.append(dist_id)
    entries = income_calendar.calendar_view(conn)
    overview = income_calendar.monthly_overview(entries, "2026-09")

    # 1000 × 1.2345 = 1234.5 is paid as 1234 (cents dropped per entry).
    assert overview["gross"] == Decimal(3234)
    assert overview["future_gross"] == Decimal(2000)
    assert overview["unknown_count"] == 1
    assert overview["payment_day_count"] == 2
    assert overview["count"] == 3
    last_day = overview["payment_days"][-1]
    assert last_day["date"] == "2026-09-30"
    assert last_day["count"] == 2
    assert last_day["gross"] == Decimal(2000)
    assert last_day["codes"] == ["2330"]
    assert overview["days"][2]["date"] == "2026-09-01"  # Tuesday


def test_announced_payment_without_ex_date_uses_current_holdings(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    dist_id = _add_distribution(conn, seeded["security_id"], None, amount="2")
    conn.execute(
        "UPDATE distributions SET pay_date='2026-09-30' WHERE id=?", (dist_id,)
    )
    entries = income_calendar.calendar_view(conn, month="2026-09")
    assert len(entries) == 1
    assert entries[0]["is_future"] is True
    assert entries[0]["ex_dividend_date"] is None
    assert income_calendar.monthly_overview(entries, "2026-09")["gross"] == 2000


def test_calendar_view_bank_prefers_preference_over_account_default(conn, seeded):
    # design 010: the dividend-receiving bank is the investor's own
    # per-(account, security) choice, unrelated to the ETF's custodian
    # bank — the account's own default only fills in where no such
    # choice was made for this particular holding.
    conn.execute(
        "UPDATE broker_accounts SET dividend_bank_name=? WHERE id=?",
        ("台新國際商業銀行", seeded["account_id"]),
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    _add_distribution(conn, seeded["security_id"], "2024-06-01")

    no_preference = income_calendar.calendar_view(conn)
    assert no_preference[0]["bank"] == "台新國際商業銀行"

    conn.execute(
        "INSERT INTO dividend_bank_preferences "
        "(broker_account_id, security_id, bank_name) VALUES (?, ?, ?)",
        (seeded["account_id"], seeded["security_id"], "玉山銀行"),
    )
    with_preference = income_calendar.calendar_view(conn)
    assert with_preference[0]["bank"] == "玉山銀行"

    conn.execute(
        "DELETE FROM dividend_bank_preferences "
        "WHERE broker_account_id=? AND security_id=?",
        (seeded["account_id"], seeded["security_id"]),
    )
    cleared = income_calendar.calendar_view(conn)
    assert cleared[0]["bank"] == "台新國際商業銀行"


def test_monthly_overview_combines_accounts_and_does_not_turn_unknown_into_zero():
    common = {
        "pay_date": "2024-02-29",
        "is_future": False,
        "reconciliation_status": None,
        "expected_gross": None,
        "security_code": "2330",
    }
    unknown = income_calendar.monthly_overview([common], "2024-02")
    assert unknown["gross"] is None
    assert unknown["payment_days"][0]["gross"] is None
    assert unknown["unknown_count"] == 1
    assert max(day["day"] for day in unknown["days"]) == 29

    entries = [
        {**common, "expected_gross": Decimal("100.25"), "broker_account_id": a}
        for a in ("account-a", "account-b")
    ]
    overview = income_calendar.monthly_overview(entries, "2024-02")
    assert overview["gross"] == Decimal("200.50")
    assert overview["payment_day_count"] == 1
    assert overview["count"] == 2
    assert income_calendar.monthly_overview(entries, "2024-03")["gross"] == 0
