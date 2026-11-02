"""Foreign-currency share classes follow their TWD class (design 043).

A "+U"/"+R" line (e.g. 00636K) is the same fund as its TWD class (00636):
one trust deed, one custodian, one distribution policy. Issuers list only
the TWD class, so the share class takes the fund-level fields from it.

The fund tax id alone is not proof: 0080 and 0081 are different funds that
share their offshore agent's id. A pair also needs TWSE's own fund type to
mark exactly one side as the foreign-currency listing, and the same index.
"""

import json
import re
import unicodedata
from collections import defaultdict

from taiwan_stock.shared.dates import now_utc_iso

FOREIGN_CURRENCY_MARK = "加掛外幣"
# "國泰中國A50+U", "群益深証中小+R", "國泰20年美債+櫃U" (TPEx has no fund type).
_FOREIGN_NAME = re.compile(r"\+櫃?[A-Z]$")


def is_foreign_currency(fund_type, name):
    """A listing quoted in a foreign currency (design 063)."""
    return FOREIGN_CURRENCY_MARK in (fund_type or "") or bool(
        _FOREIGN_NAME.search((name or "").strip())
    )


FIELDS = (
    "custodian_bank",
    "distribution_frequency",
    "distribution_policy",
    "distribution_ex_dividend_schedule",
)
# Columns with a provenance JSON of their own.
SOURCED = ("custodian_bank", "distribution_frequency", "distribution_policy")


def _index(benchmark):
    return "".join(unicodedata.normalize("NFKC", benchmark or "").split())


def pairs(conn):
    """{share class security_id: TWD class row} for unambiguous pairs only."""
    groups = defaultdict(list)
    for row in conn.execute(
        "SELECT s.id,s.code,s.status,p.fund_tax_id,p.fund_type,p.benchmark "
        "FROM securities s JOIN etf_profiles p ON p.security_id=s.id "
        "WHERE s.security_type='etf' AND p.fund_tax_id IS NOT NULL "
        "AND p.fund_type IS NOT NULL"
    ):
        groups[row["fund_tax_id"]].append(row)
    result = {}
    for rows in groups.values():
        for child in rows:
            if (
                child["status"] != "active"
                or FOREIGN_CURRENCY_MARK not in child["fund_type"]
                or not _index(child["benchmark"])
            ):
                continue
            bases = [
                row
                for row in rows
                if FOREIGN_CURRENCY_MARK not in row["fund_type"]
                and _index(row["benchmark"]) == _index(child["benchmark"])
            ]
            if len(bases) == 1 and bases[0]["status"] == "active":
                result[child["id"]] = bases[0]
    return result


def inherit(conn, child_id, base):
    """Copy the TWD class's fund-level fields, NULLs included, so a value the
    share class picked up elsewhere (00625K got 00717's page) is replaced.
    Returns the fields that now hold a value."""
    current = conn.execute(
        "SELECT " + ",".join(FIELDS + tuple(f + "_source" for f in SOURCED)) + " "
        "FROM etf_profiles WHERE security_id=?",
        (base["id"],),
    ).fetchone()
    stamp = now_utc_iso()
    values = {field: current[field] for field in FIELDS}
    for field in SOURCED:
        upstream = json.loads(current[field + "_source"] or "null")
        values[field + "_source"] = (
            json.dumps(
                {
                    "origin": "share_class",
                    "from_code": base["code"],
                    "fund_tax_id": base["fund_tax_id"],
                    "fetched_at": stamp,
                    "url": upstream.get("url") if isinstance(upstream, dict) else None,
                    "raw": upstream.get("raw") if isinstance(upstream, dict) else None,
                },
                ensure_ascii=False,
            )
            if current[field]
            else None
        )
    conn.execute(
        "UPDATE etf_profiles SET "
        + ",".join(column + "=?" for column in values)
        + ",distribution_month_pattern=NULL WHERE security_id=?",
        (*values.values(), child_id),
    )
    return [field for field in SOURCED if current[field]]
