"""Matching engine: a deterministic state machine from commands to events.

    engine.process(command) -> tuple[Event, ...]

Matching semantics:
- Price-time priority. Incoming orders match against the opposite side,
  best price first, FIFO within a level.
- Execution price is always the maker's resting price.
- LIMIT GTC: unfilled remainder rests on the book.
- LIMIT IOC: unfilled remainder is canceled.
- MARKET: matches until filled or the opposite side is empty; the
  remainder is canceled (market orders never rest).
- Self-trade prevention (cancel-resting): if the front maker order
  belongs to the taker's account, the maker order is canceled and
  matching continues. No account ever trades with itself.

Idempotency: order ids are client-supplied keys. An id the engine has
ever seen (resting, filled, canceled, or rejected) cannot be reused.
Note the exact semantics: submission is AT-MOST-ONCE. A retry of a
rejected order returns DUPLICATE_ORDER_ID, not the original rejection —
clients that want to retry after a rejection must use a fresh id, which
is the convention on real venues (client order ids are unique per
session regardless of outcome).

Purity: no clock, no randomness, no I/O. Timestamps come from commands;
sequence numbers are internal state. Same command stream in, identical
event stream out — asserted by the replay tests.
"""

from __future__ import annotations

import hashlib

from .book import Book, RestingOrder
from .events import (
    Event,
    OrderAccepted,
    OrderCanceled,
    OrderRejected,
    OrderRested,
    TradeExecuted,
)
from .risk import RiskLimits, check as risk_check
from .types import (
    CancelReason,
    Command,
    CancelOrder,
    OrderType,
    RejectReason,
    Side,
    SubmitOrder,
    TimeInForce,
)


