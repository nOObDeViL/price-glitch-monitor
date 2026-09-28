"""Glitch / deep-discount rules plus false-positive filtering.

Alert rule (from the spec):   discount >= 80%   OR   price <= ₹1
...then a product must survive these sanity checks:

* in stock, has an MRP, MRP >= price, MRP within [min_mrp, max_mrp], title not blocklisted
* **Permanent discount** - once we have >=3 sightings over >=12 h and the price
  is within 10% of its usual price, the "80% off" is just an inflated MRP.
* **MRP jump** - MRP suddenly 1.5x higher than any MRP we saw before, without
  the price actually falling -> seller inflated the MRP.
* **Marketplace MRPs** - on Amazon/Flipkart sellers type in their own MRP, and
  thousands of listings sit at a permanent "85% off". In ``strict`` mode those
  platforms alert only on a real glitch (<= ₹1 or >= 95% off) or on a price
  drop we actually observed (>= 20% below the highest price we've seen).
  Quick-commerce MRPs are the printed MRP, so they're trusted as-is.
* **Silent baseline** - the first time a page is scanned, its existing deals are
  recorded but not alerted (they've been sitting there, they aren't news); only
  extreme ones (<= ₹1 / >= 95%) alert. Afterwards a product alerts when it's new,
  or its price fell. A product already seen at this price, never alerted, stays quiet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .database import Database
from .models import Product


@dataclass
class Decision:
    alert: bool
    reason: str
    kind: str = ""  # "glitch" | "drop"
    note: str = ""  # extra context for the alert, e.g. "Was ₹999 here recently"


def evaluate(p: Product, cfg: Dict[str, Any], db: Optional[Database] = None, baseline: bool = False) -> Decision:
    m = cfg["monitor"]
    title_l = p.title.lower()

    for kw in m.get("blocklist_keywords", []):
        if kw.lower() in title_l:
            return Decision(False, f"blocklisted keyword '{kw}'")
    if not p.in_stock:
        return Decision(False, "out of stock")
    if p.price is None or p.price < 0:
        return Decision(False, "invalid price")
    if not p.mrp or p.mrp <= 0:
        return Decision(False, "missing MRP")
    if p.mrp < p.price:
        return Decision(False, "MRP below price (bad data)")
    if p.mrp < float(m["min_mrp"]):
        return Decision(False, f"MRP ₹{p.mrp:g} below min_mrp")
    if p.mrp > float(m["max_mrp"]):
        return Decision(False, "absurd MRP")

    discount = (p.mrp - p.price) / p.mrp * 100
    is_glitch = p.price <= float(m["glitch_price_max"])
    is_drop = discount >= float(m["min_discount_pct"])
    if not (is_glitch or is_drop):
        return Decision(False, f"only {discount:.0f}% off")
    extreme = is_glitch or discount >= float(m.get("extreme_discount_pct", 95))

    real_drop = False
    note = ""
    stats = db.history_stats(p.platform, p.product_id) if db is not None else None
    if stats and m.get("inflated_mrp_check", True):
        established = stats["count"] >= 3 and stats["span_h"] >= 12
        if established and p.price >= stats["median"] * 0.9:
            return Decision(False, f"permanent discount (usual price ₹{stats['median']:g}) - inflated MRP")
        real_drop = p.price <= stats["max_price"] * 0.8
        if real_drop:
            note = f"📉 Was ₹{stats['max_price']:,.0f} here recently"
        if not real_drop and stats["count"] >= 3 and stats["max_mrp"] and p.mrp > stats["max_mrp"] * 1.5:
            return Decision(False, f"MRP jumped from ₹{stats['max_mrp']:g} to ₹{p.mrp:g} - inflated MRP")

    if m.get("baseline_first_scan", True) and not extreme:
        if baseline:
            return Decision(False, f"{discount:.0f}% off - baseline (first scan of this page)")
        if stats and stats["min_price"] <= p.price + 0.005 and db is not None and not db.was_alerted(p.platform, p.product_id):
            return Decision(False, f"{discount:.0f}% off - unchanged since baseline")

    trusted = p.platform in m.get("trusted_mrp_platforms", [])
    if not trusted and m.get("marketplace_mode", "strict") == "strict" and not (extreme or real_drop):
        return Decision(False, f"{discount:.0f}% off a seller-set MRP (strict marketplace mode)")

    return Decision(True, f"{discount:.0f}% off", "glitch" if is_glitch else "drop", note)
