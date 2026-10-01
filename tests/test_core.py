"""Offline tests for extraction, detection and dedupe.  Run:  python -m unittest -v"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import DEFAULTS, _deep_merge  # noqa: E402
from core.database import Database  # noqa: E402
from core.detector import evaluate  # noqa: E402
from core.extract import extract_from_json, parse_dom_cards, to_amount  # noqa: E402
from core.models import Product  # noqa: E402
from core.notifier import format_alert_html, format_alert_plain  # noqa: E402


def cfg(**monitor):
    c = _deep_merge(DEFAULTS, {})
    c["monitor"].update(alert_mode="deals", glitch_price_max=1.0)  # legacy rules for the older tests
    c["monitor"].update(monitor)
    return c


def prod(platform="zepto", pid="p1", price=40.0, mrp=400.0, **kw):
    return Product(platform=platform, product_id=pid, title=kw.pop("title", "Test Almonds 500g"),
                   price=price, mrp=mrp, url="https://x.test/p", **kw)


class TestAmounts(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(to_amount("₹1,299"), 1299)
        self.assertEqual(to_amount("Rs. 49.50"), 49.5)
        self.assertEqual(to_amount({"units": "45", "nanos": 500000000}), 45.5)
        self.assertEqual(to_amount({"value": 99}), 99)
        self.assertEqual(to_amount({"text": "₹60"}), 60)
        self.assertIsNone(to_amount(True))
        self.assertIsNone(to_amount("free"))


class TestJsonExtraction(unittest.TestCase):
    def test_zepto_like_paise(self):
        payload = {"layout": [{"items": [{"product": {"id": "11111111-2222-3333-4444-555555555555", "name": "Cashews 1kg"},
                                          "productVariant": {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                                          "mrp": 120000, "discountedSellingPrice": 19900, "outOfStock": False}]}]}
        out = extract_from_json(payload, "zepto", lambda c: f"u/{c['id']}", price_divisor=100)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].price, 199.0)
        self.assertEqual(out[0].mrp, 1200.0)
        self.assertEqual(out[0].title, "Cashews 1kg")
        self.assertTrue(out[0].in_stock)

    def test_flipkart_like_nested_pricing(self):
        payload = {"slots": [{"widget": {"data": {"products": [{"productInfo": {"value": {
            "id": "MOBXYZ", "titles": {"title": "Phone X"}, "smartUrl": "https://www.flipkart.com/phone-x/p/itm1?pid=MOBXYZ",
            "pricing": {"finalPrice": {"value": 999}, "mrp": {"value": 9999}}}}}]}}}]}
        out = extract_from_json(payload, "flipkart", lambda c: c["url"])
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual((p.price, p.mrp, p.title, p.product_id), (999, 9999, "Phone X", "MOBXYZ"))
        self.assertIn("pid=MOBXYZ", p.url)

    def test_variant_label_not_used_as_title(self):
        payload = {"product": {"name": "Figaro Olive Oil", "id": 42,
                               "variants": [{"name": "2 ltr", "id": 43, "price": 1122, "mrp": 2799}]}}
        out = extract_from_json(payload, "blinkit", lambda c: "")
        self.assertEqual(out[0].title, "Figaro Olive Oil (2 ltr)")

    def test_out_of_stock_and_nameless(self):
        payload = [{"name": "Soap", "id": 7, "price": 5, "mrp": 50, "inventory": 0},
                   {"price": 5, "mrp": 50}]  # fee object, no name -> ignored
        out = extract_from_json(payload, "blinkit", lambda c: "")
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].in_stock)


class TestDomParsing(unittest.TestCase):
    def test_strikethrough_and_badge(self):
        cards = [
            {"href": "https://www.amazon.in/x/dp/B000000001", "text": "Earbuds\n₹199\n₹2,999", "title": "Earbuds",
             "prices": [199], "mrps": [2999]},
            {"href": "https://www.flipkart.com/y/p/itmabc", "text": "Heels\n₹286\n70% off", "title": "",
             "prices": [286], "mrps": []},
            {"href": "https://blinkit.com/prn/z/prid/5", "text": "Oil 1L\n₹100\nOut of Stock", "title": "Oil 1L",
             "prices": [100], "mrps": [200]},
        ]
        out = {p.title: p for p in parse_dom_cards(cards, "t", lambda u: u.rsplit("/", 1)[-1])}
        self.assertEqual(out["Earbuds"].mrp, 2999)
        self.assertAlmostEqual(out["Heels"].discount_pct, 70.0, delta=0.2)
        self.assertFalse(out["Oil 1L"].in_stock)

    def test_brand_line_joined(self):
        cards = [{"href": "https://www.flipkart.com/a/p/itm1", "text": "FITRAVON\nPack of 2 Men Track Pants\n₹437",
                  "title": "", "prices": [437], "mrps": [1499]}]
        self.assertEqual(parse_dom_cards(cards, "f", lambda u: "1")[0].title, "FITRAVON Pack of 2 Men Track Pants")


class TestDetector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_threshold_and_glitch(self):
        c = cfg()
        self.assertTrue(evaluate(prod(price=80, mrp=400), c).alert)       # exactly 80%
        self.assertFalse(evaluate(prod(price=81, mrp=400), c).alert)      # 79.75%
        d = evaluate(prod(price=1, mrp=150), c)
        self.assertTrue(d.alert)
        self.assertEqual(d.kind, "glitch")

    def test_false_positive_filters(self):
        c = cfg()
        self.assertFalse(evaluate(prod(mrp=None), c).alert)
        self.assertFalse(evaluate(prod(price=500, mrp=400), c).alert)
        self.assertFalse(evaluate(prod(price=2, mrp=20), c).alert)                 # below min_mrp
        self.assertFalse(evaluate(prod(in_stock=False), c).alert)
        self.assertFalse(evaluate(prod(title="Free Gift Voucher"), c).alert)

    def test_marketplace_strict(self):
        c = cfg()
        self.assertFalse(evaluate(prod(platform="amazon", price=199, mrp=1499), c, self.db).alert)  # 86.7%
        self.assertTrue(evaluate(prod(platform="amazon", price=49, mrp=1499), c, self.db).alert)    # 96.7%
        self.assertTrue(evaluate(prod(platform="amazon", price=199, mrp=1499), cfg(marketplace_mode="all"), self.db).alert)

    def test_observed_drop_on_marketplace(self):
        c = cfg()
        old = time.time() - 3600
        self.db._conn.execute("INSERT INTO observations VALUES ('amazon','A1',1200,1499,?)", (old,))
        self.db._conn.commit()
        d = evaluate(prod(platform="amazon", pid="A1", price=199, mrp=1499), c, self.db)
        self.assertTrue(d.alert)
        self.assertIn("1,200", d.note)

    def test_permanent_discount_suppressed(self):
        c = cfg()
        now = time.time()
        for h in (30, 20, 13):
            self.db._conn.execute("INSERT INTO observations VALUES ('zepto','Z',60,400,?)", (now - h * 3600,))
        self.db._conn.commit()
        self.assertFalse(evaluate(prod(pid="Z", price=60, mrp=400), c, self.db).alert)   # always 85% off
        self.assertTrue(evaluate(prod(pid="Z", price=20, mrp=400), c, self.db).alert)    # genuinely dropped

    def test_mrp_jump_suppressed(self):
        c = cfg()
        now = time.time()
        for h in (3, 2, 1):
            self.db._conn.execute("INSERT INTO observations VALUES ('zepto','M',90,100,?)", (now - h * 3600,))
        self.db._conn.commit()
        self.assertFalse(evaluate(prod(pid="M", price=90, mrp=900), c, self.db).alert)


class TestBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_first_scan_is_silent_except_extreme(self):
        c = cfg()
        self.assertFalse(evaluate(prod(price=60, mrp=400), c, self.db, baseline=True).alert)   # 85%: stale deal
        self.assertTrue(evaluate(prod(price=1, mrp=400), c, self.db, baseline=True).alert)     # glitch
        self.assertTrue(evaluate(prod(price=15, mrp=400), c, self.db, baseline=True).alert)    # 96%

    def test_unchanged_after_baseline_stays_quiet_but_drop_alerts(self):
        c = cfg()
        self.db.record_observations([prod(pid="B", price=60, mrp=400)])
        self.db._conn.execute("UPDATE observations SET timestamp = timestamp - 600")
        self.db._conn.commit()
        self.assertFalse(evaluate(prod(pid="B", price=60, mrp=400), c, self.db).alert)
        self.assertTrue(evaluate(prod(pid="B", price=40, mrp=400), c, self.db).alert)
        self.assertTrue(evaluate(prod(pid="NEW", price=60, mrp=400), c, self.db).alert)       # new listing

    def test_baseline_can_be_disabled(self):
        self.assertTrue(evaluate(prod(price=60, mrp=400), cfg(baseline_first_scan=False), self.db, baseline=True).alert)


class TestGlitchMode(unittest.TestCase):
    def test_only_tiny_prices_alert(self):
        c = cfg(alert_mode="glitch", glitch_price_max=10)
        self.assertFalse(evaluate(prod(price=45, mrp=999), c).alert)    # 95% off but ₹45: not a glitch
        self.assertFalse(evaluate(prod(price=229, mrp=2999), c).alert)  # bumped-MRP style deal
        self.assertTrue(evaluate(prod(price=10, mrp=499), c).alert)
        self.assertTrue(evaluate(prod(price=0, mrp=150), c).alert)
        self.assertFalse(evaluate(prod(price=5, mrp=20), c).alert)      # cheap item, MRP < ₹99
        self.assertFalse(evaluate(prod(price=1, mrp=500, in_stock=False), c).alert)

    def test_defaults_are_glitch_mode(self):
        self.assertEqual(DEFAULTS["monitor"]["alert_mode"], "glitch")
        self.assertEqual(DEFAULTS["monitor"]["glitch_price_max"], 10.0)


class TestDedupe(unittest.TestCase):
    def test_12h_window_and_further_drop(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.db")
            p = prod(price=50)
            self.assertTrue(db.should_alert("zepto", "p1", 50, 12))
            db.record_alert(p)
            self.assertFalse(db.should_alert("zepto", "p1", 50, 12))   # same price
            self.assertFalse(db.should_alert("zepto", "p1", 60, 12))   # went up
            self.assertTrue(db.should_alert("zepto", "p1", 45, 12))    # dropped further
            db._conn.execute("UPDATE alerts SET timestamp = timestamp - 13*3600")
            db._conn.commit()
            self.assertTrue(db.should_alert("zepto", "p1", 50, 12))    # window expired
            db.close()


class TestFormatting(unittest.TestCase):
    def test_alert_text(self):
        p = prod(price=49, mrp=499, title="Choco <Bar> & Co")
        html = format_alert_html(p, "Zepto", "drop")
        self.assertIn("80%+ PRICE DROP / GLITCH DETECTED!", html)
        self.assertIn("&lt;Bar&gt; &amp; Co", html)
        self.assertIn("₹49 (MRP: ₹499)", html)
        self.assertIn("90% OFF", html)
        plain = format_alert_plain(p, "Zepto", "drop")
        self.assertNotIn("\n\n\n", plain)


if __name__ == "__main__":
    unittest.main()
