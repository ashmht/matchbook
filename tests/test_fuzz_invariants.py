"""Property-based fuzzing with a seeded RNG (stdlib only).

Thousands of randomized commands are fired at the engine, and after the
run six system-level invariants are asserted:

  I1. The book is never crossed (checked after every command).
  I2. Quantity conservation: for every accepted order,
      original qty == filled + canceled + still-resting.
  I3. Determinism: replaying the identical command stream on a fresh
      engine yields an identical event stream and identical snapshot.
  I4. The ledger trial balance is zero for every unit.
  I5. Reconciliation between ledger balances and independently
      recomputed positions reports zero breaks.
  I6. The book's maintained per-account open-order counters match an
      independent recount over the order index (no drift).

Failures print the seed so any counterexample is reproducible.
"""

import random
import unittest

from core.engine import MatchingEngine
from core.events import OrderCanceled, OrderRested, TradeExecuted
from core.ledger import Ledger
from core.reconcile import reconcile
from core.risk import RiskLimits
from core.types import (
    CancelOrder,
    OrderType,
    Side,
    SubmitOrder,
    TimeInForce,
)

ACCOUNTS = ["alice", "bob", "carol", "dave"]
N_COMMANDS = 3_000


def generate_commands(seed: int) -> list:
    rng = random.Random(seed)
    commands = []
    live_order_ids: list[str] = []
    for i in range(N_COMMANDS):
        ts = 1_000 + i
        roll = rng.random()
        if roll < 0.15 and live_order_ids:
            oid = rng.choice(live_order_ids)
            commands.append(
                CancelOrder(ts=ts, order_id=oid, account=rng.choice(ACCOUNTS))
            )
        elif roll < 0.25:
            commands.append(
                SubmitOrder(
                    ts=ts,
                    order_id=f"mkt-{i}",
                    account=rng.choice(ACCOUNTS),
                    side=rng.choice([Side.BUY, Side.SELL]),
                    order_type=OrderType.MARKET,
                    qty=rng.randint(1, 50),
                )
            )
        else:
            oid = f"lim-{i}"
            live_order_ids.append(oid)
            commands.append(
                SubmitOrder(
                    ts=ts,
                    order_id=oid,
                    account=rng.choice(ACCOUNTS),
                    side=rng.choice([Side.BUY, Side.SELL]),
                    order_type=OrderType.LIMIT,
                    qty=rng.randint(1, 50),
                    price=rng.randint(90, 110),
                    tif=rng.choice([TimeInForce.GTC, TimeInForce.GTC, TimeInForce.IOC]),
                )
            )
        # Occasionally resubmit an existing id to exercise idempotent rejection.
        if rng.random() < 0.02 and live_order_ids:
            commands.append(
                SubmitOrder(
                    ts=ts,
                    order_id=rng.choice(live_order_ids),
                    account=rng.choice(ACCOUNTS),
                    side=Side.BUY,
                    order_type=OrderType.LIMIT,
                    qty=1,
                    price=100,
                )
            )
    return commands


def run_engine(commands, ledger=None):
    engine = MatchingEngine(RiskLimits(price_collar_bps=None))
    events = []
    for cmd in commands:
        batch = engine.process(cmd)
        events.extend(batch)
        if ledger is not None:
            for event in batch:
                ledger.apply(event)
        assert not engine.book.is_crossed(), f"crossed book after {cmd!r}"  # I1
    return engine, events


class TestFuzzInvariants(unittest.TestCase):
    SEEDS = [7, 42, 1337]

    def test_invariants_hold_under_fuzz(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                commands = generate_commands(seed)
                ledger = Ledger("BTC-USD", taker_fee_bps=10)
                engine, events = run_engine(commands, ledger)

                # I2: quantity conservation for every accepted order.
                # Only the FIRST submission of an id counts — later
                # submissions with the same id are rejected as duplicates.
                submitted: dict[str, int] = {}
                for c in commands:
                    if isinstance(c, SubmitOrder) and c.order_id not in submitted:
                        submitted[c.order_id] = c.qty
                filled: dict[str, int] = {}
                canceled: dict[str, int] = {}
                accepted_ids = set(submitted)
                for e in events:
                    if isinstance(e, TradeExecuted):
                        filled[e.taker_order_id] = filled.get(e.taker_order_id, 0) + e.qty
                        filled[e.maker_order_id] = filled.get(e.maker_order_id, 0) + e.qty
                    elif isinstance(e, OrderCanceled):
                        canceled[e.order_id] = canceled.get(e.order_id, 0) + e.remaining_qty
                for oid in accepted_ids:
                    resting = engine.book.get(oid)
                    still_open = resting.remaining if resting else 0
                    total = filled.get(oid, 0) + canceled.get(oid, 0) + still_open
                    self.assertEqual(
                        total, submitted[oid],
                        f"seed={seed} conservation violated for {oid}",
                    )

                # I3: determinism — fresh engine, same commands, same everything.
                engine2, events2 = run_engine(commands)
                self.assertEqual(events, events2, f"seed={seed} event streams differ")
                self.assertEqual(
                    engine.snapshot(), engine2.snapshot(),
                    f"seed={seed} snapshots differ",
                )

                # I4: trial balance.
                for unit, total in ledger.trial_balance().items():
                    self.assertEqual(total, 0, f"seed={seed} unit {unit} unbalanced")

                # I5: reconciliation is clean.
                self.assertEqual(
                    reconcile(ledger, events), [],
                    f"seed={seed} reconciliation breaks",
                )

                # I6: the book's maintained per-account open-order counters
                # never drift from an independent recount over the index.
                # (Maintained counters replaced an O(n) scan; drift here
                # would silently corrupt the risk gate.)
                recount: dict[str, int] = {}
                for oid in list(submitted):
                    o = engine.book.get(oid)
                    if o is not None:
                        recount[o.account] = recount.get(o.account, 0) + 1
                for account in ACCOUNTS:
                    self.assertEqual(
                        engine.book.open_order_count(account),
                        recount.get(account, 0),
                        f"seed={seed} open-order counter drift for {account}",
                    )

                # Sanity: the fuzz actually exercised the interesting paths.
                self.assertTrue(any(isinstance(e, TradeExecuted) for e in events))
                self.assertTrue(any(isinstance(e, OrderCanceled) for e in events))
                self.assertTrue(any(isinstance(e, OrderRested) for e in events))


if __name__ == "__main__":
    unittest.main()
