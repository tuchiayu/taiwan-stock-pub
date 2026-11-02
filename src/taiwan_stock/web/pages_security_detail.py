"""One security's page, its override forms and watchlist forms (design 070)."""

import sqlite3
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from taiwan_stock.catalog import (
    bank_dictionary,
    delistings,
    profile_values,
    sync_history,
    trading_days,
)
from taiwan_stock.catalog import (
    query as catalog_query,
)
from taiwan_stock.infrastructure.db import read_transaction, write_transaction
from taiwan_stock.portfolio import (
    corporate_action_reviews,
    corporate_actions,
    distribution_entitlement,
    watchlist,
)
from taiwan_stock.portfolio import trades as trades_repo
from taiwan_stock.shared.dates import taipei_date, today_taipei
from taiwan_stock.web.deps import get_db
from taiwan_stock.web.presentation import local_return, with_query
from taiwan_stock.web.templates import templates

router = APIRouter()

DISTRIBUTION_FREQUENCY_LABELS = profile_values.FREQUENCY_LABELS
_effective_custodian_bank = profile_values.custodian_bank


def _distribution_updated_label(security_type, yield_info, last_synced_at):
    """Design 050 §4.2.4: stocks share the ETFs' "配息更新至" wording (design
    023) instead of a raw UTC timestamp."""
    if security_type == "etf":
        return yield_info["updated_label"]
    if last_synced_at:
        return f"配息更新至 {taipei_date(last_synced_at).replace('-', '/')}"
    return None


def _distribution_scope_note(security_type, first_acquired, is_tracked):
    """One sentence on which years the history covers (design 050 §4.2.4)."""
    if first_acquired:
        return f"含 {first_acquired.replace('-', '/')} 第一次買進以來的配息"
    if security_type == "etf":
        return "本年及前兩年的配息"
    if is_tracked:
        return "近 2 年的配息"
    return None


