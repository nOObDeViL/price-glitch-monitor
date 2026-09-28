"""Platform-agnostic product extraction.

Two strategies, used together by every scraper:

1. ``extract_from_json`` walks any JSON payload (XHR/fetch responses, Next.js
   ``__NEXT_DATA__``, ``window.__INITIAL_STATE__`` ...) and finds "pricing
   nodes": dicts containing both a selling-price-like and an MRP-like key.
   Name / id / url / stock are looked up on the node and its nearest
   ancestors. This survives most front-end redesigns because API field names
   change far less often than CSS classes.

2. ``DOM_CARD_JS`` runs inside the page and returns product cards found via
   product-link anchors. MRP is detected from *strikethrough* styling, which is
   how every Indian store renders it, so it doesn't depend on class names.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import Product

PRICE_KEYS = [
    "discountedSellingPrice", "discounted_selling_price",
    "offer_price", "offerPrice", "selling_price", "sellingPrice",
    "final_price", "finalPrice", "sale_price", "salePrice",
    "special_price", "specialPrice", "discounted_price", "discountedPrice",
    "store_price", "storePrice", "our_price", "ourPrice",
    "sp", "price", "normal_price", "value_price", "avg_selling_price",
    "effective",  # Fynd (JioMart): price: {effective: {min, max}, marked: {...}}
]
MRP_KEYS = [
    "mrp", "MRP", "Mrp", "max_retail_price", "maxRetailPrice",
    "list_price", "listPrice", "original_price", "originalPrice",
    "strike_price", "strikePrice", "strikethrough_price", "strikeThroughPrice",
    "marked_price", "markedPrice", "actual_price", "actualPrice", "base_price",
    "marked",
]
NAME_KEYS = [
    "product_name", "productName", "display_name", "displayName",
    "name", "title", "titles", "item_name", "itemName",
]
ID_KEYS = [
    "product_id", "productId", "prid", "pvid", "product_variant_id",
    "productVariantId", "variant_id", "variantId", "sku_id", "skuId",
    "item_id", "itemId", "listingId", "listing_id", "pid", "asin",
    "objectID", "sku", "uid", "item_code", "id",
]
URL_KEYS = [
    "url", "product_url", "productUrl", "smartUrl", "baseUrl", "deeplink",
    "deep_link", "share_url", "shareUrl", "web_url", "webUrl", "link", "url_path",
]
SLUG_KEYS = ["slug", "url_key", "urlKey", "seo_name", "seoName"]
IMAGE_KEYS = ["image", "image_url", "imageUrl", "img", "thumbnail", "images"]

# Flags meaning "unavailable" when true
OOS_TRUE_KEYS = ["out_of_stock", "outOfStock", "is_sold_out", "isSoldOut", "sold_out", "soldOut", "oos"]
# Flags meaning "available" when true
INSTOCK_TRUE_KEYS = ["in_stock", "inStock", "is_in_stock", "isInStock", "available", "isAvailable", "is_available", "sellable"]
QTY_KEYS = ["inventory", "available_quantity", "availableQuantity", "stock", "quantity_available"]

_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.I)


def to_amount(value: Any) -> Optional[float]:
    """Coerce many price representations into a float (rupees or raw units)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _AMOUNT_RE.search(value.strip())
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    if isinstance(value, dict):
        # {"units": "45", "nanos": 500000000} (protobuf money)
        if "units" in value:
            units = to_amount(value.get("units"))
            if units is not None:
                nanos = to_amount(value.get("nanos")) or 0.0
                return units + nanos / 1e9
        for k in ("value", "amount", "decimalValue", "price", "min", "text", "displayValue", "display"):
            if k in value:
                got = to_amount(value[k])
                if got is not None:
                    return got
    return None


