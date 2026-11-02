"""Cash yields from the latest N distributions (design 023 §4.2); estimates
annualize the latest final payment (design 028 §4.4) and never replace
observed cash."""

import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from taiwan_stock.catalog.distribution_rows import countable
from taiwan_stock.shared.dates import today_taipei

FREQUENCIES = {
    "monthly": 12,
    "bimonthly": 6,
    "quarterly": 4,
    "semiannual": 2,
    "annual": 1,
}
# Real elapsed-day range between two consecutive ex-dividend dates for each
# frequency. Shared with `catalog.frequency_inference` (which classifies a
# gap with it) so "one cycle" means the same thing everywhere: the lower
# bound flags an extra payment squeezed into a cycle, the upper bound flags
# a cycle that has passed without a new payment.
CYCLE_DAYS = {
    "monthly": (24, 36),
    "bimonthly": (50, 68),
    "quarterly": (78, 100),
    "semiannual": (165, 195),
    "annual": (345, 385),
}


# Design 046 §4.2.4: how far before "12 months ago" a payment still counts,
# so one paid a little late is not dropped — half a cycle, as (months, days).
# Annual is calendar based instead (from 1 January of last year).
GRACE = {
    "monthly": (0, 15),
    "bimonthly": (1, 0),
    "quarterly": (1, 15),
    "semiannual": (3, 0),
}


def window_start(as_of: str, frequency: str) -> str:
    today = date.fromisoformat(as_of)
    if frequency == "annual":
        return date(today.year - 1, 1, 1).isoformat()
    months, days = GRACE[frequency]
    return (add_months(today, -12 - months) - timedelta(days=days)).isoformat()


def _cycle_of(days: int) -> str | None:
    return next(
        (name for name, (low, high) in CYCLE_DAYS.items() if low <= days <= high),
        None,
    )


