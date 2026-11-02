"""Shared effective ETF values for search, detail, filtering and audits."""

import json
from datetime import date

from taiwan_stock.catalog import frequency_inference, sync_history
from taiwan_stock.catalog.bank_dictionary import canonical_name
from taiwan_stock.catalog.distribution_rows import countable
from taiwan_stock.shared.dates import today_taipei

FREQUENCY_LABELS = {
    "monthly": "月配",
    "bimonthly": "雙月配",
    "quarterly": "季配",
    "semiannual": "半年配",
    "annual": "年配",
    "irregular": "不定期",
    "non_distributing": "不配息",
    "unknown": "資料不足",
}
# "inferred" values come from the local distribution history (design 023
# §4.6) — say where they came from rather than just "a guess".
# "share_class" values come from the same fund's TWD class (design 043).
TIER_LABELS = {
    "official": "官方",
    "inferred": "歷史",
    "user_override": "自訂",
    "share_class": "同基金",
}


def json_value(raw, default=None):
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _share_class_code(profile, field):
    """The TWD class code when this official value was inherited from it."""
    source = json_value(profile.get(field + "_source"), {})
    if isinstance(source, dict) and source.get("origin") == "share_class":
        return source.get("from_code")
    return None


def custodian_bank(profile):
    for key, tier in (
        ("custodian_bank_user_override", "user_override"),
        ("custodian_bank", "official"),
    ):
        value = canonical_name(profile.get(key))
        if value:
            from_code = _share_class_code(profile, key) if tier == "official" else None
            if from_code:
                return {"value": value, "tier": "share_class", "from_code": from_code}
            return {"value": value, "tier": tier}
    return {"value": None, "tier": "unknown"}


def _valid_ex_dividend_months(frequency, months, *, require_even_spacing=False):
    interval = {
        "monthly": 1,
        "bimonthly": 2,
        "quarterly": 3,
        "semiannual": 6,
        "annual": 12,
    }.get(frequency)
    if (
        interval is None
        or not isinstance(months, list)
        or len(months) != 12 // interval
        or any(type(month) is not int or not 1 <= month <= 12 for month in months)
        or len(set(months)) != len(months)
    ):
        return False
    ordered = sorted(months)
    if not require_even_spacing:
        return True
    return all(
        (ordered[(i + 1) % len(ordered)] - month) % 12 == interval % 12
        for i, month in enumerate(ordered)
    )


QUARTER_GROUP_MONTHS = {1: [1, 4, 7, 10], 2: [2, 5, 8, 11], 3: [3, 6, 9, 12]}


def quarter_group_of(month):
    return (month - 1) % 3 + 1


def quarter_group(events):
    """Design 046 §4.2.2: the ex-dividend month group two payments agree on.

    A payment moved into a neighbouring month simply agrees with no other
    and is ignored. With no two in one group yet, the latest one stands in
    as provisional until the next payment confirms or moves it.
    """
    days = sorted(
        (
            row["ex_dividend_date"]
            for row in events or ()
            if row.get("ex_dividend_date") and countable(row)
        ),
        reverse=True,
    )[:4]
    if not days:
        return None, None
    groups = [quarter_group_of(int(day[5:7])) for day in days]
    best = max(groups.count(g) for g in set(groups))
    if best < 2:
        return groups[0], "provisional"
    # On a 2–2 tie the latest payment's group is the current one.
    return next(g for g in groups if groups.count(g) == best), "confirmed"


def _ex_dividend_days(events, as_of):
    return sorted(
        row["ex_dividend_date"]
        for row in events or ()
        if row.get("ex_dividend_date")
        and row["ex_dividend_date"] <= as_of
        and countable(row)
    )


