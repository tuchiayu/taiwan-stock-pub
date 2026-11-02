"""Complete local result-set sorting, before pagination (design 005)."""

import re
import sqlite3
import unicodedata
from collections import Counter
from decimal import Decimal

from taiwan_stock.catalog import (
    delistings,
    etf_scale,
    industries,
    profile_values,
    search,
    sync_history,
    trading_days,
)
from taiwan_stock.catalog.distribution_rows import COUNTABLE_SQL
from taiwan_stock.portfolio import yields
from taiwan_stock.shared.dates import taipei_date, today_taipei

FREQUENCIES = [
    "monthly",
    "bimonthly",
    "quarterly",
    "semiannual",
    "annual",
    "irregular",
    "non_distributing",
]
# Rows with no real bank/frequency value still fall into different, distinctly
# actionable reasons (retry a failed sync vs. wait on a source that doesn't
# support this security vs. nothing to do because it's delisted) — group them
# in this order instead of leaving them in arbitrary code order, which looked
# unsorted to a user who clicked the column header expecting some grouping.
_MISSING_REASON_ORDER = {
    "同步失敗": 1,
    "來源待更新": 2,
    "來源未收錄此商品": 3,
    "來源尚未支援": 4,
    "有配息紀錄，頻率待確認": 5,
    "來源未提供此欄位": 6,
    "紀錄不足，無法推測": 7,
    "尚未同步": 8,
}
# One set for both the 全部商品 and 追蹤 views: tracking only narrows the
# rows, the columns stay the same (design 042 §4.2).
ALL_SORTS = {
    "code",
    "name",
    "market",
    "industry",
    "bank",
    "frequency",
    "next_distribution",
    "yield",
    "pay_date",
    "latest_close",
    "change_pct",
    "net_assets",
    "holders",
}
# Size and holder counts are compared largest-first when picking an ETF.
_DESCENDING_SORTS = ("yield", "latest_close", "change_pct", "net_assets", "holders")
# A filter value for "no value because the security stopped trading", kept
# apart from 資料不足 (missing data on a trading security).
DELISTED = "delisted"
# Design 046 §4.2.3: quarterly filter values by ex-dividend month group.
QUARTER_GROUP_FILTERS = {"quarterly_1": 1, "quarterly_2": 2, "quarterly_3": 3}


def natural_key(value):
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    )


def default_sort(mode):
    # Design 048 §4.2.3: ETFs are compared by size, stocks looked up by code.
    return ("net_assets", "desc") if mode == "etf" else ("code", "asc")


def normalize_sort(sort, direction, mode="stock"):
    if sort == "yield_desc":
        sort, direction = "yield", "desc"
    if sort not in ALL_SORTS:
        return default_sort(mode)
    return sort, direction if direction in ("asc", "desc") else (
        "desc" if sort in _DESCENDING_SORTS else "asc"
    )


def sort_rows(rows, sort, direction):
    def value(row):
        if sort == "code":
            return natural_key(row["code"])
        if sort in ("bank", "frequency") and row.get("status") == "delisted":
            # Design 048 §4.2.2: shown as 已下市 whatever value it still holds.
            return None
        if sort == "bank":
            return row["custodian_bank_display"]["value"]
        if sort == "frequency":
            display = row["distribution_frequency_display"]
            f = display["value"]
            # Design 046 §4.2.3: quarterly ETFs line up by ex-dividend group.
            group = display.get("quarter_group") or 9
            return (FREQUENCIES.index(f), group) if f in FREQUENCIES else None
        if sort == "yield":
            return row["current_yield"]
        if sort == "pay_date":
            return row["month_events"][0]["pay_date"] if row["month_events"] else None
        if sort == "next_distribution":
            return _event_date(row["next_distribution"])
        if sort in ("latest_close", "change_pct"):
            return Decimal(row[sort]) if row.get(sort) is not None else None
        if sort in ("net_assets", "holders"):
            return row.get(sort)
        if sort == "market":
            return 0 if row["market"] == "TWSE" else 1
        raw = row.get(sort)
        return unicodedata.normalize("NFKC", raw) if raw else None

    def unknown_rank(row):
        if row.get("status") == "delisted":
            return 0
        reason_field = (
            "bank_missing_reason" if sort == "bank" else "frequency_missing_reason"
        )
        return _MISSING_REASON_ORDER.get(row.get(reason_field), 8)

    stable = sorted(rows, key=lambda r: (natural_key(r["code"]), r["market"], r["id"]))
    known = [r for r in stable if value(r) is not None]
    unknown = [r for r in stable if value(r) is None]
    if sort in ("bank", "frequency"):
        unknown = sorted(unknown, key=unknown_rank)
    return sorted(known, key=value, reverse=direction == "desc") + unknown


