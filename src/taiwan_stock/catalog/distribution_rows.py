"""Which stored distributions count (design 058 §4.2.1).

A row whose ex-dividend date vanished on resync stays stored and flagged
`needs_review` for a person to resolve (design 009 §4.4); until then it is
shown but never counted, so a moved date is not paid twice.
"""

NEEDS_REVIEW = "needs_review"

COUNTABLE_SQL = (
    f"(reconciliation_status IS NULL OR reconciliation_status != '{NEEDS_REVIEW}')"
)


def countable(row) -> bool:
    """`row` is a dict, an `sqlite3.Row` or an entitlement entry."""
    key = "reconciliation_status"
    if hasattr(row, "keys"):
        status = row[key] if key in row.keys() else None
    else:
        status = getattr(row, key, None)
    return status != NEEDS_REVIEW