class MatchingEngine:
    def __init__(self, risk_limits: RiskLimits | None = None) -> None:
        self.book = Book()
        self.risk_limits = risk_limits or RiskLimits()
        self.last_trade_price: int | None = None
        self._seq = 0
        self._seen_order_ids: set[str] = set()

    # -- public API -----------------------------------------------------------

    def process(self, cmd: Command) -> tuple[Event, ...]:
        if isinstance(cmd, SubmitOrder):
            return tuple(self._submit(cmd))
        if isinstance(cmd, CancelOrder):
            return tuple(self._cancel(cmd))
        raise TypeError(f"unknown command: {cmd!r}")

    def snapshot(self) -> str:
        """Canonical engine state; equal snapshots => equal future behavior.

        Includes a digest of the seen-order-id set: two engines with
        identical books but different duplicate-rejection state are NOT
        behaviorally identical (one accepts a resubmitted id, the other
        rejects it), so the ids must be part of the snapshot.
        """
        ids_digest = hashlib.sha256(
            "\n".join(sorted(self._seen_order_ids)).encode()
        ).hexdigest()[:16]
        return (
            f"seq={self._seq}|last={self.last_trade_price}"
            f"|seen={ids_digest}|{self.book.snapshot()}"
        )

    # -- internals --------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _submit(self, cmd: SubmitOrder) -> list[Event]:
        if cmd.order_id in self._seen_order_ids:
            return [self._reject(cmd, RejectReason.DUPLICATE_ORDER_ID)]
        self._seen_order_ids.add(cmd.order_id)

        reason = risk_check(
            self.risk_limits,
            cmd,
            self.last_trade_price,
            self.book.open_order_count(cmd.account),
        )
        if reason is not None:
            return [self._reject(cmd, reason)]

        events: list[Event] = [
            OrderAccepted(
                seq=self._next_seq(),
                ts=cmd.ts,
                order_id=cmd.order_id,
                account=cmd.account,
                side=cmd.side,
                price=cmd.price,
                qty=cmd.qty,
            )
        ]

        remaining = self._match(cmd, cmd.qty, events)

        if remaining > 0:
            if cmd.order_type is OrderType.MARKET:
                events.append(
                    self._canceled(cmd, remaining, CancelReason.MARKET_REMAINDER)
                )
            elif cmd.tif is TimeInForce.IOC:
                events.append(
                    self._canceled(cmd, remaining, CancelReason.IOC_REMAINDER)
                )
            else:  # LIMIT GTC remainder rests
                assert cmd.price is not None
                self.book.add(
                    RestingOrder(
                        order_id=cmd.order_id,
                        account=cmd.account,
                        side=cmd.side,
                        price=cmd.price,
                        remaining=remaining,
                    )
                )
                events.append(
                    OrderRested(
                        seq=self._next_seq(),
                        ts=cmd.ts,
                        order_id=cmd.order_id,
                        account=cmd.account,
                        side=cmd.side,
                        price=cmd.price,
                        remaining_qty=remaining,
                    )
                )
        return events

    def _match(self, cmd: SubmitOrder, remaining: int, events: list[Event]) -> int:
        opposite = cmd.side.opposite()
        while remaining > 0:
            best = self.book.best_price(opposite)
            if best is None:
                break
            if cmd.order_type is OrderType.LIMIT and not self._crosses(
                cmd.side, cmd.price, best
            ):
                break

            maker = self.book.peek(opposite, best)
            assert maker is not None

            if maker.account == cmd.account:
                # Self-trade prevention: cancel the resting order, keep matching.
                self.book.remove(maker.order_id)
                events.append(
                    OrderCanceled(
                        seq=self._next_seq(),
                        ts=cmd.ts,
                        order_id=maker.order_id,
                        account=maker.account,
                        remaining_qty=maker.remaining,
                        reason=CancelReason.SELF_TRADE_PREVENTION,
                    )
                )
                continue

            fill = min(remaining, maker.remaining)
            maker.remaining -= fill
            remaining -= fill
            self.last_trade_price = maker.price
            events.append(
                TradeExecuted(
                    seq=self._next_seq(),
                    ts=cmd.ts,
                    price=maker.price,
                    qty=fill,
                    taker_order_id=cmd.order_id,
                    taker_account=cmd.account,
                    taker_side=cmd.side,
                    maker_order_id=maker.order_id,
                    maker_account=maker.account,
                )
            )
            if maker.remaining == 0:
                self.book.pop_front(opposite, best)
        return remaining

    def _cancel(self, cmd: CancelOrder) -> list[Event]:
        resting = self.book.get(cmd.order_id)
        if resting is None or resting.account != cmd.account:
            # Unknown, already filled/canceled, or not the owner's order.
            return [
                OrderRejected(
                    seq=self._next_seq(),
                    ts=cmd.ts,
                    order_id=cmd.order_id,
                    account=cmd.account,
                    reason=RejectReason.UNKNOWN_ORDER,
                )
            ]
        self.book.remove(cmd.order_id)
        return [
            OrderCanceled(
                seq=self._next_seq(),
                ts=cmd.ts,
                order_id=cmd.order_id,
                account=cmd.account,
                remaining_qty=resting.remaining,
                reason=CancelReason.USER_REQUESTED,
            )
        ]

    @staticmethod
    def _crosses(taker_side: Side, taker_price: int | None, best: int) -> bool:
        assert taker_price is not None
        if taker_side is Side.BUY:
            return taker_price >= best
        return taker_price <= best

    def _reject(self, cmd: SubmitOrder, reason: RejectReason) -> OrderRejected:
        return OrderRejected(
            seq=self._next_seq(),
            ts=cmd.ts,
            order_id=cmd.order_id,
            account=cmd.account,
            reason=reason,
        )

    def _canceled(
        self, cmd: SubmitOrder, remaining: int, reason: CancelReason
    ) -> OrderCanceled:
        return OrderCanceled(
            seq=self._next_seq(),
            ts=cmd.ts,
            order_id=cmd.order_id,
            account=cmd.account,
            remaining_qty=remaining,
            reason=reason,
        )