def _slash_date(iso_date):
    return iso_date.replace("-", "/")


def _event_date(event):
    if event is None:
        return None
    return event["ex_dividend_date"] or event["pay_date"]


def market_distribution_freshness(conn, as_of, successes):
    """Design 052 §4.2.4: the date every listed ETF's current-year
    distributions are at least as fresh as — a few recently refreshed
    holdings must not make the whole list look current."""
    year = as_of[:4]
    synced, missing = [], 0
    for row in conn.execute(
        "SELECT id, market FROM securities "
        "WHERE security_type='etf' AND status='active'"
    ):
        unit = successes.get(
            (row["id"], sync_history.distribution_source(row["market"]), year)
        )
        if unit:
            synced.append(unit["attempted_at"])
        else:
            missing += 1
    if not synced:
        return "全市場配息尚未同步"
    label = f"全市場配息更新至 {_slash_date(taipei_date(min(synced)))}"
    return f"{label}（{missing} 檔尚未同步本年）" if missing else label


def next_distribution(events):
    """The nearest announced event not yet ex-dividend or not yet paid.

    `events` are already limited to `ex_dividend_date >= today OR
    pay_date >= today`; nothing is projected from frequency or month
    patterns (design 042 §4.3, design 023's known-records-only rule).
    """
    return min(
        events,
        key=lambda e: (
            _event_date(e) or "9999-12-31",
            not (e["ex_dividend_date"] and e["pay_date"]),
        ),
        default=None,
    )


def _close_change(price, calendar):
    """Conservative close-to-close change; never spans a missing trading day.

    `calendar` is the stored market calendar (issue #89), so a holiday gap
    counts as adjacent once it is known; an unknown gap stays unknown. This
    deliberately prefers a missing value to a multi-day return. Prices are
    unadjusted, as on the security-detail page.
    """
    if not price.get("previous_date") or price.get("previous_close") is None:
        return None
    if not calendar.adjacent(price["previous_date"], price["trade_date"]):
        return None
    current, previous = Decimal(price["close"]), Decimal(price["previous_close"])
    if current <= 0 or previous <= 0:
        return None
    return (current - previous) / previous * 100


