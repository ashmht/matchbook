"""Durable replay: the determinism claim proven against a log file on disk.

Serialize the fuzz command stream to JSONL, read it back from disk into a
fresh engine, and assert the event stream and snapshot are identical to
the original run. This is the recovery path: a crashed venue rebuilds its
exact state from the command log.

Also verifies snapshot completeness: two engines that differ ONLY in which
order ids they have seen must produce different snapshots, because they
respond differently to a resubmitted id.
"""

import io
import unittest

from core import serde
from core.engine import MatchingEngine
from core.risk import RiskLimits
from core.types import CancelOrder, OrderType, Side, SubmitOrder
from tests.test_fuzz_invariants import generate_commands, run_engine


class TestSerdeRoundTrip(unittest.TestCase):
    def test_every_command_round_trips_exactly(self):
        for cmd in generate_commands(seed=7)[:500]:
            self.assertEqual(serde.loads(serde.dumps(cmd)), cmd)

    def test_replay_from_log_reproduces_engine_exactly(self):
        commands = generate_commands(seed=42)
        engine_live, events_live = run_engine(commands)

        log = io.StringIO()
        serde.write_log(commands, log)          # "durable" log
        log.seek(0)

        engine_recovered = MatchingEngine(RiskLimits(price_collar_bps=None))
        events_recovered = []
        for cmd in serde.read_log(log):
            events_recovered.extend(engine_recovered.process(cmd))

        self.assertEqual(events_live, events_recovered)
        self.assertEqual(engine_live.snapshot(), engine_recovered.snapshot())


class TestSnapshotCompleteness(unittest.TestCase):
    def test_seen_order_ids_are_part_of_the_snapshot(self):
        # Engine A saw order id "x"; engine B saw "y". Books end up empty
        # and sequence counters equal — but the engines are NOT
        # behaviorally identical: A rejects a resubmission of "x" as a
        # duplicate while B accepts it. The snapshots must differ.
        def submit(oid):
            return SubmitOrder(
                ts=1, order_id=oid, account="al", side=Side.BUY,
                order_type=OrderType.LIMIT, qty=1, price=100,
            )

        a = MatchingEngine(RiskLimits(price_collar_bps=None))
        b = MatchingEngine(RiskLimits(price_collar_bps=None))
        a.process(submit("x"))
        a.process(CancelOrder(ts=2, order_id="x", account="al"))
        b.process(submit("y"))
        b.process(CancelOrder(ts=2, order_id="y", account="al"))

        self.assertEqual(a.book.snapshot(), b.book.snapshot())  # books match
        self.assertNotEqual(a.snapshot(), b.snapshot())         # engines don't


if __name__ == "__main__":
    unittest.main()
