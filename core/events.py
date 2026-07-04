"""Events emitted by the matching engine.

The event stream is the source of truth. Every state change in the
system is representable as (and reconstructible from) this stream:
the book, positions, and the ledger are all projections of it.

Each event carries a monotonically increasing sequence number assigned
by the engine. Two engines fed the same command stream must emit
identical event streams — this is asserted by the determinism tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import CancelReason, RejectReason, Side


@dataclass(frozen=True)
class OrderAccepted:
    seq: int
    ts: int
    order_id: str
    account: str
    side: Side
    price: int | None
    qty: int


@dataclass(frozen=True)
class OrderRejected:
    seq: int
    ts: int
    order_id: str
    account: str
    reason: RejectReason


@dataclass(frozen=True)
class TradeExecuted:
    seq: int
    ts: int
    price: int              # execution price = maker's resting price
    qty: int
    taker_order_id: str
    taker_account: str
    taker_side: Side
    maker_order_id: str
    maker_account: str


@dataclass(frozen=True)
class OrderCanceled:
    seq: int
    ts: int
    order_id: str
    account: str
    remaining_qty: int      # quantity that was still open when canceled
    reason: CancelReason


@dataclass(frozen=True)
class OrderRested:
    """Remainder of a GTC limit order was placed on the book."""
    seq: int
    ts: int
    order_id: str
    account: str
    side: Side
    price: int
    remaining_qty: int


Event = OrderAccepted | OrderRejected | TradeExecuted | OrderCanceled | OrderRested
