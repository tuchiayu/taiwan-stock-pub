"""HTML form validation and presentation, independent of JSON trade contracts."""

from datetime import date

from taiwan_stock.portfolio import limits

LABELS = {
    "broker_account_id": "券商帳戶",
    "security_code": "商品代碼",
    "event_type": "交易類型",
    "trade_date": "日期",
    "quantity": "股數",
    "unit_price": "每股成交價",
    "executed_amount": "成交金額／期初總成本",
    "commission": "手續費",
    "transaction_tax": "證交稅",
    "other_fees": "其他費用",
}


def ungroup(values):
    """Design 078 §4.2.4: drop thousands separators from the numeric fields,
    so a form sent as `20,000` saves and redisplays as `20000`."""
    result = dict(values)
    for key in ("quantity", *limits.MONEY_FIELDS):
        if isinstance(result.get(key), str):
            result[key] = result[key].replace(",", "")
    return result


def validate(values):
    errors = {}
    if values.get("event_type") not in ("buy", "sell", "opening"):
        errors["event_type"] = "請選擇買進、賣出或期初持股。"
    try:
        date.fromisoformat(values.get("trade_date", ""))
        if len(values["trade_date"]) != 10:
            raise ValueError
    except (ValueError, TypeError):
        errors["trade_date"] = "請輸入有效日期。"
    quantity = None
    try:
        quantity = limits.check_quantity(int(values.get("quantity", "")))
    except (ValueError, TypeError):
        errors["quantity"] = f"股數必須是 1 到 {limits.MAX_QUANTITY:,} 的整數。"
    for key in limits.MONEY_FIELDS:
        if values.get(key) not in (None, ""):
            try:
                amount = limits.check_amount(
                    values[key], limits.maximum_for(key), LABELS[key]
                )
                if key == "unit_price" and quantity is not None:
                    limits.check_gross(amount, quantity)
            except ValueError as exc:
                errors[key] = str(exc)
    return errors


def domain_error(message):
    for key, label in LABELS.items():
        if key in message:
            return {key: message.replace(key, label)}
    return {"quantity": message}


def normalize_fields(values, enhanced=False):
    result = {
        key: value or None for key, value in values.items() if key != "security_code"
    }
    result["quantity"] = int(values["quantity"])
    if enhanced and result["event_type"] == "opening":
        for key in (
            "unit_price",
            "commission",
            "transaction_tax",
            "other_fees",
            "is_day_trade",
        ):
            result[key] = None
    elif enhanced and result["event_type"] == "buy":
        result["transaction_tax"] = result["is_day_trade"] = None
    return result


def resolve_security(conn, value):
    from taiwan_stock.catalog.query import resolve_security_by_code

    if ":" in value:
        return resolve_security_by_code(conn, value)
    rows = conn.execute(
        "SELECT * FROM securities WHERE code=?", (value.strip(),)
    ).fetchall()
    active = [r for r in rows if r["status"] == "active"]
    return active[0] if len(active) == 1 else rows[0] if len(rows) == 1 else None