def _record_mismatch(profile, value, events=None, as_of=None):
    """The latest full year's own count, when it disagrees with `value`.

    Design 048 §4.2.6: otherwise this year's count so far, once it already
    exceeds what `value` allows in a whole year.
    """
    inferred = json_value(profile.get("distribution_frequency_inferred"), {})
    if (
        isinstance(inferred, dict)
        and inferred.get("method") == "year_count"
        and inferred.get("value") in FREQUENCY_LABELS
        and inferred["value"] != value
    ):
        return {"year": inferred.get("basis_year"), "count": inferred.get("year_count")}
    bounds = frequency_inference.YEAR_COUNTS.get(value)
    if bounds and events is not None and as_of:
        year = as_of[:4]
        count = sum(day[:4] == year for day in _ex_dividend_days(events, as_of))
        if count > bounds[1]:
            return {"year": int(year), "count": count, "to_date": True}
    return None


def _annual_months(events, as_of):
    """Design 048 §4.2.7: an official annual fund's month from its payments.

    The frequency is already official, so no full year has to confirm it;
    the latest two payments agreeing settles the month, one is provisional.
    """
    days = _ex_dividend_days(events, as_of)
    if not days or sum(day[:4] == as_of[:4] for day in days) > 1:
        return None, None
    latest = int(days[-1][5:7])
    if len(days) > 1 and int(days[-2][5:7]) == latest:
        return [latest], "inferred"
    return [latest], "provisional"


def _historical_semiannual_months(events, units, security_id, market, as_of):
    """Accept a 5/7-month cadence only when two full years agree.

    This adds months to a known official frequency; it never infers a
    semiannual frequency from irregular day gaps alone.
    """
    if not events or not units or not security_id or not market:
        return None
    current_year = int(as_of[:4])
    years = (current_year - 2, current_year - 1)
    for year in years:
        if not sync_history.years_covered(units, security_id, market, [year]):
            return None
        unit = units.get(
            (security_id, sync_history.distribution_source(market), str(year))
        )
        if (unit.get("attempted_at") or "")[:10] < f"{year + 1}-01-01":
            return None
    dates = [
        date.fromisoformat(row["ex_dividend_date"])
        for row in events
        if row.get("ex_dividend_date")
        and row["ex_dividend_date"] <= as_of
        and countable(row)
    ]
    month_sets = []
    for year in years:
        observed = [day.month for day in dates if day.year == year]
        if len(observed) != 2 or len(set(observed)) != 2:
            return None
        month_sets.append(sorted(observed))
    if month_sets[0] != month_sets[1]:
        return None
    months = month_sets[1]
    current = [day.month for day in dates if day.year == current_year]
    if any(month not in months for month in current):
        return None
    if date.fromisoformat(as_of).month > max(months) and len(current) != 2:
        return None
    return months


def distribution_frequency(
    profile, *, events=None, units=None, security_id=None, market=None, as_of=None
):
    result = _distribution_frequency(
        profile,
        events=events,
        units=units,
        security_id=security_id,
        market=market,
        as_of=as_of,
    )
    if result["tier"] == "official":
        field = (
            "distribution_policy"
            if result["value"] == "non_distributing"
            and not profile.get("distribution_frequency")
            else "distribution_frequency"
        )
        from_code = _share_class_code(profile, field)
        if from_code:
            result["tier"] = "share_class"
            result["from_code"] = from_code
            if result["month_tier"] == "official":
                result["month_tier"] = "share_class"
    return result


