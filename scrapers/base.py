"""Shared scraping pipeline used by every platform.

Per run and per platform:
  1. open the platform's persistent profile (logged-in or guest)
  2. guest mode: inject delivery location (cookies / "detect my location")
  3. visit deal pages + a rotating slice of auto-discovered category pages
  4. harvest products from (a) every JSON API response the page makes,
     (b) embedded state blobs (__NEXT_DATA__, __INITIAL_STATE__ ...), and
     (c) product cards in the DOM
  5. merge + dedupe, return
If a bot-challenge page shows up the platform goes into an escalating cooldown.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urljoin

from playwright.async_api import BrowserContext, Page, Response

from core.browser import BrowserManager, detect_block, human_scroll, jitter
from core.config import DEBUG_DIR
from core.database import Database
from core.extract import DOM_CARD_JS, extract_from_json, parse_dom_cards
from core.models import Product

EMBEDDED_STATE_JS = r"""
() => {
  const out = [];
  const nd = document.getElementById('__NEXT_DATA__');
  if (nd) { try { out.push(JSON.parse(nd.textContent)); } catch (e) {} }
  for (const k of ['__INITIAL_STATE__', '___INITIAL_STATE___', '__PRELOADED_STATE__', '__APOLLO_STATE__', '__NUXT__']) {
    try { if (window[k]) out.push(JSON.parse(JSON.stringify(window[k]))); } catch (e) {}
  }
  document.querySelectorAll('script[type="application/json"], script[type="application/ld+json"]').forEach(s => {
    if (s.id === '__NEXT_DATA__' || s.textContent.length > 4e6) return;
    try { out.push(JSON.parse(s.textContent)); } catch (e) {}
  });
  return out;
}
"""

# Main selling price on a product page = biggest-font, non-strikethrough ₹ amount near the top.
MAIN_PRICE_JS = r"""
() => {
  const out = [];
  const num = /^\s*(?:₹|Rs\.?)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*$/;
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 3) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 14) continue;
    const m = t.match(num); if (!m) continue;
    const gp = el.parentElement && el.parentElement.parentElement;
    if (!/₹|Rs\.?|MRP/i.test(t + ((gp && gp.innerText) || '').slice(0, 200))) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.top > 1400 || r.top < 0) continue;
    let struck = false;
    for (let e = el, i = 0; e && i < 4; e = e.parentElement, i++) {
      if ((getComputedStyle(e).textDecorationLine || '').includes('line-through') || ['DEL', 'S'].includes(e.tagName)) struck = true;
    }
    if (struck) continue;
    out.push([parseFloat(m[1].replace(/,/g, '')), parseFloat(getComputedStyle(el).fontSize), Math.round(r.top)]);
  }
  out.sort((a, b) => b[1] - a[1] || a[2] - b[2]);
  return out.slice(0, 5);
}
"""

DETECT_LOCATION_RX = re.compile(
    r"detect my location|use (?:my )?current location|use my location|locate me|detect location|enable location",
    re.I,
)

LOCATION_TRIGGER_RX = re.compile(
    r"^\s*(?:select location|add your location|location not set|select delivery location|set location|enter location)\s*$",
    re.I,
)

MAX_JSON_BYTES = 6_000_000

CLOSED_RX = re.compile(
    r"closed for the day|we.ll be back at|store is (?:currently )?closed|not serviceable|"
    r"don.t deliver (?:here|to this)|coming soon to your (?:area|location)|currently unavailable in your area",
    re.I,
)


@dataclass
class ScrapeResult:
    platform: str
    products: List[Product] = field(default_factory=list)
    pages_visited: int = 0
    blocked: Optional[str] = None
    error: Optional[str] = None
    note: Optional[str] = None
    seconds: float = 0.0
    pages: List[str] = field(default_factory=list)


class BaseScraper:
    name: str = ""
    display: str = ""
    home_url: str = ""
    seed_urls: List[str] = []
    product_link_pattern: str = r"$^"
    category_link_pattern: Optional[str] = None
    json_url_pattern: str = r"."           # which responses to parse (regex on URL)
    price_divisor: float = 1.0              # 100 if the API returns paise
    discover_on_home: bool = True
    wait_after_load_ms: int = 2500

    def __init__(self, cfg: Dict[str, Any], db: Database, debug: bool = False):
        self.cfg = cfg
        self.db = db
        self.debug = debug
        self.log = logging.getLogger(self.name)
        self._json_payloads: List[Any] = []
        self._payload_pages: List[str] = []   # page URL each payload was captured on
        self._dom_products: List[Product] = []
        self._pending: List[asyncio.Task] = []
        self._current_url: str = ""
        self.visited_pages: List[str] = []

    # ---------------------------------------------------------- hooks
    def build_url(self, ctx: Dict[str, Any]) -> Optional[str]:
        """Build a product URL from a JSON product context (id/slug/url/title)."""
        url = ctx.get("url")
        if url and isinstance(url, str) and not url.startswith(("http://", "https://")) and url.startswith("/"):
            return urljoin(self.home_url, url)
        if url and isinstance(url, str) and url.startswith("http"):
            return url
        return self.search_url(ctx["title"])

    def search_url(self, title: str) -> str:
        return self.home_url

    def clean_url(self, url: str) -> str:
        """Strip tracking junk from product links (override per platform)."""
        return url

    def id_from_url(self, url: str) -> Optional[str]:
        m = re.search(self.product_link_pattern, url)
        if m and m.groups():
            return next((g for g in m.groups() if g), None)
        return None

    async def seed_location(self, ctx: BrowserContext) -> None:
        """Guest mode, before the first page load: set location cookies (override per platform)."""
        return None

    async def prepare_location(self, ctx: BrowserContext, page: Page) -> bool:
        """Guest-mode location setup. Return True if the page must be reloaded.

        Default: press the site's 'detect my location' button (the browser
        context is granted geolocation = your configured coordinates)."""
        return await self.click_detect_location(page)

    async def after_navigation(self, page: Page) -> None:
        """Per-page hook (dismiss popups etc.)."""
        return None

    # ------------------------------------------------------- helpers
    async def click_detect_location(self, page: Page) -> bool:
        """Press the site's 'use my current location' button, opening the picker first if needed."""
        async def press_detect() -> bool:
            try:
                btn = page.get_by_text(DETECT_LOCATION_RX).first
                if await btn.is_visible():
                    await btn.click(timeout=4000)
                    await page.wait_for_timeout(4000)
                    self.log.debug("clicked 'detect location'")
                    return True
            except Exception:  # noqa: BLE001
                pass
            return False

        if await press_detect():
            return True
        try:
            trigger = page.get_by_text(LOCATION_TRIGGER_RX).first
            if await trigger.is_visible():
                await trigger.click(timeout=4000)
                await page.wait_for_timeout(2000)
                return await press_detect()
        except Exception:  # noqa: BLE001
            pass
        return False

    async def dismiss_popups(self, page: Page) -> None:
        for sel in ("button:has-text('✕')", "[aria-label='Close']", "button:has-text('Not now')", "button:has-text('Maybe later')"):
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=400):
                    await el.click(timeout=1500)
            except Exception:  # noqa: BLE001
                pass

    def _cooldown_key(self) -> str:
        return f"cooldown:{self.name}"

    def in_cooldown(self) -> Optional[float]:
        state = self.db.kv_get(self._cooldown_key()) or {}
        until = state.get("until", 0)
        return until - time.time() if until > time.time() else None

    def _set_cooldown(self) -> float:
        state = self.db.kv_get(self._cooldown_key()) or {}
        strikes = int(state.get("strikes", 0)) + 1
        minutes = min(15 * 2 ** (strikes - 1), 240)
        self.db.kv_set(self._cooldown_key(), {"until": time.time() + minutes * 60, "strikes": strikes})
        return minutes

    def _clear_cooldown(self) -> None:
        if self.db.kv_get(self._cooldown_key()):
            self.db.kv_set(self._cooldown_key(), {"until": 0, "strikes": 0})

    # ------------------------------------------------------ capture
    def _on_response(self, response: Response) -> None:
        try:
            ctype = response.headers.get("content-type", "")
        except Exception:  # noqa: BLE001
            return
        if "json" not in ctype or not re.search(self.json_url_pattern, response.url):
            return
        self._pending.append(asyncio.ensure_future(self._read_json(response, self._current_url)))

    def _add_payload(self, payload: Any, page_url: str) -> None:
        self._json_payloads.append(payload)
        self._payload_pages.append(page_url)

    async def _read_json(self, response: Response, page_url: str) -> None:
        try:
            # Streaming / long-poll responses never finish: don't wait on them.
            body = await asyncio.wait_for(response.body(), timeout=15)
            if len(body) > MAX_JSON_BYTES:
                return
            text = body.decode("utf-8", errors="ignore").strip()
            # Some APIs prefix JSON with anti-hijacking junk like ")]}'"
            start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
            if start < 0:
                return
            self._add_payload(json.loads(text[start:]), page_url)
        except Exception:  # noqa: BLE001 - redirects, aborted, non-JSON
            pass

    async def _drain(self) -> None:
        if self._pending:
            _, stuck = await asyncio.wait(self._pending, timeout=20)
            for t in stuck:
                t.cancel()
            self._pending.clear()

    # ----------------------------------------------------- discovery
    def links_from_payloads(self) -> List[str]:
        """Category URLs hidden in JSON (apps that navigate via click handlers). Override per platform."""
        return []

    async def discover_categories(self, page: Page) -> List[str]:
        cache_key = f"cats:{self.name}"
        cached = self.db.kv_get(cache_key, max_age_s=6 * 3600)
        if cached:
            return cached
        cats: List[str] = []
        if self.category_link_pattern:
            try:
                hrefs = await page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
            except Exception:  # noqa: BLE001
                hrefs = []
            rx = re.compile(self.category_link_pattern)
            for h in hrefs:
                h = h.split("#")[0]
                if rx.search(h) and h not in cats:
                    cats.append(h)
        await self._drain()
        for h in self.links_from_payloads():
            if h not in cats:
                cats.append(h)
        if cats:
            self.db.kv_set(cache_key, cats[:200])
            self.log.info("discovered %d category pages", len(cats))
        return cats

    def _rotate(self, cats: List[str], n: int) -> List[str]:
        if not cats or n <= 0:
            return []
        key = f"catcursor:{self.name}"
        cur = int(self.db.kv_get(key) or 0) % len(cats)
        chosen = [cats[(cur + i) % len(cats)] for i in range(min(n, len(cats)))]
        self.db.kv_set(key, cur + len(chosen))
        return chosen

    # ----------------------------------------------------- page visit
    async def visit(self, page: Page, url: str) -> Optional[str]:
        """Navigate, scroll, collect. Returns a block reason if challenged.

        Hard 75 s cap per page: a page that hangs is skipped, not the whole platform."""
        try:
            return await asyncio.wait_for(self._visit(page, url), timeout=75)
        except asyncio.TimeoutError:
            self.log.warning("page took too long, skipped: %s", url)
            try:
                await page.evaluate("() => window.stop()")
            except Exception:  # noqa: BLE001
                pass
            return None

    async def _visit(self, page: Page, url: str) -> Optional[str]:
        t0 = time.time()
        self.log.debug("visit %s", url)
        self._current_url = url
        if url not in self.visited_pages:
            self.visited_pages.append(url)
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("navigation failed %s: %s", url, str(exc).splitlines()[0])
            return None
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(self.wait_after_load_ms)
        blocked = await detect_block(page)
        if blocked:
            await self._dump(page, "blocked")
            return blocked
        await self.dismiss_popups(page)
        await self.after_navigation(page)
        await human_scroll(page, int(self.cfg["browser"]["scrolls"]))
        await page.wait_for_timeout(1200)

        try:
            for blob in await page.evaluate(EMBEDDED_STATE_JS):
                self._add_payload(blob, url)
        except Exception:  # noqa: BLE001
            pass
        try:
            cards = await page.evaluate(DOM_CARD_JS, self.product_link_pattern)
            for prod in parse_dom_cards(cards, self.name, self.id_from_url):
                prod.extra["page"] = url
                self._dom_products.append(prod)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("DOM extraction failed: %s", exc)
        self.log.debug("  done in %.1fs (%d JSON payloads, %d DOM cards so far)", time.time() - t0,
                       len(self._json_payloads), len(self._dom_products))
        return None

    async def _dump(self, page: Page, tag: str) -> None:
        if not self.debug:
            return
        d = DEBUG_DIR / self.name
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            await page.screenshot(path=str(d / f"{stamp}-{tag}.png"), full_page=False)
            (d / f"{stamp}-{tag}.html").write_text(await page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------- verify
    async def verify(self, bm: BrowserManager, p: Product) -> Optional[str]:
        """Independently re-check a glitch before alerting. Returns None if confirmed, else why not.

        1. Product page: the *main* price (largest-font, non-strikethrough ₹ amount near the
           top) or the page's own API data for this product must match.
        2. If the product page shows no readable price (Instamart, Zepto), reload the listing
           it was found on in a fresh visit and require it to still be <= the glitch price.
        """
        ctx = await bm.open_context(self.name)
        try:
            page = await bm.get_page(ctx)
            if p.url.startswith("http") and "/search" not in p.url:
                verdict = await self._verify_product_page(page, p)
                if verdict is not None:
                    return None if verdict == "ok" else verdict
            listing = p.extra.get("page")
            if not listing:
                return "couldn't re-check (no product page price, no listing)"
            fresh = type(self)(self.cfg, self.db)
            fresh._json_payloads, fresh._payload_pages, fresh._dom_products = [], [], []
            page.on("response", fresh._on_response)
            blocked = await fresh.visit(page, listing)
            await fresh._drain()
            if blocked:
                return f"re-check blocked ({blocked})"
            again = [q for q in await asyncio.to_thread(fresh._merge)
                     if q.product_id == p.product_id or self._norm(q.title) == self._norm(p.title)]
            if not again:
                return "gone from the listing on re-check"
            if min(q.price for q in again) > float(self.cfg["monitor"]["glitch_price_max"]):
                return f"re-check shows ₹{min(q.price for q in again):g}"
            p.extra["verified_by"] = "listing re-check"
            return None
        except Exception as exc:  # noqa: BLE001
            return f"re-check failed ({str(exc).splitlines()[0][:80]})"
        finally:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass

    async def _verify_product_page(self, page: Page, p: Product) -> Optional[str]:
        """'ok', a rejection reason, or None when the page has no readable price."""
        payloads: List[Any] = []

        async def grab(r: Response) -> None:
            try:
                if "json" in r.headers.get("content-type", ""):
                    payloads.append(await asyncio.wait_for(r.json(), 10))
            except Exception:  # noqa: BLE001
                pass

        tasks: List[asyncio.Task] = []
        page.on("response", lambda r: tasks.append(asyncio.ensure_future(grab(r))))
        try:
            await page.goto(p.url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            return None
        blocked = await detect_block(page)
        if blocked:
            return f"product page blocked ({blocked})"
        text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 5000) : ''")
        if re.search(r"out of stock|sold out|currently unavailable|notify me", text, re.I):
            return "product page says out of stock"

        close = lambda v: abs(v - p.price) <= 0.5  # noqa: E731
        main = await page.evaluate(MAIN_PRICE_JS)
        if main:
            top = main[0][0]
            if close(top):
                p.extra["verified_by"] = "product page"
                return "ok"
            return f"product page shows ₹{top:g}, not ₹{p.price:g}"

        if tasks:
            await asyncio.wait(tasks, timeout=10)
        try:
            payloads += await page.evaluate(EMBEDDED_STATE_JS)
        except Exception:  # noqa: BLE001
            pass
        for blob in payloads:
            for q in extract_from_json(blob, self.name, lambda c: "", self.price_divisor):
                if q.product_id == p.product_id or self._norm(q.title) == self._norm(p.title):
                    if close(q.price):
                        p.extra["verified_by"] = "product page data"
                        return "ok"
                    return f"product page data says ₹{q.price:g}, not ₹{p.price:g}"
        return None

    # ------------------------------------------------------------ run
    async def scrape(self, bm: BrowserManager) -> ScrapeResult:
        res = ScrapeResult(self.name)
        t0 = time.time()
        remaining = self.in_cooldown()
        if remaining:
            res.blocked = f"cooling down ({remaining / 60:.0f} min left)"
            return res

        self._json_payloads, self._payload_pages, self._dom_products = [], [], []
        ctx = await bm.open_context(self.name)
        try:
            page = await bm.get_page(ctx)
            page.on("response", self._on_response)
            logged_in = BrowserManager.is_logged_in(self.name)

            urls: List[str] = []
            first = self.home_url
            if not logged_in:
                await self.seed_location(ctx)
            blocked = await self.visit(page, first)
            res.pages_visited += 1
            if not blocked and not logged_in and await self.prepare_location(ctx, page):
                # reload so the listing reflects the chosen location
                blocked = await self.visit(page, first)
            if not blocked:
                try:
                    body = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 4000) : ''")
                    m = CLOSED_RX.search(body)
                    if m:
                        res.note = f"store says: '{m.group(0)}' (closed right now / location not serviceable)"
                except Exception:  # noqa: BLE001
                    pass
                cats = await self.discover_categories(page) if self.discover_on_home else []
                extra = list(self.cfg.get("extra_urls", {}).get(self.name, []) or [])
                urls = [u for u in self.seed_urls + extra if u != first]
                urls += [u for u in self._rotate(cats, int(self.cfg["monitor"]["max_category_pages"])) if u not in urls]

            for url in urls:
                if blocked:
                    break
                await jitter(self.cfg)
                blocked = await self.visit(page, url)
                res.pages_visited += 1
            await self._drain()

            # Remember category links seen on any page this run (grows coverage over time).
            fresh = self.links_from_payloads()
            if fresh:
                known = self.db.kv_get(f"cats:{self.name}") or []
                merged = known + [u for u in fresh if u not in known]
                if len(merged) > len(known):
                    self.db.kv_set(f"cats:{self.name}", merged[:300])

            if blocked:
                if not logged_in:
                    # WAF/bot-manager verdicts are stored in cookies; a flagged guest
                    # profile stays blocked forever unless we drop them.
                    try:
                        await ctx.clear_cookies()
                    except Exception:  # noqa: BLE001
                        pass
                mins = self._set_cooldown()
                res.blocked = f"{blocked} - pausing {mins} min. Tip: python run.py --login {self.name}"
            else:
                self._clear_cooldown()
            # Parsing MBs of JSON is CPU-bound: keep it off the event loop so the
            # other platforms' browsers keep running meanwhile.
            res.products = await asyncio.to_thread(self._merge)
            res.pages = list(self.visited_pages)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("scrape failed")
            res.error = str(exc)
        finally:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
            res.seconds = time.time() - t0
        return res

    def _merge(self) -> List[Product]:
        by_id: Dict[str, Product] = {}
        for payload, page_url in zip(self._json_payloads, self._payload_pages):
            for p in extract_from_json(payload, self.name, self.build_url, self.price_divisor):
                p.extra["page"] = page_url
                by_id.setdefault(p.product_id, p)
        if self.debug and self._json_payloads:
            d = DEBUG_DIR / self.name
            d.mkdir(parents=True, exist_ok=True)
            (d / "last_json_payloads.json").write_text(json.dumps(self._json_payloads[:80], default=str)[:40_000_000])

        # Same product seen via DOM and JSON under different ids -> match by title.
        title_idx = {self._norm(p.title): pid for pid, p in by_id.items()}
        for p in self._dom_products:
            existing = by_id.get(p.product_id) or by_id.get(title_idx.get(self._norm(p.title), ""))
            if existing:
                if p.url and (not existing.url or existing.url == self.search_url(existing.title) or existing.url == self.home_url):
                    existing.url = p.url
                if existing.mrp is None and p.mrp:
                    existing.mrp = p.mrp
                continue
            by_id[p.product_id] = p
            title_idx[self._norm(p.title)] = p.product_id
        for p in by_id.values():
            if p.url:
                p.url = self.clean_url(p.url)
        return list(by_id.values())

    @staticmethod
    def _norm(title: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:120]

    @staticmethod
    def q(text: str) -> str:
        return quote_plus(text[:80])

    @staticmethod
    def rand_choice(items: List[str], k: int) -> List[str]:
        return random.sample(items, min(k, len(items)))
