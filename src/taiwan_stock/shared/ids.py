"""Stable identifier generation for business rows.

Design 002 §4.6.2 uses opaque TEXT primary keys (`ID`) for every table
instead of autoincrement integers, so ids stay stable across imports,
merges and deletions.
"""

import uuid


def new_id() -> str:
    return uuid.uuid4().hex
