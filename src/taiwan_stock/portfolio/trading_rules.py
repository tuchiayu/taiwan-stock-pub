"""Product identity and enacted date rules, shared by saves and estimates.

Sources: Securities Transaction Tax Act §2-1 (G0340078), TWSE ETF trading
rules, TPEx ETF categories. Draft extensions are deliberately not applied.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import pricing

ETF_TICK_SCHEDULE = ((Decimal("50"), Decimal("0.01")), (None, Decimal("0.05")))


@dataclass(frozen=True)
class RulePeriod:
    """One enacted period, inclusive. A law change adds a row (design 057)."""

    category: str
    start: str
    end: str
    label: str
    source: str


TAX_EXEMPT_PERIODS = (
    RulePeriod(
        "passive-bond-etf",
        "2017-01-01",
        "2026-12-31",
        "被動式債券 ETF",
        "證券交易稅條例第 2 條之 1",
    ),
)
DAY_TRADE_PERIODS = (
    RulePeriod(
        "stock", "2017-04-28", "2027-12-31", "股票當沖", "證券交易稅條例第 2 條之 2"
    ),
)
NOTICE_BEFORE_DAYS = 60
NOTICE_AFTER_DAYS = 180


@dataclass(frozen=True)
class TradingRules:
    fees: pricing.FeeSchedule
    tick_schedule: tuple
    rule_id: str
    rule_date: str
    day_trade_eligible: bool
    warnings: tuple[str, ...]
    # Expiry reminders, kept apart from data warnings so holdings lists
    # don't repeat them for every bond ETF (design 057 §3).
    notices: tuple[str, ...] = ()


def active_period(periods, category, on_date):
    return next(
        (p for p in periods if p.category == category and p.start <= on_date <= p.end),
        None,
    )


def _shift(day, days):
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def period_notice(periods, category, on_date, benefit):
    """One sentence when `on_date` is near the end of a period with no
    successor, or shortly after it lapsed (design 057 §4.2.2)."""
    for p in periods:
        if p.category != category:
            continue
        if active_period(periods, category, _shift(p.end, 1)):
            continue
        if _shift(p.end, -NOTICE_BEFORE_DAYS) <= on_date <= p.end:
            return f"{p.label}{benefit}至 {p.end}，之後依當時已生效的法規計算。"
        if p.end < on_date <= _shift(p.end, NOTICE_AFTER_DAYS) and not active_period(
            periods, category, on_date
        ):
            return f"{p.label}{benefit}已於 {p.end} 屆滿，目前依一般稅率試算。"
    return None


def classify(security):
    """Only exact domestic ETF identities support the official suffix rule."""
    kind, market, code = (
        security["security_type"],
        security["market"],
        security["code"],
    )
    if kind == "stock" and market in ("TWSE", "TPEx"):
        return "stock"
    if kind != "etf" or market not in ("TWSE", "TPEx"):
        return "unknown"
    if re.fullmatch(r"00\d{3}[BC]", code):
        return "passive-bond-etf"
    if re.fullmatch(r"00\d{3}D", code):
        return "active-bond-etf"
    if re.fullmatch(r"(?:00\d{2,4}|00\d{3}[AKLRTU])", code):
        return "general-etf"
    return "unknown"


def resolve_trading_rules(conn, security, on_date):
    if date.fromisoformat(on_date).isoformat() != on_date:
        raise ValueError("計價日期必須為 YYYY-MM-DD")
    kind = security["security_type"]
    category = classify(security)
    fees = pricing.load_fee_schedule(conn, kind)
    warnings = []
    if category == "unknown":
        warnings.append("商品分類依據不足，採一般費率估算；請核對實際費用。")
    if kind == "etf" and fees.tax_rate == 0:
        warnings.append("一般 ETF 稅率設定為零；請確認是否為舊版債券免稅設定。")
    exempt = active_period(TAX_EXEMPT_PERIODS, category, on_date)
    if exempt:
        fees = fees._replace(tax_rate=Decimal(0))
    notices = [
        n
        for n in (
            period_notice(TAX_EXEMPT_PERIODS, category, on_date, "免證交稅"),
            period_notice(DAY_TRADE_PERIODS, category, on_date, "減稅"),
        )
        if n
    ]
    return TradingRules(
        fees=fees,
        tick_schedule=ETF_TICK_SCHEDULE
        if kind == "etf"
        else tuple(pricing.TICK_SCHEDULE),
        rule_id=f"{category}:"
        + (
            f"bond-exempt-{exempt.start[:4]}-{exempt.end[:4]}"
            if exempt
            else "general-tax"
        ),
        rule_date=on_date,
        day_trade_eligible=active_period(DAY_TRADE_PERIODS, category, on_date)
        is not None,
        warnings=tuple(warnings),
        notices=tuple(notices),
    )


def exemption_summary(on_date):
    """Settings page: periods in force today, each with any notice."""
    return [
        {
            "text": f"{p.label}：{p.start}～{p.end} 免證交稅（{p.source}）",
            "notice": period_notice(
                TAX_EXEMPT_PERIODS, p.category, on_date, "免證交稅"
            ),
        }
        for p in TAX_EXEMPT_PERIODS
        if p.start <= on_date <= p.end
        or p.end < on_date <= _shift(p.end, NOTICE_AFTER_DAYS)
    ]


def historical_tax_differences(conn):
    """Comparison only: a difference is not proof that a manual tax is wrong."""
    with read_transaction(conn):
        rows = conn.execute(
            "SELECT t.*, s.market, s.code, s.name, s.security_type "
            "FROM live_trades t JOIN securities s ON s.id=t.security_id "
            "WHERE t.event_type='sell' AND s.security_type='etf' "
            "AND t.unit_price IS NOT NULL "
            "ORDER BY t.trade_date DESC, t.trade_order DESC"
        ).fetchall()
        differences = []
        for row in rows:
            rules = resolve_trading_rules(conn, row, row["trade_date"])
            expected = rules.fees.tax(
                Decimal(row["unit_price"]), Decimal(row["quantity"])
            )
            original = (
                Decimal(row["transaction_tax"])
                if row["transaction_tax"] is not None
                else None
            )
            if original != expected:
                differences.append(
                    {
                        "trade_id": row["id"],
                        "code": row["code"],
                        "trade_date": row["trade_date"],
                        "original": original,
                        "expected": expected,
                        "difference": original - expected
                        if original is not None
                        else None,
                        "rule_id": rules.rule_id,
                    }
                )
        return differences
