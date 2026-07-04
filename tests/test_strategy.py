import unittest

from strategy.market_maker import MMConfig, make_quotes


class TestMarketMakerQuotes(unittest.TestCase):
    CFG = MMConfig(half_spread=2, quote_size=5, max_position=30, skew_num=1, skew_den=4)

    def test_flat_position_quotes_symmetric(self):
        q = make_quotes(self.CFG, 10_000, position=0)
        self.assertEqual((q.bid, q.ask), (9_998, 10_002))

    def test_long_inventory_skews_quotes_down(self):
        q = make_quotes(self.CFG, 10_000, position=4)    # skew = 1
        self.assertEqual((q.bid, q.ask), (9_997, 10_001))

    def test_short_inventory_skews_quotes_up(self):
        q = make_quotes(self.CFG, 10_000, position=-4)   # skew = -1
        self.assertEqual((q.bid, q.ask), (9_999, 10_003))

    def test_skew_is_clamped_to_half_spread(self):
        # Position 20 implies raw skew 5, clamped to half_spread=2: the ask
        # lands exactly at reference and never below it. Unclamped skew would
        # let quotes cross the reference (the self-referential ratchet).
        q = make_quotes(self.CFG, 10_000, position=20)
        self.assertEqual((q.bid, q.ask), (9_996, 10_000))
        q = make_quotes(self.CFG, 10_000, position=-20)
        self.assertEqual((q.bid, q.ask), (10_000, 10_004))

    def test_max_long_stops_bidding_keeps_offering(self):
        q = make_quotes(self.CFG, 10_000, position=30)
        self.assertIsNone(q.bid)
        self.assertIsNotNone(q.ask)

    def test_max_short_stops_offering_keeps_bidding(self):
        q = make_quotes(self.CFG, 10_000, position=-30)
        self.assertIsNone(q.ask)
        self.assertIsNotNone(q.bid)

    def test_never_quotes_nonpositive_or_crossed(self):
        q = make_quotes(MMConfig(half_spread=2), reference_price=1, position=0)
        self.assertIsNone(q.bid)                 # 1 - 2 = -1 suppressed
        q2 = make_quotes(MMConfig(half_spread=0), 10_000, position=0)
        self.assertTrue(q2.bid is None and q2.ask is None)  # would cross itself


if __name__ == "__main__":
    unittest.main()