def _first(d: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _scalar_str(v: Any) -> Optional[str]:
    if isinstance(v, (str, int)) and not isinstance(v, bool):
        s = str(v).strip()
        return s or None
    if isinstance(v, dict):  # {"title": {"text": "..."}}
        for k in ("text", "title", "value", "name"):
            if isinstance(v.get(k), str) and v[k].strip():
                return v[k].strip()
    return None


def _image(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v:
        return _image(v[0])
    if isinstance(v, dict):
        for k in ("url", "path", "src", "image_url"):
            if isinstance(v.get(k), str):
                return v[k]
    return ""


def _stock(chain: List[Dict[str, Any]]) -> bool:
    for d in chain:
        for k in OOS_TRUE_KEYS:
            if isinstance(d.get(k), bool):
                return not d[k]
        for k in INSTOCK_TRUE_KEYS:
            if isinstance(d.get(k), bool):
                return d[k]
        for k in QTY_KEYS:
            if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool):
                return d[k] > 0
        avail = d.get("availability")
        if isinstance(avail, str):
            return not re.search(r"out[\s_-]*of[\s_-]*stock|sold[\s_-]*out|unavailable", avail, re.I)
    return True


_VARIANT_RX = re.compile(
    r"^\s*(?:\d+\s*x\s*)?[\d.,/]+\s*(?:g|gm|gms|kg|mg|ml|l|ltr|litre|liter|pcs?|pieces?|pack|units?|n|cm|mm|m|inch|in|oz)?\b.*$|^pack of \d+",
    re.I,
)


def _best_title(chain: List[Dict[str, Any]]) -> Optional[str]:
    """Nearest product name, skipping variant labels like '2 ltr' (appended in brackets)."""
    variant = None
    for d in chain:
        name = _scalar_str(_first(d, NAME_KEYS))
        if not name or name.replace(".", "").isdigit():
            continue
        if len(name) < 12 and _VARIANT_RX.match(name):
            variant = variant or name
            continue
        if len(name) < 3:
            continue
        return f"{name} ({variant})" if variant and variant.lower() not in name.lower() else name
    return variant if variant and len(variant) >= 3 else None


def _lookup(chain: List[Dict[str, Any]], keys: Iterable[str], validator: Callable[[Any], Any]) -> Any:
    for d in chain:
        got = validator(_first(d, keys))
        if got:
            return got
    return None


def extract_from_json(
    payload: Any,
    platform: str,
    url_builder: Callable[[Dict[str, Any]], Optional[str]],
    price_divisor: float = 1.0,
    max_ancestors: int = 3,
) -> List[Product]:
    """Find every product-looking pricing node in an arbitrary JSON tree."""
    found: Dict[str, Product] = {}

    def visit(node: Any, ancestors: List[Dict[str, Any]], depth: int) -> None:
        if depth > 60:
            return
        if isinstance(node, list):
            for item in node:
                visit(item, ancestors, depth + 1)
            return
        if not isinstance(node, dict):
            return

        price_raw = _first(node, PRICE_KEYS)
        mrp_raw = _first(node, MRP_KEYS)
        # "price" may itself be a container: {"price": {"mrp": .., "offerPrice": ..}}
        if isinstance(price_raw, dict) and _first(price_raw, MRP_KEYS) is not None:
            price_raw = None
        if price_raw is not None and mrp_raw is not None:
            # Look at the node, then its direct child objects ({"product": {...}}),
            # then the nearest ancestors.
            children = [v for v in node.values() if isinstance(v, dict) and not _first(v, MRP_KEYS)]
            chain = [node] + children + list(reversed(ancestors[-max_ancestors:]))
            product = _build(node, chain, price_raw, mrp_raw)
            if product and product.product_id not in found:
                found[product.product_id] = product

        new_anc = ancestors + [node]
        for v in node.values():
            if isinstance(v, (dict, list)):
                visit(v, new_anc, depth + 1)

    def _build(node, chain, price_raw, mrp_raw) -> Optional[Product]:
        price = to_amount(price_raw)
        mrp = to_amount(mrp_raw)
        if price is None or mrp is None:
            return None
        # String prices like "₹1,299" are already rupees; numbers may be paise.
        if not isinstance(price_raw, str):
            price /= price_divisor
        if not isinstance(mrp_raw, str):
            mrp /= price_divisor
        title = _best_title(chain)
        if not title:
            return None
        pid = _lookup(chain, ID_KEYS, _scalar_str)
        if not pid:
            pid = "h" + hashlib.sha1(title.lower().encode()).hexdigest()[:16]
        ctx = {
            "id": pid,
            "title": title,
            "slug": _lookup(chain, SLUG_KEYS, _scalar_str),
            "url": _lookup(chain, URL_KEYS, _scalar_str),
            "node": node,
            "chain": chain,
        }
        url = url_builder(ctx) or ""
        return Product(
            platform=platform,
            product_id=str(pid),
            title=title,
            price=round(price, 2),
            mrp=round(mrp, 2),
            url=url,
            in_stock=_stock(chain),
            image=_image(_lookup(chain, IMAGE_KEYS, lambda v: v)),
            source="json",
        )

    visit(payload, [], 0)
    return list(found.values())


# ---------------------------------------------------------------------------
# DOM strategy
# ---------------------------------------------------------------------------

DOM_CARD_JS = r"""
(linkPattern) => {
  const re = new RegExp(linkPattern);
  const money = /(?:₹|Rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)/g;
  const struck = (el) => {
    for (let e = el, i = 0; e && i < 4; e = e.parentElement, i++) {
      const tag = e.tagName;
      if (tag === 'DEL' || tag === 'S' || tag === 'STRIKE') return true;
      if (e.getAttribute && (e.getAttribute('data-a-strike') === 'true')) return true;
      const td = getComputedStyle(e).textDecorationLine || '';
      if (td.includes('line-through')) return true;
      const cls = (typeof e.className === 'string' ? e.className : '') || '';
      if (/strike|line-through|\bmrp\b|original-price|was-price/i.test(cls)) return true;
    }
    return false;
  };
  const anchors = Array.from(document.querySelectorAll('a[href]')).filter(a => re.test(a.href));
  const seen = new Set();
  const out = [];
  for (const a of anchors) {
    if (seen.has(a.href)) continue;
    // Climb until the container holds a price (card), but stop before it holds many products.
    let card = a;
    for (let i = 0; i < 7 && card.parentElement; i++) {
      const txt = card.innerText || '';
      if (/₹|Rs\.?\s*\d/.test(txt)) break;
      card = card.parentElement;
    }
    const others = card.querySelectorAll('a[href]');
    let productLinks = 0;
    others.forEach(o => { if (re.test(o.href) && o.href !== a.href) productLinks++; });
    if (productLinks > 3) continue; // climbed into a grid, not a card
    seen.add(a.href);

    // Read each element's *own* text (its direct text nodes joined): React often
    // splits "₹" and "1,499" into separate text nodes inside one element.
    const prices = [], mrps = [], pcts = [];
    const els = [card, ...card.querySelectorAll('*')];
    for (const el of els) {
      let own = '';
      for (const c of el.childNodes) if (c.nodeType === 3) own += c.textContent;
      own = own.trim();
      if (!own || !/\d/.test(own)) continue;
      let m; money.lastIndex = 0;
      while ((m = money.exec(own))) {
        const v = parseFloat(m[1].replace(/,/g, ''));
        if (!isNaN(v)) (struck(el) ? mrps : prices).push(v);
      }
      const pm = own.match(/^(\d{1,2})\s*%\s*off/i);
      if (pm) pcts.push(parseInt(pm[1], 10));
    }
    const img = card.querySelector('img');
    out.push({
      href: a.href,
      text: (card.innerText || '').slice(0, 800),
      title: (a.getAttribute('title') || a.getAttribute('aria-label') || (img && img.alt) || '').trim(),
      prices, mrps, pcts,
      image: img ? (img.currentSrc || img.src || '') : ''
    });
  }
  return out;
}
"""

_OOS_TEXT = re.compile(
    r"out of stock|sold out|currently unavailable|notify me|coming soon|not deliverable|unavailable",
    re.I,
)
_PCT_OFF = re.compile(r"(?<![\d,])(\d{1,2})\s*%\s*off", re.I)
_NOISE_LINE = re.compile(
    r"₹|rs\.?\s*\d|%|\badd\b|\boff\b|mins?\b|sponsored|bestseller|delivery|rating|reviews?|^\d+(\.\d+)?$|^\(|save",
    re.I,
)


def _title_from_text(text: str) -> Optional[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidates = [ln for ln in lines if not _NOISE_LINE.search(ln) and 3 <= len(ln) <= 250]
    if not candidates:
        return None
    best = max(candidates, key=len)
    # Cards often show the brand on its own line just above the title.
    i = lines.index(best)
    if i > 0:
        prev = lines[i - 1]
        if 2 <= len(prev) <= 25 and not re.search(r"\d|₹|%", prev) and prev.lower() not in best.lower():
            best = f"{prev} {best}"
    return best


def parse_dom_cards(
    cards: List[Dict[str, Any]],
    platform: str,
    id_from_url: Callable[[str], Optional[str]],
) -> List[Product]:
    products: Dict[str, Product] = {}
    for c in cards:
        prices = [p for p in c.get("prices", []) if p is not None]
        mrps = [p for p in c.get("mrps", []) if p is not None]
        if not prices:
            continue
        price = prices[0]
        mrp: Optional[float] = max(mrps) if mrps else None
        if mrp is None:
            # Fallback: derive MRP from an explicit "NN% off" badge.
            pcts = c.get("pcts") or []
            m = _PCT_OFF.search(c.get("text", ""))
            pct = pcts[0] if pcts else (int(m.group(1)) if m else 0)
            if 0 < pct < 100:
                mrp = round(price / (1 - pct / 100), 2)
            elif len(prices) >= 2 and prices[1] > price:
                mrp = prices[1]
        title = c.get("title") or _title_from_text(c.get("text", ""))
        if not title:
            continue
        href = c["href"]
        pid = id_from_url(href) or "h" + hashlib.sha1(href.split("?")[0].encode()).hexdigest()[:16]
        if pid in products:
            continue
        products[pid] = Product(
            platform=platform,
            product_id=pid,
            title=title.strip(),
            price=price,
            mrp=mrp,
            url=href,
            in_stock=not _OOS_TEXT.search(c.get("text", "")),
            image=c.get("image", ""),
            source="dom",
        )
    return list(products.values())
