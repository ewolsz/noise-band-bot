import sys, unittest
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))
import noise_rules as R
import noise_band_bot as B

CFG = B.load_cfg()


def flat_session(price=100.0, vol=1000.0):
    return [(price, price, price, price, vol) for _ in range(R.N)]


class Rules(unittest.TestCase):
    def test_minute_index(self):
        self.assertEqual(R.minute_index("10:00"), 29)
        self.assertEqual(R.minute_index("14:00"), 269)
        self.assertEqual(R.minute_index("15:45"), 374)
        self.assertEqual(len(R.check_indexes(CFG)), 25)

    def history(self, move=0.002):
        # every past day: price sits `move` away from its open all day
        s = [(100.0, 100.0, 100.0, 100.0 * (1 + move), 1000.0) for _ in range(R.N)]
        return [s] * 20

    def test_sigma(self):
        sig = R.sigma_from_history(self.history(), 20)
        self.assertAlmostEqual(sig[50], 0.002)
        self.assertIsNone(R.sigma_from_history(self.history()[:19], 20))

    def day(self, level):
        # opening range 99.8-100.2, then price goes to `level`
        bars = []
        for i in range(R.N):
            if i < 15:
                bars.append((100.0, 100.2, 99.8, 100.0, 1000.0))
            else:
                bars.append((level, level, level, level, 1000.0))
        return bars

    def test_put_signal_needs_all_three(self):
        sig = R.sigma_from_history(self.history(), 20)       # band distance = 100*1.25*0.002 = 0.25
        d, dist, _ = R.signal_at(self.day(99.5), sig, CFG, 29)   # below range-room (99.7), band (99.75), vwap
        self.assertEqual(d, -1)
        self.assertAlmostEqual(dist, 0.25)
        d, _, _ = R.signal_at(self.day(99.72), sig, CFG, 29)     # below band, not below range low minus room
        self.assertEqual(d, 0)
        d, _, _ = R.signal_at(self.day(100.5), sig, CFG, 29)
        self.assertEqual(d, 1)
        d, _, _ = R.signal_at(self.day(100.0), sig, CFG, 29)
        self.assertEqual(d, 0)

    def test_band_blocks(self):
        sig = R.sigma_from_history(self.history(0.01), 20)   # band distance 1.25: 99.5 is inside it
        d, _, _ = R.signal_at(self.day(99.5), sig, CFG, 29)
        self.assertEqual(d, 0)

    def test_trail(self):
        # short from 100: runs to 98 (gain 2), comes back to 99.1 (gain 0.9 <= half of 2)
        fired, best, fav = R.trail([99.5, 99.0, 98.0, 98.6, 99.1], 100.0, -1, 1.0, 0.5)
        self.assertTrue(fired); self.assertAlmostEqual(best, 2.0); self.assertAlmostEqual(fav, 0.9)
        # never ran far enough to arm: holds through a full give-back
        fired, _, _ = R.trail([99.5, 100.2, 100.5], 100.0, -1, 1.0, 0.5)
        self.assertFalse(fired)

    def test_fill_session(self):
        s = R.fill_session({0: (1, 1, 1, 1, 5), 3: (2, 2, 2, 2, 5)}, 4)
        self.assertEqual([b[3] for b in s], [1, 1, 1, 2, 2])
        self.assertIsNone(R.fill_session({9: (1, 1, 1, 1, 1)}, 20))

    def test_size(self):
        opt = CFG["option"]
        self.assertEqual(R.size(5000, 4.00, opt), 2)       # $1,000 budget, $400 each
        self.assertEqual(R.size(5000, 11.26, opt), 1)      # over budget, but one is within 25%
        self.assertEqual(R.size(5000, 13.00, opt), 0)      # one contract is over 25% of the account
        self.assertEqual(R.size(1_000_000, 1.00, opt), 10) # capped

    def test_pick_contract(self):
        cons = [{"symbol": "A", "strike_price": "630", "tradable": True},
                {"symbol": "B", "strike_price": "632", "tradable": True},
                {"symbol": "C", "strike_price": "633", "tradable": False}]
        self.assertEqual(R.pick_contract(cons, 632.6)["symbol"], "B")
        self.assertIsNone(R.pick_contract([], 632))


