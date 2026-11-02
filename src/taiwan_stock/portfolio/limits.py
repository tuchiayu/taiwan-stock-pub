"""Upper bounds for share counts and money (design 060).

Far above any real trade, yet low enough that a price times a share
count stays inside Decimal's default 28 digits, so nothing stored can
make a later calculation overflow.
"""

from decimal import Decimal, InvalidOperation

MAX_QUANTITY = 10**12
MAX_UNIT_PRICE = Decimal(10**7)
MAX_AMOUNT = Decimal(10**15)
MAX_DECIMAL_PLACES = 6

MONEY_FIELDS = (
    "unit_price",
    "executed_amount",
    "commission",
    "transaction_tax",
    "other_fees",
)


def maximum_for(field: str) -> Decimal:
    return MAX_UNIT_PRICE if field == "unit_price" else MAX_AMOUNT


def check_quantity(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_QUANTITY:
        raise ValueError(f"股數必須是 1 到 {MAX_QUANTITY:,} 的整數。")
    return value


def check_amount(value, maximum: Decimal, label: str = "金額") -> Decimal:
    message = f"{label}必須介於 0 到 {maximum:,}，最多 {MAX_DECIMAL_PLACES} 位小數。"
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(message) from exc
    if (
        not amount.is_finite()
        or amount < 0
        or amount > maximum
        or amount.as_tuple().exponent < -MAX_DECIMAL_PLACES
    ):
        raise ValueError(message)
    return amount


def check_gross(unit_price: Decimal, quantity: int) -> None:
    if unit_price * quantity > MAX_AMOUNT:
        raise ValueError(f"成交金額超過 {MAX_AMOUNT:,}，請確認股數與單價。")


def trade_problem(trade) -> str | None:
    """The first stored field outside these bounds, or None. `trade` is
    a stored row or a dict that may leave out unused money fields."""
    try:
        check_quantity(trade["quantity"])
    except ValueError:
        return "quantity"
    for field in MONEY_FIELDS:
        value = trade[field] if field in trade.keys() else None
        if value is None:
            continue
        try:
            amount = check_amount(value, maximum_for(field))
        except ValueError:
            return field
        if field == "unit_price":
            try:
                check_gross(amount, trade["quantity"])
            except ValueError:
                return field
    return None