def _distribution_frequency(
    profile, *, events=None, units=None, security_id=None, market=None, as_of=None
):
    override = profile.get("distribution_frequency_user_override")
    if override not in FREQUENCY_LABELS or override == "unknown":
        if profile.get("distribution_policy") == "non_distributing":
            return {
                "value": "non_distributing",
                "label": FREQUENCY_LABELS["non_distributing"],
                "tier": "official",
                "month_pattern": None,
                "month_tier": None,
                "basis_dates": None,
                "quarter_group": None,
                "quarter_group_status": None,
                "record_mismatch": None,
            }
    for key, tier in (
        ("distribution_frequency_user_override", "user_override"),
        ("distribution_frequency", "official"),
        ("distribution_frequency_inferred", "inferred"),
    ):
        payload = json_value(profile.get(key), {}) if tier == "inferred" else {}
        if not isinstance(payload, dict):
            payload = {}
        value = payload.get("value") if tier == "inferred" else profile.get(key)
        if isinstance(value, str) and value in FREQUENCY_LABELS and value != "unknown":
            months, month_tier = None, None
            confirmed_nonuniform = False
            if tier == "inferred":
                months, month_tier = payload.get("month_pattern"), "inferred"
            elif tier == "official":
                schedule = json_value(
                    profile.get("distribution_ex_dividend_schedule"), {}
                )
                if (
                    isinstance(schedule, dict)
                    and schedule.get("frequency") == value
                    and _valid_ex_dividend_months(value, schedule.get("months"))
                ):
                    months, month_tier = schedule.get("months"), "official"
                # Two full years agreeing on uneven months (e.g. 7、11 月)
                # beat evenly spaced months counted back from the latest
                # payment, which design 046's full-year rule also infers.
                annual_from_events = value == "annual" and events is not None
                if months is None and annual_from_events:
                    months, month_tier = _annual_months(events, as_of or today_taipei())
                if months is None and value == "semiannual" and events is not None:
                    months = _historical_semiannual_months(
                        events, units, security_id, market, as_of or today_taipei()
                    )
                    if months:
                        month_tier = "inferred"
                        confirmed_nonuniform = True
                if months is None and not annual_from_events:
                    inferred = json_value(
                        profile.get("distribution_frequency_inferred"), {}
                    )
                    if isinstance(inferred, dict) and inferred.get("value") == value:
                        months, month_tier = inferred.get("month_pattern"), "inferred"
            group = group_status = None
            if value == "quarterly":
                if month_tier == "official":
                    group, group_status = quarter_group_of(months[0]), "official"
                elif events is not None:
                    # Design 046 §4.2.2: groups replace the latest-month guess.
                    group, group_status = quarter_group(events)
                    months = QUARTER_GROUP_MONTHS.get(group)
                    month_tier = (
                        "provisional" if group_status == "provisional" else "inferred"
                    )
            if not _valid_ex_dividend_months(
                value,
                months,
                require_even_spacing=month_tier in ("inferred", "provisional")
                and not confirmed_nonuniform,
            ):
                months, month_tier = None, None
            # Issue #143: one label per value. 半年配 means twice a year in
            # investor usage, not a fixed six-month gap; uneven months such as
            # 7、11 月 are shown on the month line (reverses design 040's 每年 2 次).
            return {
                "value": value,
                "label": FREQUENCY_LABELS[value],
                "tier": tier,
                "month_pattern": sorted(months) if months else None,
                "month_tier": month_tier,
                "basis_dates": payload.get("basis_dates"),
                "quarter_group": group,
                "quarter_group_status": group_status,
                "record_mismatch": _record_mismatch(
                    profile, value, events, as_of or today_taipei()
                )
                if tier == "official"
                else None,
            }
    return {
        "value": None,
        "label": None,
        "tier": "unknown",
        "month_pattern": None,
        "month_tier": None,
        "basis_dates": None,
        "quarter_group": None,
        "quarter_group_status": None,
        "record_mismatch": None,
    }


def _outcome_reason(status):
    """Only a transient failure reads as "同步失敗" (issue #135)."""
    if status == "unsupported":
        return "來源尚未支援"
    if status == "not_provided":
        return "來源未提供此欄位"
    if status in sync_history.NOT_LISTED_STATUSES:
        return "來源未收錄此商品"
    if status in sync_history.SOURCE_CHANGED:
        return "來源待更新"
    if sync_history.is_maintenance(status):
        return "來源維護中"
    if sync_history.is_transient_failure(status):
        return "同步失敗"
    return None


def missing_reason(unit, *, has_events=False, field=None):
    if unit:
        if unit["status"] in sync_history.IN_PROGRESS:
            return "同步處理中"
        outcomes = json_value(unit.get("field_results"), {}) or {}
        outcome = outcomes.get(field or "frequency", {})
        reason = _outcome_reason(outcome.get("status")) if outcome else None
        if reason:
            return reason
        reason = _outcome_reason(unit["status"])
        if reason:
            return reason
        if field == "bank":
            return "來源未提供此欄位"
    if has_events:
        return "有配息紀錄，頻率待確認"
    return "紀錄不足，無法推測" if unit else "尚未同步"