@router.get("/securities/{security_id}", response_class=HTMLResponse)
def security_detail_page(
    request: Request,
    security_id: str,
    watchlist_saved: int | None = None,
    watchlist_error: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    with read_transaction(conn):
        listing_details = delistings.details(conn)
        security = conn.execute(
            "SELECT * FROM securities WHERE id = ?", (security_id,)
        ).fetchone()
        if security is None:
            raise HTTPException(404, "找不到商品")

        security = {**dict(security), **listing_details.get(security_id, {})}
        profile = None
        if security["security_type"] == "etf":
            profile = conn.execute(
                "SELECT * FROM etf_profiles WHERE security_id = ?", (security_id,)
            ).fetchone()

        price_rows = conn.execute(
            "SELECT trade_date, close FROM daily_prices WHERE security_id = ? "
            "ORDER BY trade_date DESC LIMIT 2",
            (security_id,),
        ).fetchall()
        latest_close = close_date = previous_close_date = change = change_pct = None
        if price_rows:
            latest_close = price_rows[0]["close"]
            close_date = price_rows[0]["trade_date"]
            if len(price_rows) > 1:
                previous_close_date = price_rows[1]["trade_date"]
            # Issue #89: a gap in stored closes must not read as one day's change.
            if previous_close_date and trading_days.TradingCalendar(conn).adjacent(
                previous_close_date, close_date
            ):
                prev_close = Decimal(price_rows[1]["close"])
                change = Decimal(latest_close) - prev_close
                if prev_close > 0:
                    change_pct = (change / prev_close * 100).quantize(Decimal("0.01"))

        dist_rows = conn.execute(
            "SELECT * FROM distributions WHERE security_id = ? "
            "AND ex_dividend_date IS NOT NULL ORDER BY ex_dividend_date DESC",
            (security_id,),
        ).fetchall()
        custodian_bank_display = {"value": None, "tier": "unknown"}
        if profile:
            custodian_bank_display = _effective_custodian_bank(dict(profile))
        # The catalog list and holdings use this same entry point (design
        # 058 §4.2.2), so the page's yield matches its holdings summary.
        distribution_frequency_display, yield_info = catalog_query.frequency_and_yield(
            profile,
            dist_rows,
            latest_close,
            as_of=today_taipei(),
            security_type=security["security_type"],
            security_id=security_id,
            market=security["market"],
            listed_date=security["listed_date"],
            units=sync_history.latest_units(conn),
            successes=sync_history.latest_successes(conn),
            actions=corporate_actions.list_corporate_actions(conn, security_id),
        )
        # Design 004 §4.6's closing paragraph: "商品詳情須顯示最近成功同步時間"
        # — for stocks still derived from `distributions.fetched_at`; ETFs use
        # the sync history instead (`yield_info["updated_label"]`, design 023
        # §4.4 point 6), so one page never shows two different update dates.
        last_synced_at = max(
            (row["fetched_at"] for row in dist_rows if row["fetched_at"]),
            default=None,
        )
        # Design 050 §4.2.2: the first acquisition in any account, the same
        # boundary the dividend syncs reach back to.
        first_acquired = conn.execute(
            "SELECT MIN(trade_date) FROM live_trades "
            "WHERE security_id=? AND event_type IN ('buy', 'opening')",
            (security_id,),
        ).fetchone()[0]

        holdings = trades_repo.position_overview_result(conn, security_id=security_id)[
            "items"
        ]
        corporate_action_reviews.ReviewBook(conn).annotate(holdings)
        if holdings:
            # Batch both lookups across every holding's broker account instead
            # of one round-trip per row (design 010's `bank_for` does the same).
            account_ids = sorted({h["broker_account_id"] for h in holdings})
            placeholders = ",".join("?" * len(account_ids))
            default_banks = {
                row["id"]: row["dividend_bank_name"]
                for row in conn.execute(
                    f"SELECT id, dividend_bank_name FROM broker_accounts "
                    f"WHERE id IN ({placeholders})",
                    account_ids,
                )
            }
            preferences = {
                row["broker_account_id"]: row["bank_name"]
                for row in conn.execute(
                    "SELECT broker_account_id, bank_name "
                    "FROM dividend_bank_preferences "
                    f"WHERE security_id = ? AND broker_account_id IN ({placeholders})",
                    [security_id, *account_ids],
                )
            }
            for h in holdings:
                h["account_default_bank"] = default_banks.get(h["broker_account_id"])
                h["dividend_bank_preference"] = preferences.get(h["broker_account_id"])

        watch_item = watchlist.get_watchlist_item(conn, security_id)
        response_kwargs = {
            "request": request,
            "name": "security_detail.html",
            "context": {
                "security": dict(security),
                "profile": dict(profile) if profile else None,
                "watch_item": watch_item,
                "is_tracked": watch_item is not None,
                "watchlist_saved": watchlist_saved,
                "watchlist_error": WATCHLIST_DETAIL_ERRORS.get(watchlist_error),
                "latest_close": latest_close,
                "close_date": close_date,
                "previous_close_date": previous_close_date,
                "change": change,
                "change_pct": change_pct,
                "yield_info": yield_info,
                "distribution_updated_label": _distribution_updated_label(
                    security["security_type"], yield_info, last_synced_at
                ),
                "distribution_scope_note": _distribution_scope_note(
                    security["security_type"], first_acquired, watch_item is not None
                ),
                "first_acquired": first_acquired,
                # Design 051 §4.2.1: the exchange's raw custodian is only
                # worth a row when it names a different bank.
                "show_raw_custodian": bool(
                    profile
                    and profile["custodian_institution_raw"]
                    and bank_dictionary.canonical_name(
                        profile["custodian_institution_raw"]
                    )
                    != custodian_bank_display["value"]
                ),
                "distribution_frequency_labels": DISTRIBUTION_FREQUENCY_LABELS,
                "distribution_history": [dict(row) for row in dist_rows],
                "holdings": holdings,
                "history_warnings": [
                    w
                    for w in distribution_entitlement.history_warnings(conn)
                    if w["security_id"] == security_id
                ],
                "custodian_bank_display": custodian_bank_display,
                "distribution_frequency_display": distribution_frequency_display,
                "bank_options": bank_dictionary.display_names(),
                "bank_source": profile_values.json_value(
                    dict(profile).get("custodian_bank_source"), {}
                )
                if profile
                else {},
                "frequency_source": profile_values.json_value(
                    dict(profile).get("distribution_policy_source")
                    if dict(profile).get("distribution_policy") == "non_distributing"
                    else dict(profile).get("distribution_frequency_source")
                    or dict(profile).get("distribution_policy_source"),
                    {},
                )
                if profile
                else {},
                "basic_sources": profile_values.json_value(
                    dict(profile).get("basic_info_source"), {}
                )
                if profile
                else {},
                "tier_labels": profile_values.TIER_LABELS,
                "sync_units": [
                    u
                    for (sid, _, _), u in sync_history.latest_units(conn).items()
                    if sid == security_id
                ],
            },
        }
    return templates.TemplateResponse(**response_kwargs)


@router.post("/securities/{security_id}/custodian-bank-override")
def set_custodian_bank_override_form(
    security_id: str,
    custodian_bank_user_override: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    """Design 004 §4.5.1: `custodian_bank_user_override` is restricted
    to the closed bank dictionary (a dropdown, not free text) so it
    stays comparable with the auto-detected `custodian_bank` rather
    than reintroducing "same bank, many spellings" (§2.1). An empty
    submission clears the override, falling back to the next tier."""
    value = bank_dictionary.canonical_name(custodian_bank_user_override)
    if value is not None and value not in bank_dictionary.display_names():
        raise HTTPException(400, "不是銀行字典中的有效選項")
    with write_transaction(conn):
        if value is None:
            conn.execute(
                "UPDATE etf_profiles SET custodian_bank_user_override = NULL "
                "WHERE security_id = ?",
                (security_id,),
            )
        else:
            row = conn.execute(
                "SELECT 1 FROM securities WHERE id = ? AND security_type = 'etf'",
                (security_id,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT INTO etf_profiles "
                    "(security_id, custodian_bank_user_override) VALUES (?, ?) "
                    "ON CONFLICT (security_id) DO UPDATE SET "
                    "custodian_bank_user_override = "
                    "excluded.custodian_bank_user_override",
                    (security_id, value),
                )
    return RedirectResponse(url=f"/securities/{security_id}", status_code=303)


@router.post(
    "/securities/{security_id}/broker-accounts/{broker_account_id}/dividend-bank"
)
def set_dividend_bank_preference_form(
    security_id: str,
    broker_account_id: str,
    dividend_bank: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    """Design 010: which bank THIS holding's dividend actually lands in —
    unrelated to the ETF's custodian bank (which only says where the
    fund's money comes from). Restricted to the closed bank dictionary
    for the same reason as `custodian_bank_user_override`. An empty
    submission clears the preference, falling back to the broker
    account's default receiving bank."""
    value = bank_dictionary.canonical_name(dividend_bank)
    if value is not None and value not in bank_dictionary.display_names():
        raise HTTPException(400, "不是銀行字典中的有效選項")
    with write_transaction(conn):
        # Design 065: 404, not a foreign-key 500, for an unknown id.
        for table, row_id, missing in (
            ("securities", security_id, "找不到商品"),
            ("broker_accounts", broker_account_id, "找不到券商帳戶"),
        ):
            if not conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)
            ).fetchone():
                raise HTTPException(404, missing)
        if value is None:
            conn.execute(
                "DELETE FROM dividend_bank_preferences "
                "WHERE broker_account_id=? AND security_id=?",
                (broker_account_id, security_id),
            )
        else:
            conn.execute(
                "INSERT INTO dividend_bank_preferences "
                "(broker_account_id, security_id, bank_name) VALUES (?, ?, ?) "
                "ON CONFLICT (broker_account_id, security_id) "
                "DO UPDATE SET bank_name = excluded.bank_name",
                (broker_account_id, security_id, value),
            )
    return RedirectResponse(url=f"/securities/{security_id}", status_code=303)


@router.post("/securities/{security_id}/distribution-frequency-override")
def set_distribution_frequency_override_form(
    security_id: str,
    distribution_frequency_user_override: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    """Design 004 §4.4.1: same override-tier rule as the custodian bank
    above, restricted to the known classification set so the stored
    value stays comparable with the official/inferred tiers."""
    value = distribution_frequency_user_override.strip() or None
    if value is not None and value not in DISTRIBUTION_FREQUENCY_LABELS:
        raise HTTPException(400, "不是有效的配息頻率選項")
    with write_transaction(conn):
        if value is None:
            conn.execute(
                "UPDATE etf_profiles SET distribution_frequency_user_override = NULL "
                "WHERE security_id = ?",
                (security_id,),
            )
        else:
            row = conn.execute(
                "SELECT 1 FROM securities WHERE id = ? AND security_type = 'etf'",
                (security_id,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT INTO etf_profiles "
                    "(security_id, distribution_frequency_user_override) "
                    "VALUES (?, ?) "
                    "ON CONFLICT (security_id) DO UPDATE SET "
                    "distribution_frequency_user_override = "
                    "excluded.distribution_frequency_user_override",
                    (security_id, value),
                )
    return RedirectResponse(url=f"/securities/{security_id}", status_code=303)


@router.post("/watchlist/add")
def add_watchlist_form(
    security_id: str = Form(...),
    return_to: str = Form(default="/securities"),
    conn: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    try:
        watchlist.add_to_watchlist(conn, security_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse(
        with_query(local_return(return_to, "/securities"), watchlist_added="1"),
        status_code=303,
    )


WATCHLIST_NOTE_MAX = 200
# Codes, not free text, travel in the redirect so nothing user-supplied is
# echoed back into the page.
WATCHLIST_DETAIL_ERRORS = {
    "price": watchlist.TARGET_PRICE_ERROR,
    "note": f"備註最多 {WATCHLIST_NOTE_MAX} 字。",
    "untracked": "此商品未在追蹤清單中，無法設定目標價與備註。",
}
_target_price = watchlist.parse_target_price


@router.post("/watchlist/details")
def update_watchlist_details_form(
    security_id: str = Form(...),
    target_price: str = Form(default=""),
    note: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    back = f"/securities/{quote(security_id, safe='')}"
    error = None
    try:
        price = _target_price(target_price)
    except ValueError:
        error = "price"
    text = note.strip() or None
    if error is None and text and len(text) > WATCHLIST_NOTE_MAX:
        error = "note"
    if error is None and not watchlist.update_watchlist_details(
        conn, security_id, note=text, target_price=price
    ):
        error = "untracked"
    if error:
        return RedirectResponse(
            with_query(back, watchlist_error=error), status_code=303
        )
    return RedirectResponse(with_query(back, watchlist_saved="1"), status_code=303)


@router.post("/watchlist/remove")
def remove_watchlist_form(
    security_id: str = Form(...),
    return_to: str = Form(default="/securities"),
    conn: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    watchlist.remove_from_watchlist(conn, security_id)
    return RedirectResponse(
        with_query(local_return(return_to, "/securities"), watchlist_removed="1"),
        status_code=303,
    )
