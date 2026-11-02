"""Standard CSV import/export for trades (design 002 §4.12.1, 033).

Import is preview-then-confirm. Each row is classified against the
database — a `trade_id` from an export updates or skips its trade, a
row without one is created, unless it is identical to a stored trade
(a suspected duplicate, skipped unless the user opts in) — so feeding
an export back in changes nothing. The whole batch is validated in one
transaction, oversell included, and rolls back entirely on any bad row;
a failed row is reported with its line number. Uploads are kept between
the two steps by `trade_imports`.
"""

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.portfolio import replay
from taiwan_stock.portfolio import trades as trades_repo

REQUIRED_COLUMNS = (
    "broker_account_name",
    "security_code",
    "market",
    "event_type",
    "trade_date",
    "quantity",
)
OPTIONAL_COLUMNS = (
    "broker_account_id",
    "unit_price",
    "executed_amount",
    "commission",
    "transaction_tax",
    "other_fees",
    "is_day_trade",
    "note",
    "trade_id",
)
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
MONEY_FIELDS = (
    "unit_price",
    "executed_amount",
    "commission",
    "transaction_tax",
    "other_fees",
)
COMPARED_FIELDS = (
    "event_type",
    "trade_date",
    "quantity",
    *MONEY_FIELDS,
    "is_day_trade",
    "note",
)

CREATE, UPDATE, UNCHANGED, DUPLICATE = "create", "update", "unchanged", "duplicate"
_TRADE_ID = re.compile(r"[0-9a-f]{32}")

MAX_ROWS = 1000
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

# Traditional-Chinese Windows Excel decodes a BOM-less CSV as cp950, so
# exports carry one; imports strip it so the first header still matches.
BOM = "\ufeff"

# Spreadsheets run a cell starting with these as a formula (#115), so
# exported free text gets a leading `'`, which imports remove again.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_ESCAPED_COLUMNS = ("broker_account_name", "note")


def _escape_formula(value):
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _unescape_formula(value):
    if (
        isinstance(value, str)
        and value.startswith("'")
        and value[1:].startswith(_FORMULA_PREFIXES)
    ):
        return value[1:]
    return value


class ImportError_(Exception):  # noqa: N818 - `ImportError` shadows a builtin
    def __init__(self, row_number: int, message: str):
        self.row_number = row_number
        self.message = message
        prefix = "標頭" if row_number == 0 else f"第 {row_number} 列"
        super().__init__(f"{prefix}：{message}")


class StalePreviewError(Exception):
    """The database changed between the preview and its confirmation."""

    def __init__(self):
        super().__init__("預覽後交易資料已變更，請重新預覽後再確認")


@dataclass
class PlannedRow:
    row_number: int
    status: str
    trade_id: str | None
    values: dict
    changes: dict[str, tuple]
    security_code: str
    broker_account_name: str


@dataclass
class ImportPlan:
    rows: list[PlannedRow]
    digest: str

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row.status == status)

    @property
    def summary(self) -> dict:
        return {
            "created": self.count(CREATE),
            "updated": self.count(UPDATE),
            "unchanged": self.count(UNCHANGED),
            "duplicates": self.count(DUPLICATE),
        }

    @property
    def has_writes(self) -> bool:
        return any(row.status in (CREATE, UPDATE, DUPLICATE) for row in self.rows)


@dataclass
class ImportResult:
    created: int
    updated: int


class _DryRun(Exception):  # noqa: N818 - control flow, never surfaces
    def __init__(self, plan: ImportPlan):
        self.plan = plan


def read_upload_text(stream) -> str:
    raw = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImportError_(
            0,
            f"上傳檔案超過大小上限（{MAX_UPLOAD_BYTES // (1024 * 1024)} MB），請先切檔",
        )
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_(0, "檔案無法以 UTF-8 或 Big5 解碼，請確認上傳的是純文字 CSV 檔")