def yield_summary(
    events,
    close,
    *,
    as_of=None,
    complete=False,
    frequency=None,
    month_pattern=None,
    listed_date=None,
    sync=None,
):
    """`sync` is `(latest_units, latest_successes, security_id, market)` for
    ETFs, whose distribution sync is tracked per security/year; without it
    (stocks), `complete` alone decides whether zero events may read as 0%."""
    result = yields.summarize(
        events,
        close=close,
        as_of=as_of,
        frequency=frequency,
        month_pattern=month_pattern,
    )
    status = None
    if sync is not None:
        units, successes, security_id, market = sync
        status = dividend_status(
            units,
            successes,
            security_id,
            market,
            result["end"],
            result["required_from_year"],
        )
        complete = status["years_ok"] and status["fresh_today"]
    missing_amount = result["missing_amount_count"]
    # Design 046 §4.2.4: nothing paid in the range is a real 0% once the
    # fund could have paid throughout it and every year of it is synced.
    no_payment = bool(
        result["no_payment_in_range"]
        and status is not None
        and status["years_ok"]
        and (
            result["paid_before_range"]
            or (listed_date is not None and listed_date < result["window_start"])
        )
    )
    result["no_payment"] = no_payment
    non_distributing = frequency == "non_distributing"
    # design 005 §4.5 follow-up: zero observed events looks identical
    # whether the security had a full opportunity to pay and confirmed
    # it didn't, or it simply listed after the query window started (a
    # real live case: 00400A listed 2026-04-09, ~150 days before its
    # first eligible payout window closes) — `listed_date` lets that be
    # shown distinctly instead of implying a confirmed non-payer, even
    # when every year is synced.
    newly_listed = (
        not result["observed_count"]
        and listed_date is not None
        and listed_date > result["window_start"]
    )
    # Design 028 §4.2: whether the cash side can be trusted, kept apart from
    # the price — cost yield needs the cash but never the close.
    # Design 059: a per-share amount across a capital reduction can't be
    # restated in today's shares, so it is not compared with today's price.
    unadjustable = any(r.get("unadjustable") for r in result["selected"])
    basis = result["estimate_basis"]
    if basis is not None and basis.get("unadjustable"):
        result["estimated_annual_cash"] = result["estimated_yield"] = None
        result["estimate_basis"], result["estimate_periods"] = None, None
        result["estimate_unavailable"] = "unadjustable"
    result["cash_reliable"] = not (
        non_distributing or newly_listed or unadjustable
    ) and bool(
        result["observed_count"] or no_payment or (complete and not missing_amount)
    )
    if not result["cash_reliable"]:
        result["known_yield"] = None
    result["complete"] = complete and not missing_amount
    never_synced = status is not None and not status["any_success"] and not events
    if non_distributing:
        cash_label = "不配息"
    elif unadjustable:
        cash_label = "股數異動無法換算"
    elif newly_listed:
        cash_label = "新上市，尚無配息紀錄"
    elif missing_amount:
        cash_label = f"{missing_amount} 筆配息金額待確認"
    elif never_synced:
        cash_label = "尚未同步配息資料"
    elif status is not None and not result["selected"]:
        cash_label = "尚無已知配息紀錄"
    else:
        cash_label = "資料不足"
    result["cash_label"] = None if result["cash_reliable"] else cash_label
    if newly_listed or non_distributing or unadjustable:
        label = cash_label
    elif result["known_yield"] is None:
        label = "缺少有效收盤價，無法計算" if result["observed_count"] else cash_label
    elif not result["observed_count"]:
        # An ETF's own note says 近一年未配息; stocks keep the generic label.
        label = None if no_payment else "該期間未查得配息"
    else:
        # Design 023 §4.5: an ETF's ordinary row shows only its update and
        # close dates; specific problems go to `notes` instead.
        label = None if status is not None else "依已知紀錄計算"
    result["quality_label"] = label

    notes = []
    if status is not None and not never_synced and not non_distributing:
        notes += [f"{year} 年配息資料尚未同步" for year in status["unsynced_years"]]
        notes += [
            f"{item['year']} 年配息更新失敗"
            + (
                f"（前次成功 {_slash_date(item['last_success'])}）"
                if item["last_success"]
                else ""
            )
            for item in status["failed_years"]
        ]
    if missing_amount and result["known_yield"] is not None:
        notes.append(f"{missing_amount} 筆配息金額待確認")
    if result["provisional"]:
        notes.append("含暫估金額")
    if result["overdue"]:
        notes.append("已超過一個配息週期未查得新配息")
    if no_payment:
        notes.append("近一年未配息")
    if result["annualized_from"]:
        notes.append(f"推估（依 {result['annualized_from']} 次）")
    if result["short_count"]:
        notes.append(
            f"近一年僅配 {result['short_count']} 次（應 {result['expected_count']} 次）"
        )
    if result["frequency_changed"]:
        notes.append("近期頻率變動")
    if result["extra_distribution"]:
        notes.append("近一年含額外配息")
    if result["cash_reliable"] and any(
        r.get("share_adjusted") for r in result["selected"]
    ):
        notes.append("已依股數異動換算")
    if result["needs_review_count"]:
        notes.append(f"{result['needs_review_count']} 筆來源待覆核，未計入")
    result["notes"] = notes

    result["updated_on"] = status["updated_on"] if status else None
    result["updated_label"] = None
    if status is not None:
        fetched = [
            taipei_date(row["fetched_at"])
            for row in map(dict, events)
            if row.get("fetched_at")
        ]
        if status["updated_on"]:
            result["updated_label"] = f"配息更新至 {_slash_date(status['updated_on'])}"
        elif fetched:
            # No year was ever confirmed synced: the rows exist, but nothing
            # says the source had no others (design 023 §4.4 point 5).
            result["updated_label"] = f"紀錄取得於 {_slash_date(max(fetched))}"
        elif not never_synced:
            result["updated_label"] = "更新日期未知"
    return result


