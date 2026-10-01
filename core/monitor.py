"""One monitoring cycle: scrape all platforms -> detect -> dedupe -> alert."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from scrapers import SCRAPERS, ScrapeResult

from .browser import BrowserManager
from .config import DB_PATH
from .database import Database
from .detector import evaluate
from .models import Product
from .notifier import Notifier

log = logging.getLogger("monitor")

BLOCK_NOTICE_EVERY_S = 6 * 3600


async def run_cycle(
    cfg: Dict[str, Any],
    platforms: Optional[List[str]] = None,
    dry_run: bool = False,
    debug: bool = False,
    show_top: int = 0,
) -> Dict[str, Any]:
    platforms = [p for p in (platforms or cfg["monitor"]["platforms"]) if p in SCRAPERS]
    db = Database(DB_PATH)
    notifier = Notifier(cfg)
    started = time.time()
    summary: Dict[str, Any] = {"platforms": {}, "alerts": 0}
    try:
        if not dry_run:
            await notifier.start()
        sem = asyncio.Semaphore(max(1, int(cfg["monitor"]["concurrency"])))

        async with BrowserManager(cfg) as bm:
            async def one(name: str) -> None:
                async with sem:
                    scraper = SCRAPERS[name](cfg, db, debug=debug)
                    budget = float(cfg["monitor"].get("platform_timeout_s", 240))
                    try:
                        res: ScrapeResult = await asyncio.wait_for(scraper.scrape(bm), timeout=budget)
                    except asyncio.TimeoutError:
                        res = ScrapeResult(name, error=f"timed out after {budget:.0f}s on {scraper._current_url or '?'}",
                                           pages_visited=len(scraper.visited_pages), seconds=budget)
                sent = await _process(res, scraper.display, cfg, db, notifier, dry_run, show_top, scraper, bm)
                summary["platforms"][name] = {
                    "products": len(res.products),
                    "pages": res.pages_visited,
                    "alerts": sent,
                    "blocked": res.blocked,
                    "error": res.error,
                    "note": res.note,
                    "seconds": round(res.seconds, 1),
                }
                summary["alerts"] += sent
                if res.blocked and not res.blocked.startswith("cooling") and not dry_run:
                    await _notify_block(name, scraper.display, res.blocked, db, notifier)

            await asyncio.gather(*(one(p) for p in platforms))
        db.prune()
    finally:
        await notifier.stop()
        db.close()

    summary["seconds"] = round(time.time() - started, 1)
    _log_summary(summary)
    return summary


async def _process(
    res: ScrapeResult,
    display: str,
    cfg: Dict[str, Any],
    db: Database,
    notifier: Notifier,
    dry_run: bool,
    show_top: int,
    scraper: Any = None,
    bm: Any = None,
) -> int:
    hours = float(cfg["monitor"]["dedupe_hours"])
    known_pages = db.known_pages(res.platform)
    to_send: List[tuple] = []
    held: Counter = Counter()
    for p in res.products:
        baseline = p.extra.get("page", "") not in known_pages
        decision = evaluate(p, cfg, db, baseline=baseline)
        if not decision.alert:
            qualifies = p.discount_pct >= float(cfg["monitor"]["min_discount_pct"]) or p.price <= 1
            if qualifies and not decision.reason.startswith("only "):  # "only 79.97% off" rounds to 80.0
                log.debug("[%s] skip %s ₹%s/₹%s: %s", p.platform, p.title[:60], p.price, p.mrp, decision.reason)
                held[_reason_group(decision.reason)] += 1
            continue
        if not db.should_alert(p.platform, p.product_id, p.price, hours):
            log.debug("[%s] dedupe: %s at ₹%s already alerted", p.platform, p.title[:60], p.price)
            continue
        if decision.note:
            p.extra["note"] = decision.note
        to_send.append((p, decision.kind))

    # History is recorded *after* evaluation so today's price can't mask itself.
    # Dry runs write nothing, so testing never suppresses a later real alert.
    if not dry_run:
        db.record_observations(res.products)
        if not res.blocked:
            db.add_pages(res.platform, res.pages)

    if held:
        log.info("[%s] %d deal(s) at %g%%+ held back: %s", res.platform, sum(held.values()),
                 float(cfg["monitor"]["min_discount_pct"]), ", ".join(f"{k} {v}" for k, v in held.most_common()))
    if show_top:
        _print_top(res.products, display, show_top)

    to_send.sort(key=lambda x: -(x[0].mrp or 0))
    m = cfg["monitor"]
    if m.get("verify_glitches", True) and scraper is not None and bm is not None:
        limit = int(m.get("max_verifications", 5))
        if len(to_send) > limit:
            log.warning("[%s] %d glitch candidates in one scan - looks like a parsing problem or promo shelf; "
                        "verifying only the top %d", res.platform, len(to_send), limit)
        confirmed = []
        for p, kind in to_send[:limit]:
            problem = await scraper.verify(bm, p)
            if problem:
                log.info("[%s] not alerting %s ₹%g: %s", res.platform, p.title[:60], p.price, problem)
            else:
                how = p.extra.get("verified_by", "product page")
                p.extra["note"] = (p.extra.get("note", "") + f"\n✅ Price re-checked ({how})").strip()
                confirmed.append((p, kind))
        to_send = confirmed

    sent = 0
    for p, kind in to_send:
        if dry_run:
            log.info("[DRY-RUN] %s | %s | ₹%s (MRP ₹%s) %s%% | %s", display, p.title[:80], p.price, p.mrp, p.discount_pct, p.url)
            sent += 1
            continue
        if await notifier.send_alert(p, display, kind):
            db.record_alert(p)
            sent += 1
            log.info("ALERT %s | %s | ₹%s (MRP ₹%s) %s%%", display, p.title[:80], p.price, p.mrp, p.discount_pct)
    return sent


async def _notify_block(name: str, display: str, reason: str, db: Database, notifier: Notifier) -> None:
    key = f"blocknotice:{name}"
    last = db.kv_get(key) or 0
    if time.time() - float(last) < BLOCK_NOTICE_EVERY_S:
        return
    db.kv_set(key, time.time())
    await notifier.send_text(
        f"⚠️ <b>{display}</b> is showing a bot check: {reason}\n"
        f"Fix: run <code>python run.py --login {name}</code>, solve it in the window, then close it.",
    )


def _reason_group(reason: str) -> str:
    for key in ("baseline", "unchanged since baseline", "seller-set MRP", "permanent discount", "MRP jumped",
                "out of stock", "blocklisted", "min_mrp", "missing MRP"):
        if key in reason:
            return {"unchanged since baseline": "unchanged", "seller-set MRP": "marketplace-MRP",
                    "permanent discount": "permanent", "MRP jumped": "MRP-jump", "min_mrp": "cheap"}.get(key, key)
    return "other"


def _print_top(products: List[Product], display: str, n: int) -> None:
    top = sorted((p for p in products if p.mrp), key=lambda p: -p.discount_pct)[:n]
    print(f"\n  Top {len(top)} discounts on {display} ({len(products)} products parsed):")
    for p in top:
        stock = "" if p.in_stock else " [OOS]"
        print(f"   {p.discount_pct:5.1f}%  ₹{p.price:<9g} MRP ₹{p.mrp:<9g} {p.title[:60]}{stock}  ({p.source})")
        print(f"          {p.url[:120]}")


def _log_summary(summary: Dict[str, Any]) -> None:
    parts = []
    for name, s in summary["platforms"].items():
        status = "BLOCKED" if s["blocked"] else "ERROR" if s["error"] else "ok"
        parts.append(f"{name}:{status} {s['products']}p/{s['pages']}pg/{s['alerts']}a/{s['seconds']}s")
        if s["blocked"]:
            log.warning("%s: %s", name, s["blocked"])
        elif s.get("note"):
            log.info("%s: %s", name, s["note"])
        elif s["error"]:
            log.warning("%s: %s", name, s["error"])
    log.info("cycle done in %ss, %d alerts | %s", summary["seconds"], summary["alerts"], " | ".join(parts))
