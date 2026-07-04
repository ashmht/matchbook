"""Limit order book with strict price-time priority.

Structure: one FIFO deque of resting orders per price level, plus a
sorted list of active prices per side (maintained with bisect). Best
bid is the highest bid price; best ask is the lowest ask price.

This is the readable reference implementation. A planned Rust port will
replace the per-level deques with intrusive doubly linked lists and an
index from order id to node for O(1) cancels; this version accepts an
O(level depth) cancel in exchange for clarity, since it serves as the
oracle in differential tests rather than the hot path.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque
from dataclasses import dataclass

from .types import Side


@dataclass
class RestingOrder:
    order_id: str
    account: str
    side: Side
    price: int
    remaining: int


class Book:
    def __init__(self) -> None:
        self._levels: dict[Side, dict[int, deque[RestingOrder]]] = {
            Side.BUY: {},
            Side.SELL: {},
        }
        # Sorted ascending. Best bid = last element; best ask = first.
        self._prices: dict[Side, list[int]] = {Side.BUY: [], Side.SELL: []}
        # order_id -> resting order, for cancel lookups.
        self._index: dict[str, RestingOrder] = {}
        # account -> number of resting orders, maintained on add/remove so
        # the risk gate's open-order check is O(1) instead of a scan.
        self._open_counts: dict[str, int] = {}

    # -- queries ------------------------------------------------------------

    def best_price(self, side: Side) -> int | None:
        prices = self._prices[side]
        if not prices:
            return None
        return prices[-1] if side is Side.BUY else prices[0]

    def peek(self, side: Side, price: int) -> RestingOrder | None:
        level = self._levels[side].get(price)
        return level[0] if level else None

    def contains(self, order_id: str) -> bool:
        return order_id in self._index

    def get(self, order_id: str) -> RestingOrder | None:
        return self._index.get(order_id)

    def open_order_count(self, account: str) -> int:
        return self._open_counts.get(account, 0)

    def is_crossed(self) -> bool:
        bb, ba = self.best_price(Side.BUY), self.best_price(Side.SELL)
        return bb is not None and ba is not None and bb >= ba

    def depth(self, side: Side) -> list[tuple[int, int]]:
        """(price, total qty) per level, best first. For snapshots/tests."""
        prices = self._prices[side]
        ordered = reversed(prices) if side is Side.BUY else iter(prices)
        return [
            (p, sum(o.remaining for o in self._levels[side][p])) for p in ordered
        ]

    # -- mutations ----------------------------------------------------------

    def add(self, order: RestingOrder) -> None:
        assert order.remaining > 0
        assert not self.contains(order.order_id), "duplicate resting order id"
        levels = self._levels[order.side]
        if order.price not in levels:
            levels[order.price] = deque()
            insort(self._prices[order.side], order.price)
        levels[order.price].append(order)
        self._index[order.order_id] = order
        self._open_counts[order.account] = self._open_counts.get(order.account, 0) + 1

    def pop_front(self, side: Side, price: int) -> None:
        """Remove the front (oldest) order at a level; used after full fill."""
        level = self._levels[side][price]
        order = level.popleft()
        del self._index[order.order_id]
        self._decrement_open(order.account)
        if not level:
            self._drop_level(side, price)

    def remove(self, order_id: str) -> RestingOrder | None:
        """Remove an arbitrary resting order (cancel path)."""
        order = self._index.pop(order_id, None)
        if order is None:
            return None
        level = self._levels[order.side][order.price]
        level.remove(order)
        self._decrement_open(order.account)
        if not level:
            self._drop_level(order.side, order.price)
        return order

    def _decrement_open(self, account: str) -> None:
        count = self._open_counts[account] - 1
        if count:
            self._open_counts[account] = count
        else:
            del self._open_counts[account]

    def _drop_level(self, side: Side, price: int) -> None:
        del self._levels[side][price]
        prices = self._prices[side]
        prices.pop(bisect_left(prices, price))

    # -- canonical snapshot ---------------------------------------------------

    def snapshot(self) -> str:
        """Canonical string of full book state, FIFO order preserved.

        Two books with identical snapshots are behaviorally identical.
        Used for determinism assertions and replay verification.
        """
        parts: list[str] = []
        for side in (Side.BUY, Side.SELL):
            parts.append(side.value)
            for price in self._prices[side]:
                queue = ",".join(
                    f"{o.order_id}:{o.account}:{o.remaining}"
                    for o in self._levels[side][price]
                )
                parts.append(f"{price}=[{queue}]")
        return "|".join(parts)