def frequency_and_yield(
    profile,
    events,
    close,
    *,
    as_of,
    security_type,
    security_id,
    market,
    listed_date,
    units,
    successes,
    actions=(),
):
    """One entry point for the catalog list, detail page and holdings, so
    all three show the same frequency, months and yield (design 058 §4.2.2).
    Amounts are restated in today's shares across `actions` (design 059).
    The frequency reads only ex-dividend events already past (design 046);
    with none it gets `None`, which keeps a stored inferred month pattern."""
    past = [
        dict(e)
        for e in events
        if e["ex_dividend_date"] and e["ex_dividend_date"] <= as_of
    ]
    frequency = profile_values.distribution_frequency(
        dict(profile) if profile else {},
        events=past or None,
        units=units,
        security_id=security_id,
        market=market,
        as_of=as_of,
    )
    etf = security_type == "etf"
    return frequency, yield_summary(
        yields.adjust_for_actions(events, actions, as_of),
        close,
        as_of=as_of,
        complete=True,
        frequency=frequency["value"],
        month_pattern=frequency["month_pattern"],
        listed_date=listed_date,
        sync=(units, successes, security_id, market) if etf else None,
    )


def dividend_status(units, successes, security_id, market, as_of, from_year):
    """Design 023 §4.3–4.4: keeps "which years were confirmed", "was it
    confirmed today" and "when was it last confirmed" apart instead of
    folding them into one complete/incomplete flag."""
    source = sync_history.distribution_source(market)
    current_year = int(as_of[:4])
    years = range(min(from_year, current_year), current_year + 1)
    unsynced, failed, confirmed = [], [], []
    for year in years:
        key = (security_id, source, str(year))
        latest, success = units.get(key), successes.get(key)
        if success:
            confirmed.append(taipei_date(success["attempted_at"]))
        if latest is None:
            unsynced.append(year)
        elif sync_history.is_failure(latest["status"]):
            failed.append(
                {
                    "year": year,
                    "last_success": taipei_date(success["attempted_at"])
                    if success
                    else None,
                }
            )
    years_ok = sync_history.years_covered(units, security_id, market, years)
    current_key = (security_id, source, str(current_year))
    current = units.get(current_key)
    # Only this year's success says how current the data is; an older
    # year's success must not stand in for a year that never succeeded.
    current_success = successes.get(current_key)
    return {
        "years_ok": years_ok,
        # A previous run cannot certify announcements published since it
        # ran — only the zero-yield judgement still needs this.
        "fresh_today": bool(
            years_ok and current and taipei_date(current["attempted_at"]) >= as_of
        ),
        "updated_on": taipei_date(current_success["attempted_at"])
        if current_success
        else None,
        "any_success": bool(confirmed),
        "unsynced_years": unsynced,
        "failed_years": failed,
    }


