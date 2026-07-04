import unittest

from core.engine import MatchingEngine
from core.events import (
    OrderAccepted,
    OrderCanceled,
    OrderRejected,
    OrderRested,
    TradeExecuted,
)
from core.risk import RiskLimits
from core.types import (
    CancelOrder,
    CancelReason,
    OrderType,
    RejectReason,
    Side,
    SubmitOrder,
    TimeInForce,
)

TS = 1_000


def limit(oid, account, side, price, qty, tif=TimeInForce.GTC, ts=TS):
    return SubmitOrder(
        ts=ts, order_id=oid, account=account, side=side,
        order_type=OrderType.LIMIT, qty=qty, price=price, tif=tif,
    )


def market(oid, account, side, qty, ts=TS):
    return SubmitOrder(
        ts=ts, order_id=oid, account=account, side=side,
        order_type=OrderType.MARKET, qty=qty,
    )


def trades(events):
    return [e for e in events if isinstance(e, TradeExecuted)]


class TestMatching(unittest.TestCase):
    def setUp(self):
        # Disable the price collar so tests control prices freely.
        self.engine = MatchingEngine(RiskLimits(price_collar_bps=None))

    def test_resting_order_rests(self):
        events = self.engine.process(limit("o1", "alice", Side.BUY, 100, 10))
        self.assertIsInstance(events[0], OrderAccepted)
        self.assertIsInstance(events[1], OrderRested)
        self.assertEqual(self.engine.book.depth(Side.BUY), [(100, 10)])

    def test_full_fill_at_maker_price(self):
        self.engine.process(limit("o1", "alice", Side.SELL, 101, 10))
        events = self.engine.process(limit("o2", "bob", Side.BUY, 105, 10))
        [t] = trades(events)
        self.assertEqual((t.price, t.qty), (101, 10))  # maker's price, not 105
        self.assertEqual(t.maker_account, "alice")
        self.assertEqual(self.engine.book.depth(Side.SELL), [])

    def test_partial_fill_remainder_rests(self):
        self.engine.process(limit("o1", "alice", Side.SELL, 101, 4))
        events = self.engine.process(limit("o2", "bob", Side.BUY, 101, 10))
        [t] = trades(events)
        self.assertEqual(t.qty, 4)
        rested = [e for e in events if isinstance(e, OrderRested)]
        self.assertEqual(rested[0].remaining_qty, 6)
        self.assertEqual(self.engine.book.depth(Side.BUY), [(101, 6)])

    def test_price_priority_sweeps_best_first(self):
        self.engine.process(limit("a", "alice", Side.SELL, 103, 5))
        self.engine.process(limit("b", "alice", Side.SELL, 101, 5))
        self.engine.process(limit("c", "alice", Side.SELL, 102, 5))
        events = self.engine.process(limit("d", "bob", Side.BUY, 103, 15))
        self.assertEqual([t.price for t in trades(events)], [101, 102, 103])

    def test_time_priority_fifo_within_level(self):
        self.engine.process(limit("first", "alice", Side.SELL, 101, 5))
        self.engine.process(limit("second", "carol", Side.SELL, 101, 5))
        events = self.engine.process(limit("t", "bob", Side.BUY, 101, 5))
        [t] = trades(events)
        self.assertEqual(t.maker_order_id, "first")

    def test_limit_does_not_cross_worse_price(self):
        self.engine.process(limit("o1", "alice", Side.SELL, 105, 10))
        events = self.engine.process(limit("o2", "bob", Side.BUY, 104, 10))
        self.assertEqual(trades(events), [])
        self.assertEqual(self.engine.book.depth(Side.BUY), [(104, 10)])
        self.assertFalse(self.engine.book.is_crossed())

    def test_ioc_cancels_remainder(self):
        self.engine.process(limit("o1", "alice", Side.SELL, 101, 4))
        events = self.engine.process(
            limit("o2", "bob", Side.BUY, 101, 10, tif=TimeInForce.IOC)
        )
        cancels = [e for e in events if isinstance(e, OrderCanceled)]
        self.assertEqual(cancels[0].reason, CancelReason.IOC_REMAINDER)
        self.assertEqual(cancels[0].remaining_qty, 6)
        self.assertEqual(self.engine.book.depth(Side.BUY), [])

    def test_market_order_never_rests(self):
        self.engine.process(limit("o1", "alice", Side.SELL, 101, 4))
        events = self.engine.process(market("o2", "bob", Side.BUY, 10))
        [t] = trades(events)
        self.assertEqual(t.qty, 4)
        cancels = [e for e in events if isinstance(e, OrderCanceled)]
        self.assertEqual(cancels[0].reason, CancelReason.MARKET_REMAINDER)
        self.assertEqual(self.engine.book.depth(Side.BUY), [])

    def test_market_order_into_empty_book_cancels_fully(self):
        events = self.engine.process(market("o1", "bob", Side.BUY, 10))
        cancels = [e for e in events if isinstance(e, OrderCanceled)]
        self.assertEqual(cancels[0].remaining_qty, 10)

    def test_self_trade_prevention_cancels_resting(self):
        self.engine.process(limit("own", "bob", Side.SELL, 101, 5))
        self.engine.process(limit("other", "alice", Side.SELL, 101, 5))
        events = self.engine.process(limit("taker", "bob", Side.BUY, 101, 5))
        cancels = [e for e in events if isinstance(e, OrderCanceled)]
        self.assertEqual(cancels[0].order_id, "own")
        self.assertEqual(cancels[0].reason, CancelReason.SELF_TRADE_PREVENTION)
        [t] = trades(events)
        self.assertEqual((t.maker_account, t.taker_account), ("alice", "bob"))

    def test_cancel_resting_order(self):
        self.engine.process(limit("o1", "alice", Side.BUY, 100, 10))
        events = self.engine.process(CancelOrder(ts=TS, order_id="o1", account="alice"))
        self.assertIsInstance(events[0], OrderCanceled)
        self.assertEqual(self.engine.book.depth(Side.BUY), [])

    def test_cancel_unknown_or_foreign_order_rejected(self):
        events = self.engine.process(CancelOrder(ts=TS, order_id="nope", account="a"))
        self.assertEqual(events[0].reason, RejectReason.UNKNOWN_ORDER)
        self.engine.process(limit("o1", "alice", Side.BUY, 100, 10))
        events = self.engine.process(CancelOrder(ts=TS, order_id="o1", account="mallory"))
        self.assertEqual(events[0].reason, RejectReason.UNKNOWN_ORDER)
        self.assertTrue(self.engine.book.contains("o1"))  # alice's order untouched

    def test_duplicate_order_id_rejected_idempotently(self):
        self.engine.process(limit("o1", "alice", Side.BUY, 100, 10))
        events = self.engine.process(limit("o1", "alice", Side.BUY, 100, 10))
        self.assertEqual(events[0].reason, RejectReason.DUPLICATE_ORDER_ID)
        self.assertEqual(self.engine.book.depth(Side.BUY), [(100, 10)])  # no double add

    def test_qty_conservation_per_order(self):
        self.engine.process(limit("m", "alice", Side.SELL, 101, 7))
        events = self.engine.process(
            limit("t", "bob", Side.BUY, 101, 10, tif=TimeInForce.IOC)
        )
        filled = sum(t.qty for t in trades(events))
        canceled = sum(
            e.remaining_qty for e in events if isinstance(e, OrderCanceled)
        )
        self.assertEqual(filled + canceled, 10)


