"""End-to-end simulation properties.

  S1. End-to-end determinism: same seed => identical events, snapshot, PnL.
  S2. Zero-sum: total equity across ALL accounts (traders, MM, exchange
      fees) is exactly zero at any mark price. Fees redistribute, never
      create.
  S3. Inventory bound: |MM position| <= max_position + quote_size at every
      step of the session.
  S4. Spread capture: against pure noise flow, the MM's mean equity across
      seeds is positive (it earns the spread).
  S5. Adverse selection: fully informed flow strictly reduces MM PnL vs
      pure noise flow, seed by seed.
"""

import unittest

from sim import pnl
from sim.session import MM_ACCOUNT, SimConfig, run_session


class TestSimulation(unittest.TestCase):
    SEEDS = [7, 42, 1337, 2024, 9001]

    def test_s1_end_to_end_determinism(self):
        a = run_session(seed=7)
        b = run_session(seed=7)
        self.assertEqual(a.events, b.events)
        self.assertEqual(a.engine.snapshot(), b.engine.snapshot())
        self.assertEqual(a.mm_equity, b.mm_equity)

    def test_s2_market_is_zero_sum_at_any_mark(self):
        result = run_session(seed=42, config=SimConfig(taker_fee_bps=10))
        self.assertGreater(result.trades_count, 100)  # sanity: activity happened
        for mark in (1, result.fair, 123_456):
            self.assertEqual(
                pnl.total_equity_all_accounts(result.ledger, mark), 0,
                f"non-zero total equity at mark={mark}",
            )

    def test_s3_inventory_hard_bound(self):
        cfg = SimConfig(steps=1_500)
        bound = cfg.mm.max_position + cfg.mm.quote_size
        # Recompute MM position step by step from the trade stream.
        from core.events import TradeExecuted
        from core.types import Side
        result = run_session(seed=1337, config=cfg)
        position = 0
        for e in result.events:
            if isinstance(e, TradeExecuted):
                if e.maker_account == MM_ACCOUNT or e.taker_account == MM_ACCOUNT:
                    mm_is_buyer = (
                        (e.taker_account == MM_ACCOUNT and e.taker_side is Side.BUY)
                        or (e.maker_account == MM_ACCOUNT and e.taker_side is Side.SELL)
                    )
                    position += e.qty if mm_is_buyer else -e.qty
                    self.assertLessEqual(
                        abs(position), bound, f"inventory bound broken at seq={e.seq}"
                    )
        self.assertEqual(position, result.mm_position)

    def test_s4_mm_captures_spread_from_noise_flow(self):
        equities = [run_session(seed=s).mm_equity for s in self.SEEDS]
        mean = sum(equities) / len(equities)
        self.assertGreater(
            mean, 0,
            f"MM failed to profit from pure noise flow on average: {equities}",
        )

    def test_s5_informed_flow_hurts_the_mm(self):
        for seed in self.SEEDS:
            noise = run_session(seed, SimConfig(informed_fraction=0.0))
            informed = run_session(seed, SimConfig(informed_fraction=1.0))
            self.assertLess(
                informed.mm_equity, noise.mm_equity,
                f"seed={seed}: informed flow did not reduce MM PnL "
                f"({informed.mm_equity} vs {noise.mm_equity})",
            )


if __name__ == "__main__":
    unittest.main()
