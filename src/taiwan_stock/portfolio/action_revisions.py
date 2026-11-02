"""Immutable official corrections; current conflicts are always replayed afresh."""

import hashlib
import json

from taiwan_stock.portfolio import replay
from taiwan_stock.shared import sync_progress
from taiwan_stock.shared.dates import now_utc_iso
from taiwan_stock.shared.ids import new_id


def replay_impacts(conn, security_id, before_actions, after_actions):
    accounts = {}
    for trade in conn.execute(
        "SELECT * FROM live_trades WHERE security_id=?", (security_id,)
    ):
        accounts.setdefault(trade["broker_account_id"], []).append(trade)
    impacts = []
    for account, history in sorted(accounts.items()):
        as_of = max(t["trade_date"] for t in history)
        before, before_quantity = _replay_state(history, before_actions, as_of)
        after, after_quantity = _replay_state(history, after_actions, as_of)
        impacts.append(
            {
                "broker_account_id": account,
                "security_id": security_id,
                "as_of_date": as_of,
                "before": _conflict(before),
                "after": _conflict(after),
                "before_quantity": before_quantity,
                "after_quantity": after_quantity,
            }
        )
    return impacts


def _replay_state(history, actions, as_of):
    try:
        position = replay.replay_trades(history, actions, as_of_date=as_of)
        return None, str(position.remaining_quantity)
    except replay.OversellError as exc:
        return exc, None


def _conflict(error):
    if error is None:
        return None
    return {
        "trade_id": error.trade_id,
        "shortfall": str(error.shortfall),
        "requested": str(error.requested),
        "detail": str(error),
    }


def record(conn, before, after, impacts, *, source, source_url=None):
    def encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    payload = [encode(dict(before)), encode(dict(after)), encode(impacts)]
    progress = sync_progress.current.get()
    conn.execute(
        "INSERT INTO corporate_action_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            new_id(),
            after["id"],
            after["security_id"],
            now_utc_iso(),
            source,
            source_url,
            progress.job_id if progress else None,
            *payload,
            hashlib.sha256("\n".join(payload).encode()).hexdigest(),
        ),
    )


def list_revisions(conn, *, security_id=None):
    rows = conn.execute(
        "SELECT * FROM corporate_action_revisions "
        + ("WHERE security_id=? " if security_id else "")
        + "ORDER BY changed_at DESC, rowid DESC",
        (security_id,) if security_id else (),
    ).fetchall()
    return [
        {
            **dict(row),
            "before": json.loads(row["before_json"]),
            "after": json.loads(row["after_json"]),
            "impacts": json.loads(row["impact_json"]),
        }
        for row in rows
    ]
