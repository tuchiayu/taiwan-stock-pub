"""Bounded trade browsing; full replay and CSV import retain their own contracts."""

from datetime import date

from taiwan_stock.portfolio.trades import _trade_total_amount

FILTER_KEYS = (
    "q",
    "broker_account_id",
    "date_from",
    "date_to",
    "event_type",
    "include_voided",
)
JOIN = (
    "FROM trades t JOIN broker_accounts b ON b.id=t.broker_account_id "
    "JOIN securities s ON s.id=t.security_id"
)


def parse_filters(conn, params):
    state = {key: str(params.get(key, "")).strip() for key in FILTER_KEYS}
    errors = {}
    for key in ("date_from", "date_to"):
        if state[key]:
            try:
                date.fromisoformat(state[key])
                if len(state[key]) != 10:
                    raise ValueError
            except ValueError:
                errors[key] = "請輸入有效日期（YYYY-MM-DD）。"
    if (
        not errors
        and state["date_from"]
        and state["date_to"]
        and state["date_from"] > state["date_to"]
    ):
        errors["date_to"] = "結束日期不可早於開始日期。"
    if state["event_type"] not in ("", "buy", "sell", "opening"):
        errors["event_type"] = "請選擇有效的交易類型。"
    if state["include_voided"] not in ("", "1"):
        state["include_voided"] = ""
    if (
        state["broker_account_id"]
        and not conn.execute(
            "SELECT 1 FROM broker_accounts WHERE id=?", (state["broker_account_id"],)
        ).fetchone()
    ):
        errors["broker_account_id"] = "找不到此券商帳戶，請重新選擇。"
    return state, errors


def where_clause(state):
    clauses, values = [], []
    if state.get("q"):
        clauses.append("(s.code LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\')")
        query = state["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        values.extend([f"%{query}%"] * 2)
    for key, sql in (
        ("broker_account_id", "t.broker_account_id=?"),
        ("date_from", "t.trade_date>=?"),
        ("date_to", "t.trade_date<=?"),
        ("event_type", "t.event_type=?"),
    ):
        if state.get(key):
            clauses.append(sql)
            values.append(state[key])
    if not state.get("include_voided"):
        clauses.append("t.voided_at IS NULL")
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), values


def browse(conn, state, page=1, page_size=50):
    def positive(value, default):
        try:
            return max(1, int(value))
        except (ValueError, TypeError):
            return default

    size = min(100, positive(page_size, 50))
    where, values = where_clause(state)
    total = conn.execute(f"SELECT count(*) {JOIN}{where}", values).fetchone()[0]
    pages = max(1, (total + size - 1) // size)
    page = min(pages, positive(page, 1))
    rows = conn.execute(
        "SELECT t.*, b.name AS broker_account_name, s.code AS security_code, "
        f"s.name AS security_name {JOIN}{where} "
        "ORDER BY t.trade_date DESC,t.created_at DESC,t.trade_order DESC "
        "LIMIT ? OFFSET ?",
        (*values, size, (page - 1) * size),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        total_amount = _trade_total_amount(item)
        item["total_amount"] = str(total_amount) if total_amount is not None else None
        items.append(item)
    return {
        "transactions": items,
        "total_results": total,
        "page": page,
        "page_size": size,
        "total_pages": pages,
    }
