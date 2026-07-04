import unittest

from core.engine import MatchingEngine
from core.ledger import CASH, FEE_ACCOUNT, Ledger
from core.reconcile import reconcile
from core.risk import RiskLimits
from core.types import OrderType, Side, SubmitOrder

TS = 1_000


def limit(oid, account, side, price, qty):
    return SubmitOrder(
        ts=TS, order_id=oid, account=account, side=side,
        order_type=OrderType.LIMIT, qty=qty, price=price,
    )


def run(commands, taker_fee_bps=0):
    engine = MatchingEngine(RiskLimits(price_collar_bps=None))
    ledger = Ledger("BTC-USD", taker_fee_bps=taker_fee_bps)
    events = []
    for cmd in commands:
        for event in engine.process(cmd):
            events.append(event)
            ledger.apply(event)
    return engine, ledger, events


class TestLedger(unittest.TestCase):
    def test_trade_moves_position_and_cash_symmetrically(self):
        _, ledger, _ = run([
            limit("s", "alice", Side.SELL, 100, 5),
            limit("b", "bob", Side.BUY, 100, 5),
        ])
        self.assertEqual(ledger.balance("bob:position", "BTC-USD"), +5)
        self.assertEqual(ledger.balance("alice:position", "BTC-USD"), -5)
        self.assertEqual(ledger.balance("bob:cash", CASH), -500)
        self.assertEqual(ledger.balance("alice:cash", CASH), +500)

    def test_trial_balance_is_zero(self):
        _, ledger, _ = run([
            limit("s1", "alice", Side.SELL, 100, 5),
            limit("s2", "carol", Side.SELL, 101, 7),
            limit("b1", "bob", Side.BUY, 101, 9),
        ])
        for unit, total in ledger.trial_balance().items():
            self.assertEqual(total, 0, f"unit {unit} does not balance")

    def test_taker_fee_flows_to_fee_account(self):
        _, ledger, _ = run(
            [
                limit("s", "alice", Side.SELL, 100, 10),   # maker
                limit("b", "bob", Side.BUY, 100, 10),      # taker
            ],
            taker_fee_bps=25,  # 0.25% of 1000 = 2 (floor)
        )
        self.assertEqual(ledger.balance(FEE_ACCOUNT, CASH), 2)
        self.assertEqual(ledger.balance("bob:cash", CASH), -1002)
        self.assertEqual(ledger.balance("alice:cash", CASH), +1000)
        for unit, total in ledger.trial_balance().items():
            self.assertEqual(total, 0)

    def test_duplicate_event_delivery_is_idempotent(self):
        # Event delivery is at-least-once: retries and replay overlaps
        # redeliver events. The ledger must dedupe by event seq, or it
        # silently double-posts.
        from core.events import TradeExecuted
        from core.types import Side as S

        ledger = Ledger("BTC-USD", taker_fee_bps=10)
        trade = TradeExecuted(
            seq=99, ts=1, price=100, qty=5,
            taker_order_id="b", taker_account="bob", taker_side=S.BUY,
            maker_order_id="s", maker_account="alice",
        )
        ledger.apply(trade)
        before = (list(ledger.postings), ledger.balance("bob:position", "BTC-USD"))
        ledger.apply(trade)                       # redelivery
        ledger.apply(trade)                       # and again
        self.assertEqual(ledger.postings, before[0])
        self.assertEqual(ledger.balance("bob:position", "BTC-USD"), before[1])
        for unit, total in ledger.trial_balance().items():
            self.assertEqual(total, 0)

    def test_postings_trace_to_event_seq(self):
        _, ledger, events = run([
            limit("s", "alice", Side.SELL, 100, 5),
            limit("b", "bob", Side.BUY, 100, 5),
        ])
        from core.events import TradeExecuted
        trade_seqs = {e.seq for e in events if isinstance(e, TradeExecuted)}
        self.assertEqual({p.journal_id for p in ledger.postings}, trade_seqs)

    def test_reconciliation_clean(self):
        _, ledger, events = run([
            limit("s1", "alice", Side.SELL, 100, 5),
            limit("b1", "bob", Side.BUY, 100, 3),
            limit("b2", "carol", Side.BUY, 100, 2),
        ])
        self.assertEqual(reconcile(ledger, events), [])

    def test_reconciliation_detects_injected_break(self):
        _, ledger, events = run([
            limit("s", "alice", Side.SELL, 100, 5),
            limit("b", "bob", Side.BUY, 100, 5),
        ])
        # Corrupt the ledger's balance projection directly.
        ledger._balances[("bob:position", "BTC-USD")] += 1
        breaks = reconcile(ledger, events)
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0].account, "bob")
        self.assertEqual(
            (breaks[0].ledger_position, breaks[0].recomputed_position), (6, 5)
        )


if __name__ == "__main__":
    unittest.main()