class FakeApi:
    def __init__(self, bid=11.20, ask=11.30, equity=5000.0, status="filled"):
        self.bid, self.ask, self.equity, self.status = bid, ask, equity, status
        self.sent, self.pos = [], []
    def calendar(self, s, e): return [{"date": "2026-11-20"}, {"date": "2026-11-23"}]
    def contracts(self, u, exp, kind, lo, hi):
        self.asked = (exp, kind)
        return [{"symbol": "QQQ261123P00632000", "strike_price": "632", "tradable": True}]
    def option_quote(self, s): return self.bid, self.ask
    def account(self): return {"equity": str(self.equity)}
    def submit(self, symbol, qty, side, cid=None):
        self.sent.append((symbol, qty, side, cid)); return {"id": "1"}
    def order(self, oid):
        return {"status": self.status, "filled_avg_price": "11.26" if len(self.sent) == 1 else "25.76",
                "filled_at": "2026-11-20T15:31:02Z"}
    def positions(self): return self.pos


class Bot(unittest.TestCase):
    def setUp(self):
        B.notify = lambda *a, **k: None
        B.summary = lambda *a, **k: None

    def test_tag_round_trip(self):
        t = B.make_tag("2026-11-20", -1, 632.03, 4.12)
        self.assertEqual(t, "nb-20261120-P-u63203-d412")
        self.assertEqual(B.parse_tag(t), {"day": "2026-11-20", "dir": -1, "entry_u": 632.03, "dist": 4.12})
        self.assertIsNone(B.parse_tag("day-20261110-TSLA-r4688"))
        self.assertIsNone(B.parse_tag(None))

    def test_enter_buys_next_day_put(self):
        api = FakeApi()
        got = B.enter(api, CFG, "2026-11-20", -1, 4.12, 632.03, dry=False)
        self.assertEqual(api.asked, ("2026-11-23", "put"))
        self.assertEqual(api.sent, [("QQQ261123P00632000", 1, "buy", "nb-20261120-P-u63203-d412")])
        self.assertEqual(got["tag"]["dir"], -1)

    def test_dry_run_sends_nothing(self):
        api = FakeApi()
        B.enter(api, CFG, "2026-11-20", -1, 4.12, 632.03, dry=True)
        self.assertEqual(api.sent, [])

    def test_wide_quote_or_too_dear_is_skipped(self):
        api = FakeApi(bid=5.0, ask=11.3)
        self.assertIsNone(B.enter(api, CFG, "2026-11-20", -1, 4.12, 632.03, dry=False))
        api = FakeApi(bid=13.9, ask=14.0)
        self.assertIsNone(B.enter(api, CFG, "2026-11-20", -1, 4.12, 632.03, dry=False))
        self.assertEqual(api.sent, [])

    def test_sell_closes_only_the_option(self):
        api = FakeApi()
        api.pos = [{"asset_class": "us_equity", "symbol": "QQQ", "qty": "5"},
                   {"asset_class": "us_option", "symbol": "QQQ261123P00632000", "qty": "1", "avg_entry_price": "11.26"}]
        api.sent.append("placeholder")
        B.sell(api, "QQQ", "test")
        self.assertEqual(api.sent[-1], ("QQQ261123P00632000", 1, "sell", None))

    def test_index_bars_uses_new_york_time(self):
        raw = [{"t": "2026-11-20T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},   # 9:30 ET
               {"t": "2026-11-20T21:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]   # 4:00 PM: outside
        d = B.index_bars(raw)
        self.assertEqual(list(d["2026-11-20"].keys()), [0])

    def test_paper_only(self):
        self.assertIn("paper-api", B.TRADING_URL)


if __name__ == "__main__":
    unittest.main()
