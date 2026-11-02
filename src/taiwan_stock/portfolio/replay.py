"""FIFO replay engine: the computational core of design 002 §4.8.

`trades` is the sole source of truth (§4.6.3) — nothing here is
persisted. Every call re-derives remaining quantity, remaining cost
and per-lot detail by replaying a security's `buy`/`opening`/`sell`
rows and its `corporate_actions` split/stock-dividend rows as one
date-ordered event stream (design 030 §4.2.1).

On each ex-date every held lot is multiplied by the action's ratio,
then the account's total holding is floored to whole shares: TDCC
allots stock dividends and splits per account and pays the fraction
(畸零股) in cash, so a sale of the shares the broker actually shows
must leave nothing behind. The trimmed fraction comes off the newest
lots; their cost stays with the whole shares that remain, since the
cash in lieu isn't recorded as income. Lots are therefore tracked in
"current" units, so a sell consumes them directly instead of dividing
back through a ratio — that division is what used to leave residues
like 0.4499…955 shares. `original_remaining_quantity` (the shares of
the original purchase not yet sold) shrinks only on sells, pro rata to
the lot's current quantity.

`as_of_date` defaults to today for current holdings, but the same
function computes yesterday's position too for §4.8.2 single-day P&L.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from taiwan_stock.portfolio.corporate_actions import AUTO_APPLIED_TYPES

# Cost splits use division (consumed/remaining ratios), which almost
# never terminates cleanly in Decimal — quantizing each split to
# currency-cent precision keeps `lot.cost` from silently accumulating
# meaningless extra decimal places across repeated partial sells
# (design 002 §4.4's "高精度計算，再按最小金額單位分配").
_COST_QUANTUM = Decimal("0.01")
# Ratio-adjusted share counts are fixed to this precision so that later
# additions/subtractions stay exact even for a non-terminating ratio
# such as a manually entered 10/3 split (design 030 §4.2.1).
_QTY_QUANTUM = Decimal("0.000001")


class OversellError(Exception):
    """Raised mid-replay when a sell can't be covered by prior lots.

    §4.8 "超賣防呆": the caller is expected to run this over the full
    trade history *including* the candidate insert/edit before writing
    anything, so this error prevents the write rather than describing
    an already-broken database.
    """

    def __init__(self, trade_id: str, shortfall: Decimal, requested: Decimal):
        self.trade_id = trade_id
        self.shortfall = shortfall
        self.requested = requested
        super().__init__(
            f"trade {trade_id} sells {requested} shares but only "
            f"{requested - shortfall} are available at that point in time"
        )


@dataclass
class LotPosition:
    trade_id: str
    trade_date: str
    original_remaining_quantity: Decimal
    adjusted_quantity: Decimal
    cost: Decimal | None


@dataclass
class PositionResult:
    remaining_quantity: Decimal
    remaining_cost: Decimal | None
    lots: list[LotPosition]


@dataclass
class _Lot:
    trade_id: str
    trade_date: str
    quantity: Decimal  # current units, after every ratio applied so far
    cost: Decimal | None
    original: Decimal  # purchased shares not yet sold; trims don't count


def replay_trades(
    trades: list[dict], actions: list[dict], as_of_date: str
) -> PositionResult:
    """Compute the position for one (broker_account, security) as of a date.

    `trades` must already be filtered to a single broker account and
    security; `actions` to a single security. Trades and actions dated
    after `as_of_date` are excluded before replay — this is a genuine
    point-in-time query: a trade that (from `as_of_date`'s perspective)
    hasn't happened yet must not be able to create or consume a lot.
    On the same date an action applies before trades, so a lot bought on
    the ex-date isn't adjusted while a same-day sell already sees the
    adjusted holding; same-day buys come before same-day sells (design
    072). Raises `OversellError` if any sell *up to that
    date* can't be covered — the same check `trades.py` uses to validate
    a new/edited trade before it is written (§4.8), with the cutoff set
    to the last recorded trade date so future oversells are checked as
    well.
    """
    lots: list[_Lot] = []
    for key, event in _events(trades, actions):
        if key[0] > as_of_date:
            break
        _step(lots, key[1], event)
    return _read_out(lots)


def quantities_at(trades: list[dict], actions: list[dict], dates) -> dict[str, Decimal]:
    """`replay_trades(...).remaining_quantity` for every date, in one pass.

    The sort key starts with the date, so replaying the events dated on or
    before a date is a prefix of the full replay (design 069). Raises
    `OversellError` like `replay_trades`.
    """
    pending = sorted(set(dates), reverse=True)
    result = {}
    lots: list[_Lot] = []

    def held():
        return sum((lot.quantity for lot in lots if lot.quantity > 0), Decimal(0))

    for key, event in _events(trades, actions):
        while pending and key[0] > pending[-1]:
            result[pending.pop()] = held()
        _step(lots, key[1], event)
    for day in pending:
        result[day] = held()
    return result


def _events(trades, actions):
    """Actions apply before trades on the same date (design 030 §4.2.1), and
    buys before sells (design 072): FIFO sells the oldest lots first, so this
    changes no valid history and lets a same-day sell-then-buy replay."""
    events = [
        ((action["ex_date"], 0, 0, "", 0), action)
        for action in actions
        if action["action_type"] in AUTO_APPLIED_TYPES
    ]
    events += [
        (
            (
                trade["trade_date"],
                1,
                1 if trade["event_type"] == "sell" else 0,
                trade["created_at"],
                trade["trade_order"] if "trade_order" in trade.keys() else 0,
            ),
            trade,
        )
        for trade in trades
    ]
    events.sort(key=lambda event: event[0])
    return events


def _step(lots: list[_Lot], kind: int, event) -> None:
    if kind == 0:
        _apply_action(lots, event)
    elif event["event_type"] in ("buy", "opening"):
        lots.append(
            _Lot(
                trade_id=event["id"],
                trade_date=event["trade_date"],
                quantity=Decimal(event["quantity"]),
                cost=_trade_cost(event),
                original=Decimal(event["quantity"]),
            )
        )
    elif event["event_type"] == "sell":
        _consume(lots, event)


def _trade_cost(trade: dict) -> Decimal | None:
    if trade["event_type"] == "opening":
        if trade["executed_amount"] is None:
            return None
        return Decimal(trade["executed_amount"])

    if trade["executed_amount"] is not None:
        base = Decimal(trade["executed_amount"])
    elif trade["unit_price"] is not None:
        base = Decimal(trade["unit_price"]) * trade["quantity"]
    else:
        return None

    commission = _decimal_or_zero(trade["commission"])
    other_fees = _decimal_or_zero(trade["other_fees"])
    return base + commission + other_fees


def _decimal_or_zero(value: str | None) -> Decimal:
    return Decimal(value) if value is not None else Decimal(0)


def _clean_quantity(value: Decimal) -> Decimal:
    """Fix a share count to `_QTY_QUANTUM`, without trailing zeros."""
    value = value.quantize(_QTY_QUANTUM)
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


def _apply_action(lots: list[_Lot], action: dict) -> None:
    numerator = Decimal(action["ratio_numerator"])
    denominator = Decimal(action["ratio_denominator"])
    held = [lot for lot in lots if lot.quantity > 0]
    if not held:
        return

    # Target whole holding for the entire account: exact rational arithmetic
    # prevents per-lot quantization truncation from dropping whole shares.
    pre_total = sum((lot.quantity for lot in held), Decimal(0))
    whole = Decimal(
        int(
            (Fraction(pre_total) * int(action["ratio_numerator"]))
            // int(action["ratio_denominator"])
        )
    )

    for lot in held:
        lot.quantity = _clean_quantity(lot.quantity * numerator / denominator)

    total = sum((lot.quantity for lot in held), Decimal(0))
    diff = total - whole
    if diff < 0:
        # Quantization dropped shares below rightful whole shares (e.g. 10/3 split)
        held[-1].quantity = _clean_quantity(held[-1].quantity + (-diff))
        diff = Decimal(0)

    fraction = diff
    # Newest first; the cost of a lot trimmed to nothing moves to the
    # next older lot, so the account's total cost is unchanged.
    carried = Decimal(0)
    for lot in reversed(held):
        if carried and lot.cost is not None:
            lot.cost += carried
            carried = Decimal(0)
        if fraction <= 0 and not carried:
            break
        if fraction > 0:
            take = min(lot.quantity, fraction)
            lot.quantity = _clean_quantity(lot.quantity - take)
            fraction -= take
            if lot.quantity == 0:
                lot.original = Decimal(0)
                if lot.cost is not None:
                    carried, lot.cost = lot.cost, Decimal(0)


def _consume(lots: list[_Lot], sell_trade: dict) -> None:
    to_sell = Decimal(sell_trade["quantity"])
    requested = to_sell

    for lot in lots:
        if to_sell <= 0:
            break
        if lot.quantity <= 0:
            continue

        take = min(lot.quantity, to_sell)
        if lot.cost is not None:
            reduction = (lot.cost * (take / lot.quantity)).quantize(
                _COST_QUANTUM, rounding=ROUND_HALF_UP
            )
            lot.cost -= reduction
        if take == lot.quantity:
            lot.original = Decimal(0)
        elif lot.original == lot.quantity:
            lot.original -= take
        else:
            lot.original = _clean_quantity(
                lot.original * (lot.quantity - take) / lot.quantity
            )
        lot.quantity -= take
        to_sell -= take

    if to_sell > 0:
        raise OversellError(sell_trade["id"], shortfall=to_sell, requested=requested)


def _read_out(lots: list[_Lot]) -> PositionResult:
    remaining_quantity = Decimal(0)
    remaining_cost = Decimal(0)
    cost_known = True
    lot_positions = []

    for lot in lots:
        if lot.quantity <= 0:
            continue
        remaining_quantity += lot.quantity
        if lot.cost is None:
            cost_known = False
        else:
            remaining_cost += lot.cost
        lot_positions.append(
            LotPosition(
                trade_id=lot.trade_id,
                trade_date=lot.trade_date,
                original_remaining_quantity=lot.original,
                adjusted_quantity=lot.quantity,
                cost=lot.cost,
            )
        )

    return PositionResult(
        remaining_quantity=remaining_quantity,
        remaining_cost=remaining_cost if cost_known else None,
        lots=lot_positions,
    )