class TestRiskGate(unittest.TestCase):
    def test_kill_switch_rejects_everything(self):
        engine = MatchingEngine(RiskLimits(kill_switch=True))
        events = engine.process(limit("o1", "alice", Side.BUY, 100, 1))
        self.assertEqual(events[0].reason, RejectReason.KILL_SWITCH)

    def test_max_order_qty(self):
        engine = MatchingEngine(RiskLimits(max_order_qty=100))
        events = engine.process(limit("o1", "alice", Side.BUY, 100, 101))
        self.assertEqual(events[0].reason, RejectReason.MAX_ORDER_QTY)

    def test_price_collar_after_first_trade(self):
        engine = MatchingEngine(RiskLimits(price_collar_bps=1_000))  # 10%
        engine.process(limit("s", "alice", Side.SELL, 100, 5))
        engine.process(limit("b", "bob", Side.BUY, 100, 5))          # last = 100
        ok = engine.process(limit("in", "bob", Side.BUY, 109, 1))
        self.assertIsInstance(ok[0], OrderAccepted)
        bad = engine.process(limit("out", "bob", Side.BUY, 111, 1))
        self.assertEqual(bad[0].reason, RejectReason.PRICE_COLLAR)

    def test_max_open_orders(self):
        engine = MatchingEngine(
            RiskLimits(max_open_orders_per_account=2, price_collar_bps=None)
        )
        engine.process(limit("a", "alice", Side.BUY, 100, 1))
        engine.process(limit("b", "alice", Side.BUY, 99, 1))
        events = engine.process(limit("c", "alice", Side.BUY, 98, 1))
        self.assertEqual(events[0].reason, RejectReason.MAX_OPEN_ORDERS)

    def test_market_order_with_price_rejected(self):
        engine = MatchingEngine()
        bad = SubmitOrder(
            ts=TS, order_id="m1", account="a", side=Side.BUY,
            order_type=OrderType.MARKET, qty=1, price=100,
        )
        events = engine.process(bad)
        self.assertEqual(events[0].reason, RejectReason.INVALID_PRICE)


if __name__ == "__main__":
    unittest.main()
