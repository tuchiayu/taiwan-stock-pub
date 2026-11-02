"""Securities search over the synced catalog (design 002 §4.3.2 商品查詢).

Reads only `securities`/`etf_profiles`, which are synced in full ahead
of time (design 002 §4.7) — this never calls out to TWSE/TPEx itself.
"""

import sqlite3

from taiwan_stock.catalog import delistings
from taiwan_stock.catalog.distribution_rows import COUNTABLE_SQL

MAX_LIMIT = 100
DEFAULT_LIMIT = 50


def search_securities(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    security_type: str | None = None,
    market: str | None = None,
    industry: str | None = None,
    custodian_bank: str | None = None,
    distribution_frequency: str | None = None,
    pay_month: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """`custodian_bank`/`distribution_frequency` are ETF-only fields on
    `etf_profiles`; passing either narrows the match to ETFs that have
    a matching profile row, combined with an AND like the other filters
    (design 002 §4.3.2's ETF 組合篩選). Both match the same
    user-override → official → (frequency only) inferred priority the
    detail/search-result display uses (design 004 §4.4.1/§4.5.1) —
    `json_extract` reads `distribution_frequency_inferred`'s `value`
    key without needing a copy of it in its own column.

    `pay_month` is `YYYY-MM` and matches securities with at least one
    `distributions.pay_date` announced in that month — design 004
    §4.7.3: "指定年月的已公告發放事件", not a recurring/estimated
    schedule. The caller must validate the format; this function only
    matches it literally against the stored `YYYY-MM-DD` prefix.
    """
    rows = candidate_securities(
        conn,
        query=query,
        security_type=security_type,
        market=market,
        industry=industry,
        pay_month=pay_month,
    )
    if custodian_bank or distribution_frequency:
        from taiwan_stock.catalog import profile_values

        profiles = {
            r["security_id"]: dict(r)
            for r in conn.execute("SELECT * FROM etf_profiles")
        }

        def matches(row):
            profile = profiles.get(row["id"], {})
            bank = profile_values.custodian_bank(profile)["value"]
            freq = profile_values.distribution_frequency(profile)["value"]
            return (
                not custodian_bank
                or (
                    bank is None
                    if custodian_bank == "unknown"
                    else bank == profile_values.canonical_name(custodian_bank)
                )
            ) and (
                not distribution_frequency
                or (
                    freq is None
                    if distribution_frequency == "unknown"
                    else freq == distribution_frequency
                )
            )

        rows = [r for r in rows if matches(r)]
    offset = max(0, offset)
    return rows[offset : offset + max(1, min(limit, MAX_LIMIT))]


def candidate_securities(
    conn,
    *,
    query=None,
    security_type=None,
    market=None,
    industry=None,
    pay_month=None,
    tracked_only=False,
):
    """Internal full candidate set; public search keeps its limit contract."""
    delistings.refresh_status(conn)
    clauses = []
    params: list[str] = []
    if query:
        clauses.append("(code LIKE ? OR name LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if security_type:
        clauses.append("security_type = ?")
        params.append(security_type)
    if market:
        clauses.append("market = ?")
        params.append(market)
    if industry:
        clauses.append("industry = ?")
        params.append(industry)
    if tracked_only:
        clauses.append("id IN (SELECT security_id FROM watchlist)")
    if pay_month:
        clauses.append(
            "id IN (SELECT security_id FROM distributions WHERE "
            f"substr(pay_date,1,7)=? AND {COUNTABLE_SQL})"
        )
        params.append(pay_month)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return conn.execute(
        f"""
        SELECT securities.*, EXISTS(SELECT 1 FROM distributions d WHERE
        d.security_id=securities.id) AS has_events FROM securities {where}
        ORDER BY code
        """,
        params,
    ).fetchall()


def list_custodian_banks(conn: sqlite3.Connection) -> list[str]:
    from taiwan_stock.catalog.profile_values import custodian_bank

    return sorted(
        {
            value
            for row in conn.execute("SELECT * FROM etf_profiles")
            if (value := custodian_bank(dict(row))["value"])
        }
    )


def list_industries(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT industry FROM securities "
            "WHERE industry IS NOT NULL ORDER BY industry"
        )
    ]