def parse_csv(content: str) -> list[dict]:
    content = content.removeprefix(BOM)
    if "\x00" in content:
        raise ImportError_(
            0, "CSV 內容包含無效的二進位字元（NUL），請確認上傳的是純文字 CSV 檔"
        )
    try:
        reader = csv.DictReader(io.StringIO(content))
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise ImportError_(0, f"缺少必要欄位：{', '.join(missing)}")
        rows = list(reader)
    except csv.Error as exc:
        raise ImportError_(0, f"CSV 格式無法解析：{exc}") from exc
    if len(rows) > MAX_ROWS:
        raise ImportError_(
            0, f"單次匯入最多 {MAX_ROWS} 列，這份檔案有 {len(rows)} 列，請先切檔"
        )
    for row in rows:
        for col in _ESCAPED_COLUMNS:
            if col in row:
                row[col] = _unescape_formula(row[col])
    return rows


def plan_import(conn: sqlite3.Connection, content: str) -> ImportPlan:
    """Read-only classification, without the history replay."""
    return _plan(conn, parse_csv(content))


def preview_import(conn: sqlite3.Connection, content: str) -> ImportPlan:
    """Classifies every row and really applies the batch inside a
    transaction that is then rolled back, so fee defaults and oversell
    errors in the preview come from the same code as the real write.
    """
    rows = parse_csv(content)
    try:
        with write_transaction(conn):
            plan = _plan(conn, rows)
            _apply(conn, plan, ())
            raise _DryRun(plan)
    except _DryRun as dry_run:
        return dry_run.plan


def confirm_import(
    conn: sqlite3.Connection, content: str, digest: str, include_rows
) -> ImportResult:
    """Re-plans under the write lock; a digest differing from the one the
    user saw means the database moved on, so nothing stale is applied."""
    rows = parse_csv(content)
    with write_transaction(conn):
        plan = _plan(conn, rows)
        if plan.digest != digest:
            raise StalePreviewError()
        return _apply(conn, plan, include_rows)


def import_trades(conn: sqlite3.Connection, rows: list[dict], include_rows=()) -> int:
    """Plan and apply in one step, for callers without a preview."""
    with write_transaction(conn):
        return _apply(conn, _plan(conn, rows), include_rows).created


def _plan(conn: sqlite3.Connection, rows: list[dict]) -> ImportPlan:
    """Row numbers start at 2 (row 1 is the header) to match what the
    user sees when they open the file."""
    planned = []
    seen_ids: dict[str, int] = {}
    duplicate_pools: dict[tuple, Counter] = {}
    claimed_ids = {
        (row.get("trade_id") or "").strip()
        for row in rows
        if (row.get("trade_id") or "").strip()
    }
    for offset, row in enumerate(rows):
        row_number = offset + 2
        candidate = _resolve_row(conn, row_number, row)
        try:
            values = trades_repo.prepare(conn, candidate)
        except trades_repo.TradeValidationError as exc:
            raise ImportError_(row_number, str(exc)) from exc
        labels = conn.execute(
            "SELECT b.name AS broker_account_name, s.code AS security_code "
            "FROM broker_accounts b, securities s WHERE b.id = ? AND s.id = ?",
            (values["broker_account_id"], values["security_id"]),
        ).fetchone()
        trade_id = (row.get("trade_id") or "").strip() or None

        changes: dict[str, tuple] = {}
        current = None
        if trade_id is not None:
            if trade_id in seen_ids:
                raise ImportError_(
                    row_number,
                    f"trade_id「{trade_id}」與第 {seen_ids[trade_id]} 列重複",
                )
            seen_ids[trade_id] = row_number
            current = conn.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
        if current is not None:
            if current["voided_at"] is not None:
                raise ImportError_(row_number, trades_repo.VOIDED_EDIT_MESSAGE)
            if (current["broker_account_id"], current["security_id"]) != (
                values["broker_account_id"],
                values["security_id"],
            ):
                raise ImportError_(
                    row_number, "不能透過 CSV 變更既有交易的券商帳戶或商品"
                )
            for field in COMPARED_FIELDS:
                if _same(field, current[field], values[field]):
                    # Keep the stored spelling (`69900.0`) of an equal value.
                    values[field] = current[field]
                else:
                    changes[field] = (current[field], values[field])
            status = UPDATE if changes else UNCHANGED
        else:
            # An unknown trade_id comes from another database (or a copy of
            # this one): the row is new here. A well-formed ID is kept so the
            # trade stays matchable between the two databases afterwards.
            if trade_id is not None and _TRADE_ID.fullmatch(trade_id):
                values["id"] = trade_id
            trade_id = None
            pair = (values["broker_account_id"], values["security_id"])
            if pair not in duplicate_pools:
                duplicate_pools[pair] = Counter(
                    _duplicate_key(t)
                    for t in trades_repo.fetch_trades(conn, *pair)
                    if t["id"] not in claimed_ids
                )
            pool, key = duplicate_pools[pair], _duplicate_key(values)
            # Matched by count: one stored trade absorbs one identical row,
            # so two genuinely identical orders still import the second.
            if pool[key] > 0:
                pool[key] -= 1
                status = DUPLICATE
            else:
                status = CREATE
        planned.append(
            PlannedRow(
                row_number=row_number,
                status=status,
                trade_id=trade_id,
                values=values,
                changes=changes,
                security_code=labels["security_code"],
                broker_account_name=labels["broker_account_name"],
            )
        )
    return ImportPlan(rows=planned, digest=_digest(planned))