def browse(
    conn,
    *,
    mode="stock",
    view="all",
    q=None,
    market=None,
    industry=None,
    pay_month=None,
    bank=None,
    frequency=None,
    sort="code",
    direction="asc",
    page=1,
    include_delisted=False,
):
    rows = search.candidate_securities(
        conn,
        query=q,
        security_type=mode,
        market=market,
        industry=industry,
        pay_month=pay_month,
        tracked_only=view == "watchlist",
    )
    ids = [r["id"] for r in rows]
    profiles, prices, events, month_events, upcoming = {}, {}, {}, {}, {}
    actions = {}
    as_of = today_taipei()
    units = sync_history.latest_units(conn)
    successes = sync_history.latest_successes(conn) if mode == "etf" else {}
    listing_details = delistings.details(conn)
    calendar = trading_days.TradingCalendar(conn)
    scale = etf_scale.latest(conn) if mode == "etf" else None
    for offset in range(0, len(ids), 400):
        batch = ids[offset : offset + 400]
        marks = ",".join("?" for _ in batch)
        profiles.update(
            {
                r["security_id"]: dict(r)
                for r in conn.execute(
                    f"SELECT * FROM etf_profiles WHERE security_id IN ({marks})", batch
                )
            }
        )
        prices.update(
            {
                r["security_id"]: dict(r)
                for r in conn.execute(
                    f"""
                    SELECT dp.*,
                      (SELECT p.close FROM daily_prices p
                       WHERE p.security_id=dp.security_id AND p.trade_date<dp.trade_date
                       ORDER BY p.trade_date DESC LIMIT 1) AS previous_close,
                      (SELECT max(p.trade_date) FROM daily_prices p
                       WHERE p.security_id=dp.security_id AND p.trade_date<dp.trade_date
                      ) AS previous_date
                    FROM daily_prices dp WHERE security_id IN
                    ({marks}) AND trade_date=(SELECT max(trade_date) FROM
                    daily_prices p WHERE p.security_id=dp.security_id)
                    """,
                    batch,
                )
            }
        )
        # Every past event, not just the last 12 months: a known frequency
        # takes the latest N wherever they fall (design 023 §4.2).
        for r in conn.execute(
            f"""
            SELECT * FROM distributions WHERE security_id IN ({marks}) AND
            ex_dividend_date<=?
            """,
            [*batch, as_of],
        ):
            events.setdefault(r["security_id"], []).append(dict(r))
        for r in conn.execute(
            f"SELECT * FROM corporate_actions WHERE security_id IN ({marks})", batch
        ):
            actions.setdefault(r["security_id"], []).append(dict(r))
        for r in conn.execute(
            f"""
            SELECT * FROM distributions WHERE security_id IN ({marks}) AND
            (ex_dividend_date>=? OR pay_date>=?) AND {COUNTABLE_SQL}
            """,
            [*batch, as_of, as_of],
        ):
            upcoming.setdefault(r["security_id"], []).append(dict(r))
        if pay_month:
            for r in conn.execute(
                f"""
                SELECT * FROM distributions WHERE security_id IN ({marks}) AND
                substr(pay_date,1,7)=? AND {COUNTABLE_SQL} ORDER BY pay_date
                """,
                [*batch, pay_month],
            ):
                month_events.setdefault(r["security_id"], []).append(dict(r))
    enriched = []
    for row in rows:
        item = dict(row)
        sid = item["id"]
        item.update(listing_details.get(sid, {}))
        item["security_id"] = sid
        profile = profiles.get(sid, {})
        price = prices.get(sid, {})
        item["latest_close"] = price.get("close")
        item["close_date"] = price.get("trade_date")
        item["change_pct"] = _close_change(price, calendar)
        item["previous_close_date"] = price.get("previous_date")
        item["industry_name"] = industries.industry_name(
            item.get("industry"), item["market"]
        )
        item["custodian_bank_display"] = profile_values.custodian_bank(profile)
        # Sync status only exists for the per-unit ETF distribution-list
        # sync; stock dividends come from `sync_ex_dividend_notices`, which
        # commits its whole batch in one transaction and isn't tracked per
        # security/year in `catalog_sync_units` — treating that as "always
        # incomplete" would mislabel every stock regardless of actual data
        # quality.
        item["distribution_frequency_display"], info = frequency_and_yield(
            profile,
            events.get(sid, []),
            price.get("close"),
            as_of=as_of,
            security_type=mode,
            security_id=sid,
            market=item["market"],
            listed_date=item.get("listed_date"),
            units=units,
            successes=successes,
            actions=actions.get(sid, ()),
        )
        unit = units.get((sid, "investment_trust", None))
        item["bank_missing_reason"] = profile_values.missing_reason(unit, field="bank")
        item["frequency_missing_reason"] = profile_values.missing_reason(
            unit, has_events=item["has_events"]
        )
        if (
            profile.get("distribution_policy") == "distributing"
            and not item["distribution_frequency_display"]["value"]
        ):
            item["frequency_missing_reason"] = "有配息，頻率待確認"
        source = (
            profile_values.json_value(profile.get("custodian_bank_source"), {}) or {}
        )
        item["bank_conflict"] = source.get("cross_check") == "conflict"
        item["sync_failed"] = bool(
            unit and sync_history.is_transient_failure(unit["status"])
        )
        outcomes = (
            profile_values.json_value(unit.get("field_results"), {}) if unit else {}
        )
        bank_outcome = (outcomes or {}).get("bank")
        item["bank_sync_failed"] = (
            bank_outcome["status"] != "not_provided"
            and sync_history.is_transient_failure(bank_outcome["status"])
            if bank_outcome
            else item["sync_failed"]
        )
        item["yield_info"] = info
        item["current_yield"] = info["known_yield"]
        # Design 023 §4.5: an ordinary row shows only the update and close
        # dates; specific problems add their own short lines.
        item["yield_notes"] = [
            *([info["quality_label"]] if info["quality_label"] else []),
            *info["notes"],
            *([info["updated_label"]] if info["updated_label"] else []),
        ]
        item["month_events"] = month_events.get(sid, [])
        item["next_distribution"] = next_distribution(upcoming.get(sid, []))
        if scale is not None:
            item["net_assets"] = scale["sizes"].get(sid, {}).get("net_assets_twd")
            item["holders"] = scale["holders"].get(sid, {}).get("holders")
        enriched.append(item)

    def matches_missing(r, val, wanted):
        # Design 048 §4.2.2: a delisted row reads 已下市 in both columns,
        # so it matches only that option, whatever value it still holds.
        if r["status"] == "delisted":
            return wanted == DELISTED
        if wanted == "unknown":
            return val is None
        return val == wanted

    def matches_bank(r):
        val = r["custodian_bank_display"]["value"]
        return not bank or matches_missing(r, val, bank)

    def matches_frequency(r):
        display = r["distribution_frequency_display"]
        if frequency in QUARTER_GROUP_FILTERS:
            return (
                r["status"] != "delisted"
                and display["value"] == "quarterly"
                and display.get("quarter_group") == QUARTER_GROUP_FILTERS[frequency]
            )
        return not frequency or matches_missing(r, display["value"], frequency)

    # Design 048 §4.2.1: 全部商品 lists trading securities unless asked (an
    # 已下市 filter asks too); a tracked delisted security stays in 追蹤.
    delisted_hidden = 0
    if view == "all" and not include_delisted and DELISTED not in (bank, frequency):
        delisted_hidden = sum(
            r["status"] == "delisted" and matches_bank(r) and matches_frequency(r)
            for r in enriched
        )
        enriched = [r for r in enriched if r["status"] != "delisted"]

    def coverage_tier(r):
        # An inherited value is the fund's own official value (design 043).
        tier = r["distribution_frequency_display"]["tier"]
        return "official" if tier == "share_class" else tier

    bank_scope = [r for r in enriched if matches_frequency(r)]
    freq_scope = [r for r in enriched if matches_bank(r)]
    # `None` counts only trading securities, matching the 資料不足 filter.
    banks = Counter(
        r["custodian_bank_display"]["value"]
        for r in bank_scope
        if r["status"] != "delisted"
    )
    bank_delisted = sum(r["status"] == "delisted" for r in bank_scope)
    tiers = Counter(coverage_tier(r) for r in freq_scope)
    # Design 047: per-option counts, same scope rule as the bank options.
    frequencies = Counter()
    for r in freq_scope:
        display = r["distribution_frequency_display"]
        value = display["value"]
        if r["status"] == "delisted":
            frequencies[DELISTED] += 1
            continue
        if value is None:
            frequencies["unknown"] += 1
            continue
        frequencies[value] += 1
        if value == "quarterly":
            for key, group in QUARTER_GROUP_FILTERS.items():
                if display.get("quarter_group") == group:
                    frequencies[key] += 1
    result = [r for r in bank_scope if matches_bank(r)]
    active_result = [r for r in result if r["status"] == "active"]
    result_tiers = Counter(
        "user_override"
        if coverage_tier(r) == "user_override"
        else "non_distributing"
        if r["distribution_frequency_display"]["value"] == "non_distributing"
        else coverage_tier(r)
        for r in active_result
    )
    result_coverage = {
        "total": len(active_result),
        "bank_known": sum(
            bool(r["custodian_bank_display"]["value"]) for r in active_result
        ),
        **{
            key: result_tiers[key]
            for key in (
                "official",
                "non_distributing",
                "inferred",
                "user_override",
                "unknown",
            )
        },
    }
    total = len(result)
    pages = max(1, (total + search.DEFAULT_LIMIT - 1) // search.DEFAULT_LIMIT)
    page = min(max(1, page), pages)
    result = sort_rows(result, sort, direction)
    return {
        "results": result[
            (page - 1) * search.DEFAULT_LIMIT : page * search.DEFAULT_LIMIT
        ],
        "total_results": total,
        "total_pages": pages,
        "page": page,
        "has_next_page": page < pages,
        "custodian_banks": sorted(b for b in banks if b),
        "bank_counts": banks,
        "bank_delisted_count": bank_delisted,
        "delisted_hidden": delisted_hidden,
        "frequency_counts": frequencies,
        "scale_dates": etf_scale.display_dates(scale),
        "distribution_freshness": (
            market_distribution_freshness(conn, as_of, successes)
            if mode == "etf"
            else None
        ),
        "result_coverage": result_coverage,
        "coverage": {
            "bank_total": len(bank_scope),
            "bank_known": sum(n for b, n in banks.items() if b),
            "frequency_total": len(freq_scope),
            **tiers,
            "non_distributing": sum(
                r["distribution_frequency_display"]["value"] == "non_distributing"
                and coverage_tier(r) == "official"
                for r in freq_scope
            ),
            "official_frequency": sum(
                coverage_tier(r) == "official"
                and r["distribution_frequency_display"]["value"] != "non_distributing"
                for r in freq_scope
            ),
        },
        "latest_sync": conn.execute(
            "SELECT max(finished_at) FROM catalog_sync_runs"
        ).fetchone()[0],
    }


def resolve_security_by_code(
    conn: sqlite3.Connection,
    code_input: str | None,
    market: str | None = None,
) -> sqlite3.Row | None:
    """Resolves a security by code, disambiguating multi-market transfers.

    Supports 'TWSE:8908' / 'TPEx:8908' syntax or an explicit `market` argument.
    When multiple rows match the same code (e.g. TPEx delisted + TWSE active after
    an exchange transfer), prioritizes the single 'active' security.
    """
    code = (code_input or "").strip()
    if not code:
        return None

    if ":" in code:
        prefix, rest = code.split(":", 1)
        if prefix.upper() in ("TWSE", "TPEX"):
            market = "TWSE" if prefix.upper() == "TWSE" else "TPEx"
            code = rest.strip()

    if market:
        norm_market = (
            "TWSE"
            if market.upper() == "TWSE"
            else ("TPEx" if market.upper() == "TPEX" else market)
        )
        return conn.execute(
            "SELECT * FROM securities WHERE market = ? AND code = ?",
            (norm_market, code),
        ).fetchone()

    sql = (
        "SELECT * FROM securities WHERE code = ? "
        "ORDER BY (status = 'active') DESC, rowid DESC"
    )
    rows = conn.execute(sql, (code,)).fetchall()
    if not rows:
        return None
    active = [r for r in rows if r["status"] == "active"]
    if len(active) == 1:
        return active[0]
    return rows[0]
