"""Server fallback for holdings ordering and presentation choices."""

from decimal import Decimal

SORT_LABELS = {
    "market_value": "市值",
    "unrealized_pnl": "未實現損益",
    "return_pct": "報酬率",
    "daily_pnl": "當日損益",
    "cost_yield": "成本殖利率",
    "received_yield": "實際領息殖利率",
    "account": "帳戶",
    "security": "商品",
    "quantity": "股數",
    "average_cost": "均價",
    "close": "收盤價",
    "weight": "佔比",
}


def arrange(positions, params):
    key = params.get("sort", "market_value")
    if key not in SORT_LABELS:
        key = "market_value"
    direction = params.get("direction", "descending")
    if direction not in ("ascending", "descending"):
        direction = "descending"
    display = params.get("display", "auto")
    if display not in ("auto", "table", "cards"):
        display = "auto"
    names = {
        "account": "broker_account_name",
        "security": "security_code",
        "return_pct": "unrealized_pnl_pct",
        "close": "latest_close",
        "weight": "weight_pct",
    }

    def value(row):
        raw = (
            row["position"].remaining_quantity
            if key == "quantity"
            else row.get(names.get(key, key))
        )
        return (
            raw if key in ("account", "security") or raw is None else Decimal(str(raw))
        )

    known = [p for p in positions if value(p) is not None]
    missing = [p for p in positions if value(p) is None]
    known.sort(key=value, reverse=direction == "descending")
    return known + missing, {
        "sort": key,
        "direction": direction,
        "display": display,
        "labels": SORT_LABELS,
    }
