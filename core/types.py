"""Core domain types for the matchbook engine.

Design notes:
- All prices are integer ticks, all quantities integer lots. No floats
  ever touch the money path. Conversion to human units happens only at
  the presentation edge.
- Commands carry their own logical timestamp (`ts`). The core never
  reads a clock, generates randomness, or performs I/O — this is what
  makes replay byte-for-byte deterministic.
- `order_id` is a client-supplied idempotency key. Resubmitting an id
  the engine has already seen is rejected as a duplicate rather than
  creating a second order (at-most-once submission).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(Enum):
    GTC = "GTC"  # rest on the book until filled or canceled
    IOC = "IOC"  # fill what crosses immediately, cancel the remainder


class RejectReason(Enum):
    DUPLICATE_ORDER_ID = "DUPLICATE_ORDER_ID"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    INVALID_QTY = "INVALID_QTY"
    INVALID_PRICE = "INVALID_PRICE"
    MAX_ORDER_QTY = "MAX_ORDER_QTY"
    PRICE_COLLAR = "PRICE_COLLAR"
    MAX_OPEN_ORDERS = "MAX_OPEN_ORDERS"
    KILL_SWITCH = "KILL_SWITCH"


class CancelReason(Enum):
    USER_REQUESTED = "USER_REQUESTED"
    IOC_REMAINDER = "IOC_REMAINDER"
    MARKET_REMAINDER = "MARKET_REMAINDER"
    SELF_TRADE_PREVENTION = "SELF_TRADE_PREVENTION"


# ---------------------------------------------------------------------------
# Commands (inputs to the deterministic core)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmitOrder:
    ts: int                 # logical timestamp supplied by the shell
    order_id: str           # client order id — idempotency key
    account: str
    side: Side
    order_type: OrderType
    qty: int                # lots, must be > 0
    price: int | None = None        # ticks; required for LIMIT, None for MARKET
    tif: TimeInForce = TimeInForce.GTC


@dataclass(frozen=True)
class CancelOrder:
    ts: int
    order_id: str
    account: str


Command = SubmitOrder | CancelOrder
