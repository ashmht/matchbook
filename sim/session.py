"""Seeded market simulation: the market maker versus order flow.

One session wires together the whole stack built so far:

    fair-value random walk  ->  order flow (noise / informed traders)
                                        |
    market maker quotes  ->  MatchingEngine  ->  events  ->  Ledger
              ^                                               |
              +--------------- position, PnL <----------------+

Each step: (1) fair value takes a random-walk step, (2) the MM cancels its
stale quotes and requotes around the observable reference price with
inventory skew (its position read from the ledger), (3) a trader arrives
and sends a market order — a *noise* trader picks a random side; an
*informed* trader trades toward fair value when it diverges from the
reference, which is exactly the adverse selection that hurts market makers.

The whole session is a deterministic function of (seed, config): the same
seed replays to an identical event stream, ledger, and PnL — the core's
determinism guarantee extended end to end.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.engine import MatchingEngine
from core.events import Event
from core.ledger import Ledger
from core.risk import RiskLimits
from core.types import (
    CancelOrder,
    OrderType,
    Side,
    SubmitOrder,
)
from sim import pnl
from strategy.market_maker import MMConfig, make_quotes

import random

MM_ACCOUNT = "mm"


@dataclass(frozen=True)
class SimConfig:
    steps: int = 2_000
    initial_fair: int = 10_000        # ticks
    walk_step_max: int = 1            # fair moves by [-max, +max] per step
    informed_fraction: float = 0.0    # 0.0 = pure noise flow
    taker_qty_max: int = 3
    taker_fee_bps: int = 0
    mm: MMConfig = MMConfig()


@dataclass
class SimResult:
    engine: MatchingEngine
    ledger: Ledger
    events: list[Event]
    fair: int
    mm_equity: int                    # marked at final fair value
    mm_position: int
    trades_count: int


def run_session(seed: int, config: SimConfig = SimConfig()) -> SimResult:
    rng = random.Random(seed)
    engine = MatchingEngine(RiskLimits(price_collar_bps=None))
    ledger = Ledger("SIM", taker_fee_bps=config.taker_fee_bps)
    events: list[Event] = []
    fair = config.initial_fair
    reference = config.initial_fair       # last known good two-sided mid
    live_quote_ids: list[str] = []
    live_anchor_ids: list[str] = []

    def send(cmd) -> None:
        for event in engine.process(cmd):
            events.append(event)
            ledger.apply(event)

    for step in range(config.steps):
        ts = step

        # 1. Fair value random walk.
        fair += rng.randint(-config.walk_step_max, config.walk_step_max)

        # 2. Market maker requotes. It first pulls its own quotes, then
        #    observes the mid of the REMAINING (external) book — quoting
        #    around the market's liquidity, not its own last trade, is what
        #    keeps it from being systematically picked off as fair drifts.
        for oid in live_quote_ids:
            if engine.book.contains(oid):
                send(CancelOrder(ts=ts, order_id=oid, account=MM_ACCOUNT))
        live_quote_ids.clear()

        best_bid = engine.book.best_price(Side.BUY)
        best_ask = engine.book.best_price(Side.SELL)
        if best_bid is not None and best_ask is not None:
            reference = (best_bid + best_ask) // 2
        # else: keep the previous reference rather than falling back to the
        # last trade print. Right after a sweep the last print can sit
        # several ticks off fair, and quoting around it walks the MM into
        # the anchor's fresh quotes; a one-step-stale mid is a far better
        # estimate than a stale extreme print.

        quotes = make_quotes(config.mm, reference, pnl.position(ledger, MM_ACCOUNT))
        for side, price in ((Side.BUY, quotes.bid), (Side.SELL, quotes.ask)):
            if price is None:
                continue
            oid = f"mm-{step}-{side.value[0]}"
            live_quote_ids.append(oid)
            send(
                SubmitOrder(
                    ts=ts, order_id=oid, account=MM_ACCOUNT, side=side,
                    order_type=OrderType.LIMIT, qty=quotes.size, price=price,
                )
            )

        # 3a. Anchor liquidity: an outside liquidity provider maintains
        #     two-sided quotes around ITS view of fair, refreshing every
        #     step. Refreshing every step matters — stale anchor quotes drag
        #     the observed mid toward old fair, and the MM then trades into
        #     the drift. The anchor requotes AFTER the MM, so the MM's view
        #     of the mid is one step stale: realistic quoting latency,
        #     identical across noise/informed scenarios.
        for oid in live_anchor_ids:
            if engine.book.contains(oid):
                send(CancelOrder(ts=ts, order_id=oid, account="anchor"))
        live_anchor_ids.clear()
        for side in (Side.BUY, Side.SELL):
            # Offsets are strictly wider than the MM's half-spread plus the
            # max one-step fair move, so the two liquidity providers can
            # never cross each other's quotes: the anchor is outer book
            # depth, the MM is the inside market. Tighter anchor offsets let
            # the anchor — requoting after the MM, with fresher information —
            # pick the MM off on every fair move.
            offset = rng.randint(3, 5)
            price = fair - offset if side is Side.BUY else fair + offset
            if price > 0:
                oid = f"anchor-{step}-{side.value[0]}"
                live_anchor_ids.append(oid)
                send(
                    SubmitOrder(
                        ts=ts, order_id=oid, account="anchor", side=side,
                        order_type=OrderType.LIMIT,
                        qty=rng.randint(1, config.taker_qty_max), price=price,
                    )
                )

        # 3b. A taker arrives: informed (trades toward fair, the classic
        #     adverse selection) with probability informed_fraction, else
        #     pure noise (coin-flip side).
        qty = rng.randint(1, config.taker_qty_max)
        if rng.random() < config.informed_fraction:
            if fair == reference:
                continue                                  # nothing to exploit
            side = Side.BUY if fair > reference else Side.SELL
        else:
            side = rng.choice([Side.BUY, Side.SELL])
        send(
            SubmitOrder(
                ts=ts, order_id=f"taker-{step}", account=f"taker-{side.value}",
                side=side, order_type=OrderType.MARKET, qty=qty,
            )
        )

    from core.events import TradeExecuted
    # Mark at the last traded price — the observable market price — rather
    # than the sim's hidden fair value.
    mark = engine.last_trade_price or config.initial_fair
    return SimResult(
        engine=engine,
        ledger=ledger,
        events=events,
        fair=fair,
        mm_equity=pnl.equity(ledger, MM_ACCOUNT, mark),
        mm_position=pnl.position(ledger, MM_ACCOUNT),
        trades_count=sum(1 for e in events if isinstance(e, TradeExecuted)),
    )
