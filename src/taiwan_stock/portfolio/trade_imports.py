"""Keeps a CSV upload between its preview and confirmation (design 033 §4.5).

The decoded upload is stored under `<data_dir>/imports/<preview_id>.csv`
rather than echoed through the browser, so the confirm step re-plans
from exactly what was previewed. A database backup is taken right
before any confirmed write: trades cannot be deleted, so restoring it is
the way back from a mistaken import.
"""

import re
import sqlite3
import time
from pathlib import Path

from taiwan_stock.infrastructure import backup
from taiwan_stock.portfolio import csv_trades
from taiwan_stock.shared.ids import new_id

PREVIEW_TTL_SECONDS = 24 * 60 * 60
_PREVIEW_ID = re.compile(r"[0-9a-f]{32}")


class PreviewNotFoundError(Exception):
    def __init__(self):
        super().__init__(
            "找不到這次匯入的預覽，可能已確認、取消或超過 24 小時，請重新上傳"
        )


def create_preview(
    conn: sqlite3.Connection, data_dir: Path, content: str, filename: str | None = None
) -> tuple[str | None, csv_trades.ImportPlan]:
    """Raises `csv_trades.ImportError_` before anything is stored. Only a
    plan with something to write is kept; there is nothing to confirm
    otherwise, so the returned preview_id is None."""
    plan = csv_trades.preview_import(conn, content)
    _purge_expired(data_dir)
    if not plan.has_writes:
        return None, plan
    preview_id = new_id()
    _path(data_dir, preview_id).write_text(content, encoding="utf-8")
    # The upload's name only labels the backup taken on confirmation.
    _name_path(data_dir, preview_id).write_text(filename or "", encoding="utf-8")
    return preview_id, plan


def refresh_preview(
    conn: sqlite3.Connection, data_dir: Path, preview_id: str
) -> csv_trades.ImportPlan:
    """Re-previews a stored upload against the current database, for a
    confirmation rejected as stale."""
    return csv_trades.preview_import(conn, _load(data_dir, preview_id))


def confirm_preview(
    conn: sqlite3.Connection,
    data_dir: Path,
    preview_id: str,
    digest: str,
    include_rows,
) -> tuple[csv_trades.ImportResult, str | None]:
    """Returns the result and the backup's filename, None when nothing
    was written. The read-only digest check up front avoids a pointless
    backup for a stale preview; `confirm_import` checks again under the
    write lock."""
    content = _load(data_dir, preview_id)
    plan = csv_trades.plan_import(conn, content)
    if plan.digest != digest:
        raise csv_trades.StalePreviewError()
    created = plan.count(csv_trades.CREATE) + len(
        csv_trades.checked_duplicates(plan, include_rows)
    )
    updated = plan.count(csv_trades.UPDATE)
    backup_name = None
    if created or updated:
        name_path = _name_path(data_dir, preview_id)
        filename = name_path.read_text(encoding="utf-8") if name_path.exists() else ""
        note = f"匯入 {filename or 'CSV'} 前（將新增 {created}、修改 {updated}）"
        backup_name = backup.create_backup(data_dir, backup.PRE_IMPORT, note).name
    result = csv_trades.confirm_import(conn, content, digest, include_rows)
    discard_preview(data_dir, preview_id)
    return result, backup_name


def discard_preview(data_dir: Path, preview_id: str) -> None:
    if _PREVIEW_ID.fullmatch(preview_id or ""):
        _path(data_dir, preview_id).unlink(missing_ok=True)
        _name_path(data_dir, preview_id).unlink(missing_ok=True)


def _load(data_dir: Path, preview_id: str) -> str:
    if not _PREVIEW_ID.fullmatch(preview_id or ""):
        raise PreviewNotFoundError()
    path = _path(data_dir, preview_id)
    if not path.exists() or _expired(path):
        path.unlink(missing_ok=True)
        raise PreviewNotFoundError()
    return path.read_text(encoding="utf-8")


def _purge_expired(data_dir: Path) -> None:
    for path in _directory(data_dir).glob("*.csv"):
        if _expired(path):
            path.unlink(missing_ok=True)
            path.with_suffix(".name").unlink(missing_ok=True)


def _expired(path: Path) -> bool:
    return time.time() - path.stat().st_mtime > PREVIEW_TTL_SECONDS


def _path(data_dir: Path, preview_id: str) -> Path:
    return _directory(data_dir) / f"{preview_id}.csv"


def _name_path(data_dir: Path, preview_id: str) -> Path:
    return _directory(data_dir) / f"{preview_id}.name"


def _directory(data_dir: Path) -> Path:
    path = data_dir / "imports"
    path.mkdir(parents=True, exist_ok=True)
    return path