def checked_duplicates(plan: ImportPlan, include_rows) -> set[int]:
    """The opted-in row numbers, which must all be suspected duplicates."""
    include = {int(n) for n in include_rows}
    duplicates = {r.row_number for r in plan.rows if r.status == DUPLICATE}
    if include - duplicates:
        raise ImportError_(min(include - duplicates), "只能勾選疑似重複的列")
    return include


def _apply(conn: sqlite3.Connection, plan: ImportPlan, include_rows) -> ImportResult:
    include = checked_duplicates(plan, include_rows)
    creates = [
        r
        for r in plan.rows
        if r.status == CREATE or (r.status == DUPLICATE and r.row_number in include)
    ]
    updates = [r for r in plan.rows if r.status == UPDATE]
    new_values = [
        {**r.values, "id": r.values.get("id") or trades_repo.new_id()} for r in creates
    ]
    rows_by_trade = {v["id"]: r.row_number for v, r in zip(new_values, creates)}
    rows_by_trade.update({r.trade_id: r.row_number for r in updates})
    try:
        trades_repo.apply_batch(
            conn, new_values, {r.trade_id: r.values for r in updates}
        )
    except replay.OversellError as exc:
        if exc.trade_id in rows_by_trade:
            raise ImportError_(rows_by_trade[exc.trade_id], str(exc)) from exc
        # A stored sell broke because an imported row lowered its buys.
        sell = conn.execute(
            "SELECT broker_account_id, security_id FROM trades WHERE id = ?",
            (exc.trade_id,),
        ).fetchone()
        related = sorted(
            r.row_number
            for r in [*creates, *updates]
            if (r.values["broker_account_id"], r.values["security_id"])
            == (sell["broker_account_id"], sell["security_id"])
        )
        raise ImportError_(
            related[0],
            f"{exc}（受本次匯入影響的列：{'、'.join(map(str, related))}）",
        ) from exc
    return ImportResult(created=len(creates), updated=len(updates))


def _same(field: str, stored, imported) -> bool:
    if field in MONEY_FIELDS:
        if stored is None or imported is None:
            return stored is None and imported is None
        return Decimal(stored) == Decimal(imported)
    if field == "is_day_trade":
        return bool(stored) == bool(imported)
    if field == "quantity":
        return int(stored) == int(imported)
    return stored == imported


def _duplicate_key(trade) -> tuple:
    return (
        trade["event_type"],
        trade["trade_date"],
        int(trade["quantity"]),
        *(None if trade[f] is None else Decimal(trade[f]) for f in MONEY_FIELDS),
        bool(trade["is_day_trade"]),
        trade["note"],
    )