def add_months(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


SHARE_ADJUSTING_TYPES = ("split", "stock_dividend")
# Recorded but carrying no ratio, so a per-share amount across one can't
# be restated (design 059 §4.2.1).
UNADJUSTABLE_TYPES = ("cash_capital_reduction", "other")
_ADJUSTED_QUANTUM = Decimal("0.000001")


def adjust_for_actions(events, actions, as_of):
    """Copies of `events` with each per-share amount restated in today's
    shares (design 059): a split or stock dividend on or after its
    ex-dividend date, and no later than `as_of`, divides it by the ratio.
    The same-day case adjusts too, since the cash went to pre-action
    shares. Announced rows and entitlements are never changed."""
    adjusted = []
    for event in events:
        row = dict(event)
        ex_date = row.get("ex_dividend_date")
        applicable = [
            a for a in actions or () if ex_date and ex_date <= a["ex_date"] <= as_of
        ]
        row["unadjustable"] = any(
            a["action_type"] in UNADJUSTABLE_TYPES for a in applicable
        )
        factor = Decimal(1)
        for action in applicable:
            if action["action_type"] in SHARE_ADJUSTING_TYPES:
                factor *= Decimal(action["ratio_denominator"]) / Decimal(
                    action["ratio_numerator"]
                )
        row["share_adjusted"] = factor != 1 and row["amount_per_share"] is not None
        if row["share_adjusted"]:
            row["original_amount_per_share"] = row["amount_per_share"]
            amount = (Decimal(row["amount_per_share"]) * factor).quantize(
                _ADJUSTED_QUANTUM, rounding=ROUND_HALF_UP
            )
            # Trailing zeros dropped without turning 10 into 1E+1.
            row["amount_per_share"] = (
                amount.quantize(Decimal(1))
                if amount == amount.to_integral()
                else amount.normalize()
            )
        adjusted.append(row)
    return adjusted


def _days_between(older: str, newer: str) -> int:
    return (date.fromisoformat(newer) - date.fromisoformat(older)).days


def _nonuniform_semiannual_months(frequency, months):
    return (
        frequency == "semiannual"
        and isinstance(months, list)
        and len(months) == 2
        and all(type(month) is int and 1 <= month <= 12 for month in months)
        and (months[1] - months[0]) % 12 not in (0, 6)
    )


def _scheduled_pair(older, newer, months):
    """Two payments in the schedule's two months, about as far apart as
    those months are (design 058 §4.2.3); ex-dates drift, months don't."""
    first = date.fromisoformat(older)
    second = date.fromisoformat(newer)
    if first.month not in months or second.month not in months:
        return False
    if first.month == second.month:
        return False
    expected = (second.month - first.month) % 12 * 30.44
    return abs((second - first).days - expected) <= 31


def _estimate_basis(past, latest_final, frequency, months=None):
    """Design 028 §4.4: the latest final payment, annualized, says what the
    current payout level is; averaging the latest N would only repeat the
    known yield. Returns `(row, None)` or `(None, reason)`."""
    if frequency not in FREQUENCIES:
        return None, "unknown_frequency"
    if not latest_final:
        return None, "no_final"
    latest = latest_final[-1]
    earlier = [r for r in past if r["ex_dividend_date"] < latest["ex_dividend_date"]]
    # Closer than one cycle to the payment before it can only be an extra
    # payment, which must not be scaled up as if it were the regular one.
    if (
        earlier
        and _days_between(earlier[-1]["ex_dividend_date"], latest["ex_dividend_date"])
        < CYCLE_DAYS[frequency][0]
        and not (
            months
            and _scheduled_pair(
                earlier[-1]["ex_dividend_date"], latest["ex_dividend_date"], months
            )
        )
    ):
        return None, "extra_distribution"
    return latest, None


def summarize(
    distributions, close=None, as_of=None, frequency=None, month_pattern=None
):
    as_of = as_of or today_taipei()
    months = (
        sorted(month_pattern)
        if _nonuniform_semiannual_months(frequency, month_pattern)
        else None
    )
    start = add_months(date.fromisoformat(as_of), -12).isoformat()
    # Announced but not yet ex-dividend events are not cash already earned;
    # a flagged row awaiting review is shown elsewhere but never counted.
    earned = [
        dict(r)
        for r in distributions
        if r["ex_dividend_date"] and r["ex_dividend_date"] <= as_of
    ]
    past = sorted(
        (r for r in earned if countable(r)), key=lambda r: r["ex_dividend_date"]
    )
    latest_final = [
        r
        for r in past
        if r["amount_per_share"] is not None and r["amount_status"] == "final"
    ]
    if latest_final:
        frequency = latest_final[-1].get("frequency_label") or frequency
    f = FREQUENCIES.get(frequency)
    in_window = [r for r in past if r["ex_dividend_date"] > start]
    # Ex-dates drift a few days each year, so a known frequency takes the
    # latest N events of a range half a cycle wider than 12 months rather
    # than whatever a fixed 12-month window happens to catch.
    counted_from = window_start(as_of, frequency) if f else start
    in_range = [r for r in past if r["ex_dividend_date"] >= counted_from]
    # Two or more gaps of one other known cadence inside the range mean the
    # policy changed (or the stated frequency is stale). One skipped
    # payment leaves a single odd gap; one extra payment splits a cycle
    # into two short gaps that rarely share a cadence.
    gaps = [
        _cycle_of(_days_between(a["ex_dividend_date"], b["ex_dividend_date"]))
        for a, b in zip(in_range, in_range[1:], strict=False)
    ]
    frequency_changed = bool(f) and any(
        gaps.count(other) >= 2 for other in set(gaps) if other and other != frequency
    )
    # Two events closer than one cycle inside the window can only be an
    # extra payment, which the latest N would wrongly crowd out.
    extra_distribution = bool(
        f
        and not frequency_changed
        and len(in_window) > f
        and any(
            _days_between(a, b) < CYCLE_DAYS[frequency][0]
            and not (months and _scheduled_pair(a, b, months))
            for a, b in pairwise(r["ex_dividend_date"] for r in in_window)
        )
    )
    by_count = bool(f) and not extra_distribution and not frequency_changed
    # The range already holds whole cycles, so a regular payer always has N.
    rows = in_range[-f:] if by_count else in_window
    known = [r for r in rows if r["amount_per_share"] is not None]
    total = sum((Decimal(r["amount_per_share"]) for r in known), Decimal(0))
    short = by_count and 0 < len(rows) < f
    # Started paying inside the range: the average stands in for the
    # payments it has not had the time to make yet. A fund paying for
    # years with fewer in range really did pay fewer, so it stays a sum.
    newly_paying = short and past[0]["ex_dividend_date"] >= counted_from
    annualized_from = len(known) if newly_paying and known else None
    annual_cash = total / len(known) * f if annualized_from else total
    estimate_basis, estimate_unavailable = _estimate_basis(
        past, latest_final, frequency, months
    )
    annual = Decimal(estimate_basis["amount_per_share"]) * f if estimate_basis else None
    price = Decimal(close) if close is not None else None
    overdue = bool(
        by_count
        and past
        and _days_between(past[-1]["ex_dividend_date"], as_of)
        > CYCLE_DAYS[frequency][1]
    )
    if by_count and past and months:
        last = date.fromisoformat(past[-1]["ex_dividend_date"])
        if last.month in months:
            next_month = months[1] if last.month == months[0] else months[0]
            distance = (next_month - last.month) % 12
            overdue = date.fromisoformat(as_of) > add_months(
                last, distance
            ) + timedelta(days=20)
    return {
        "cash_per_share": total,
        # The cash a full year is expected to pay: the actual sum, or the
        # average × N for a fund that started paying within the range.
        "annual_cash_per_share": annual_cash,
        "annualized_from": annualized_from,
        "short_count": len(rows) if short and not newly_paying else None,
        "frequency_changed": frequency_changed,
        "no_payment_in_range": bool(f) and not in_range,
        "paid_before_range": bool(past) and past[0]["ex_dividend_date"] < counted_from,
        "window_start": counted_from,
        "known_yield": annual_cash / price * 100 if price and price > 0 else None,
        "estimated_annual_cash": annual,
        "estimated_yield": annual / price * 100
        if annual is not None and price and price > 0
        else None,
        "estimate_basis": estimate_basis,
        "estimate_periods": f if estimate_basis else None,
        "estimate_unavailable": estimate_unavailable,
        "frequency": frequency,
        "start": start,
        "end": as_of,
        # An unknown amount is "pending", not an estimate already counted.
        "provisional": any(r["amount_status"] != "final" for r in known),
        "observed_count": len(known),
        "basis": "by_count" if by_count else "by_window",
        "expected_count": f if by_count else None,
        "selected": rows,
        "missing_amount_count": len(rows) - len(known),
        "overdue": overdue,
        "extra_distribution": extra_distribution,
        "needs_review_count": sum(
            not countable(r) and r["ex_dividend_date"] >= min(start, counted_from)
            for r in earned
        ),
        # First calendar year whose sync must be confirmed before the
        # selected events can be trusted as the latest ones (§4.3).
        # With all N found, older years in the range cannot change the
        # result; with fewer, the whole range must be known not to hide one.
        "required_from_year": int(
            (
                rows[0]["ex_dividend_date"]
                if by_count and len(rows) == f
                else counted_from
            )[:4]
        ),
    }
