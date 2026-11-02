"""Income calendar: synced `distributions` x actual/projected holdings
(design 002 §4.10). Entirely independent of §4.8's cost basis and the
homepage — this only ever answers "which date, how much, how many
shares does/did that apply to", it never adjusts remaining cost or
market value.

The per-distribution entitlement computation itself lives in
`distribution_entitlement` (design 009 §4.6) so the homepage's auto
announced-dividend break-even can share the same result without a
`trades` <-> `income_calendar` import cycle.
"""

import sqlite3
from calendar import Calendar
from dataclasses import asdict
from datetime import date
from decimal import Decimal

from taiwan_stock.catalog.distribution_rows import countable
from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio.distribution_entitlement import (
    EntitlementEntry as IncomeCalendarEntry,  # noqa: F401 (re-exported)
)
from taiwan_stock.portfolio.distribution_entitlement import (
    HistoryBook,
    completeness,
    history_warnings,
    list_entitlements,
)
from taiwan_stock.portfolio.income_scope import ScopeOptions

list_income_calendar = list_entitlements


def payment_summary(entries):
    """Known gross amounts are a subtotal when any payment amount is unknown.

    `future_gross` is the subset still priced off today's holdings
    (ex-dividend date not yet reached, design 009 §4.3 point 2); the
    rest of `gross` is confirmed entitlement. Neither figure claims a
    bank has actually paid out — see `status` on each entry. Entries
    awaiting source review stay listed but are not summed (design 058).
    """
    counted = [e for e in entries if countable(e)]
    known = [e["expected_gross"] for e in counted if e["expected_gross"] is not None]
    return {
        "gross": sum(known, Decimal(0)) if known or not counted else None,
        "unknown_count": sum(e["expected_gross"] is None for e in counted),
        "future_gross": sum(
            (
                e["expected_gross"]
                for e in counted
                if e["is_future"] and e["expected_gross"] is not None
            ),
            Decimal(0),
        ),
        "reconciliation_count": len(entries) - len(counted),
        "count": len(entries),
    }


def monthly_overview(entries, month):
    """Group the entire payment month, including dates after today.

    Unscheduled announcements (missing pay_date) cannot enter this total.
    """
    first = date.fromisoformat(month + "-01")
    selected = [e for e in entries if e["pay_date"] and e["pay_date"].startswith(month)]
    by_date = {}
    for entry in selected:
        by_date.setdefault(entry["pay_date"], []).append(entry)
    days = []
    for week in Calendar(firstweekday=6).monthdayscalendar(first.year, first.month):
        for day in week:
            iso = f"{month}-{day:02d}" if day else None
            payments = by_date.get(iso, [])
            days.append(
                {
                    "day": day,
                    "date": iso,
                    "entries": payments,
                    "codes": sorted({e["security_code"] for e in payments}),
                    **payment_summary(payments),
                }
            )
    return {
        **payment_summary(selected),
        "days": days,
        "payment_days": [d for d in days if d["entries"]],
        "payment_day_count": len(by_date),
    }


def calendar_view(
    conn: sqlite3.Connection,
    *,
    month=None,
    filter_value="all",
    options=None,
    history=None,
):
    """Payment-date view of announced distributions only (design 009 §4.5:
    the historical-frequency 12-month projection has been removed —
    every entry here is backed by a real `distributions` row)."""
    with read_transaction(conn):
        if month:
            try:
                if len(month) != 7:
                    raise ValueError
                date.fromisoformat(month + "-01")
            except ValueError as exc:
                raise ValueError("月份格式必須是 YYYY-MM") from exc
        options = options if options is not None else ScopeOptions(conn)
        selected = options.select(filter_value)
        entries = []
        for item in list_entitlements(conn, history=history):
            # `dividend_bank_preferences` is the investor's own choice of
            # where THIS holding's dividend actually lands (design 010) —
            # unrelated to the ETF's custodian bank (`etf_profiles`, which
            # only says where the fund's money comes FROM). Falls back to
            # the account's default receiving bank when no preference is
            # set for this (account, security) pair.
            bank = options.bank_for(item.broker_account_id, item.security_id)
            if not selected.includes(item.broker_account_id, bank):
                continue
            entry = asdict(item)
            entry.update(
                bank=bank,
                status="依目前持股試算" if item.is_future else "已確定",
            )
            if month and (not item.pay_date or not item.pay_date.startswith(month)):
                continue
            entries.append(entry)
        return sorted(entries, key=lambda e: e["pay_date"] or "9999")


def scoped_warnings(conn, options, filter_value, history=None):
    selected = options.select(filter_value)
    return [
        w
        for w in (history.warnings if history is not None else history_warnings(conn))
        if selected.includes(
            w["broker_account_id"],
            options.bank_for(w["broker_account_id"], w["security_id"]),
        )
    ]


def calendar_result(conn, *, month=None, filter_value="all", options=None):
    with read_transaction(conn):
        options = options if options is not None else ScopeOptions(conn)
        history = HistoryBook(conn)
        items = calendar_view(
            conn,
            month=month,
            filter_value=filter_value,
            options=options,
            history=history,
        )
        return {
            "items": items,
            **completeness(scoped_warnings(conn, options, filter_value, history)),
        }
