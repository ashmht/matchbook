"""Inventory-skewed market maker (Avellaneda-Stoikov flavored, integer form).

Pure decision function: (config, reference price, current position) -> quotes.
No I/O, no state — position arrives as an input (read from the ledger by the
caller), so the strategy is trivially unit-testable and replayable.

Quoting rule, all in integer ticks:

    skew = (position * skew_num) // skew_den        # rounds toward -inf
    bid  = ref - half_spread - skew
    ask  = ref + half_spread - skew

A long position pushes both quotes down (eager to sell, reluctant to buy);
a short position pushes both up. At +/- max_position the strategy stops
quoting the side that would grow the position further, so inventory is
hard-bounded by max_position + quote_size.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MMConfig:
    half_spread: int = 2          # ticks each side of reference
    quote_size: int = 5           # lots per quote
    max_position: int = 30        # lots, absolute
    skew_num: int = 1             # skew = position * num // den ticks
    skew_den: int = 4


@dataclass(frozen=True)
class Quotes:
    bid: int | None               # None = don't quote this side
    ask: int | None
    size: int


def make_quotes(config: MMConfig, reference_price: int, position: int) -> Quotes:
    # Floor division rounds toward -inf, which conveniently makes the skew
    # symmetric in effect: long inventory lowers quotes, short raises them.
    skew = (position * config.skew_num) // config.skew_den

    # Clamp |skew| to the half-spread so a quote can never cross the
    # reference price. Unclamped, a large short position produces a bid
    # ABOVE reference; in a market where our own trades feed the reference
    # this becomes a self-referential ratchet that squeezes our own position.
    skew = max(-config.half_spread, min(config.half_spread, skew))

    bid: int | None = reference_price - config.half_spread - skew
    ask: int | None = reference_price + config.half_spread - skew

    if position >= config.max_position:
        bid = None                # already max long: stop buying
    if position <= -config.max_position:
        ask = None                # already max short: stop selling

    # Degenerate guard: never quote non-positive prices or a crossed pair.
    if bid is not None and bid <= 0:
        bid = None
    if bid is not None and ask is not None and bid >= ask:
        bid, ask = None, None

    return Quotes(bid=bid, ask=ask, size=config.quote_size)