def _digest(planned: list[PlannedRow]) -> str:
    payload = [
        [row.row_number, row.status, row.trade_id, row.values, row.changes]
        for row in planned
    ]
    encoded = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _resolve_row(conn: sqlite3.Connection, row_number: int, row: dict) -> dict:
    """The account ID wins (renamed and same-named accounts stay apart);
    an ID this database doesn't know comes from another database, so the
    name is tried instead — the preview shows the account it landed on."""
    account_id = (row.get("broker_account_id") or "").strip()
    accounts = []
    if account_id:
        accounts = conn.execute(
            "SELECT id FROM broker_accounts WHERE id = ?", (account_id,)
        ).fetchall()
    if not accounts:
        accounts = conn.execute(
            "SELECT id FROM broker_accounts WHERE name = ?",
            (row.get("broker_account_name"),),
        ).fetchall()
        if not accounts:
            raise ImportError_(
                row_number, f"找不到券商帳戶「{row.get('broker_account_name')}」"
            )
        if len(accounts) > 1:
            raise ImportError_(
                row_number,
                f"有多個名為「{row.get('broker_account_name')}」的券商帳戶，"
                "請先改名讓帳戶名稱不重複",
            )
    account = accounts[0]

    security = _resolve_security(
        conn, row_number, row.get("market"), row.get("security_code")
    )

    try:
        quantity = int(row["quantity"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ImportError_(
            row_number, f"股數「{row.get('quantity')}」不是整數"
        ) from exc

    if row.get("event_type") not in ("buy", "sell", "opening"):
        raise ImportError_(
            row_number, f"買賣類型「{row.get('event_type')}」必須是 buy／sell／opening"
        )

    return {
        "broker_account_id": account["id"],
        "security_id": security["id"],
        "event_type": row["event_type"],
        "trade_date": _normalize_date(row.get("trade_date")),
        "quantity": quantity,
        "unit_price": row.get("unit_price") or None,
        "executed_amount": row.get("executed_amount") or None,
        "commission": row.get("commission") or None,
        "transaction_tax": row.get("transaction_tax") or None,
        "other_fees": row.get("other_fees") or None,
        "is_day_trade": row.get("is_day_trade") or None,
        "note": row.get("note") or None,
    }


def _normalize_date(value):
    """Excel re-saves `2021-12-02` as `2021-12-2` or `2021/12/2`, so
    year-first dates get zero-padded here; trades.create_trade still
    rejects impossible ones. Day-first/month-first forms are ambiguous and
    are left as-is to fail validation.
    """
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", (value or "").strip())
    if match is None:
        return value
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _resolve_security(
    conn: sqlite3.Connection, row_number: int, market, code
) -> sqlite3.Row:
    """Falls back to a leading-zero-insensitive match when the exact code
    is missing, because Excel turns `00752` into `752` on save. Only an
    unambiguous match is accepted — `6208` could be either `6208` or a
    mangled `006208` — and a code that still has its leading zero was
    evidently not mangled, so it never falls back.
    """
    security = conn.execute(
        "SELECT id FROM securities WHERE market = ? AND code = ?", (market, code)
    ).fetchone()
    if security is not None:
        return security
    if code and code.isdigit() and not code.startswith("0"):
        candidates = conn.execute(
            "SELECT id, code FROM securities "
            "WHERE market = ? AND ltrim(code, '0') = ? ORDER BY code",
            (market, code),
        ).fetchall()
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            options = "、".join(c["code"] for c in candidates)
            raise ImportError_(
                row_number,
                f"商品代碼「{code}」可能是 {options}（Excel 可能吃掉了前導零），"
                "請填入完整代碼",
            )
    raise ImportError_(row_number, f"找不到商品「{market} {code}」")


def export_trades_csv(conn: sqlite3.Connection, filters: dict | None = None) -> str:
    """Same column shape as import; `trade_id` lets a re-import of a
    spreadsheet-edited export update these trades instead of adding."""
    from taiwan_stock.portfolio.trade_query import where_clause

    where, values = where_clause(filters or {})
    rows = conn.execute(
        """
        SELECT b.name AS broker_account_name, t.broker_account_id,
               s.code AS security_code,
               s.market, t.event_type, t.trade_date, t.quantity,
               t.unit_price, t.executed_amount, t.commission,
               t.transaction_tax, t.other_fees, t.is_day_trade, t.note,
               t.id AS trade_id
        FROM live_trades t
        JOIN broker_accounts b ON b.id = t.broker_account_id
        JOIN securities s ON s.id = t.security_id
        """
        + where
        + " ORDER BY t.trade_date, t.created_at, t.trade_order",
        values,
    ).fetchall()

    buffer = io.StringIO()
    buffer.write(BOM)
    writer = csv.DictWriter(buffer, fieldnames=ALL_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                col: _escape_formula(row[col]) if col in _ESCAPED_COLUMNS else row[col]
                for col in ALL_COLUMNS
            }
        )
    return buffer.getvalue()
