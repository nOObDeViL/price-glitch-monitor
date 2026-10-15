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
                        try:  # keep what was collected before the stall
                            res.products = scraper._merge()
                            res.pages = list(scraper.visited_pages[:-1])
                        except Exception:  # noqa: BLE001
                            pass
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
                if not dry_run and not (res.note or "").startswith("skipped"):
                    db.bump_stats(name, scans=1, products=len(res.products),
                                  blocked=1 if res.blocked else 0, failed=1 if res.error and not res.products else 0)
                if (res.blocked and not res.blocked.startswith("cooling") and not dry_run
                        and cfg["monitor"].get("block_notices", True)):
                    await _notify_block(name, scraper.display, res.blocked, db, notifier)

            await asyncio.gather(*(one(p) for p in platforms))
        if not dry_run:
            await _maybe_send_digest(cfg, db, notifier)
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
        if not dry_run and to_send:
            db.bump_stats(res.platform, candidates=len(to_send))
        for p, kind in to_send[:limit]:
            problem = await scraper.verify(bm, p)
            if problem:
                if not dry_run:
                    db.bump_stats(res.platform, rejected=1)
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
            db.bump_stats(res.platform, alerts=1)
            sent += 1
            log.info("ALERT %s | %s | ₹%s (MRP ₹%s) %s%%", display, p.title[:80], p.price, p.mrp, p.discount_pct)
    return sent


async def _maybe_send_digest(cfg: Dict[str, Any], db: Database, notifier: Notifier) -> None:
    """Once a day after `digest_hour_ist`: biggest genuine drops + how each platform did."""
    m = cfg["monitor"]
    if not m.get("digest_enabled", True):
        return
    import datetime as _dt
    ist_now = _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)
    today = db.ist_date()
    if ist_now.hour < int(m.get("digest_hour_ist", 21)) or db.kv_get("digest:last") == today:
        return
    if await notifier.send_text(build_digest(cfg, db)):
        db.kv_set("digest:last", today)


def build_digest(cfg: Dict[str, Any], db: Database) -> str:
    import html as _html

    from scrapers import SCRAPERS as _S

    m = cfg["monitor"]
    e = lambda t: _html.escape(str(t), quote=False)  # noqa: E731
    names = {k: v.display for k, v in _S.items()}
    min_drop = float(m.get("digest_min_drop_pct", 30))
    drops = db.biggest_real_drops(min_drop, float(m.get("error_min_reference", 50)), int(m.get("digest_size", 10)))

    lines = ["📊 <b>Daily deal digest</b>",
             f"<i>Biggest real price drops today: each compared to that product's own lowest-ever price "
             f"(min {min_drop:g}% lower), so inflated MRPs can't sneak in.</i>", ""]
    if drops:
        for i, (plat, _pid, price, mrp, floor, _n, title, url) in enumerate(drops, 1):
            pct = (1 - price / floor) * 100
            name = e((title or "product")[:70])
            link = f'<a href="{_html.escape(url or "", quote=True)}">{name}</a>' if url else name
            lines.append(f"{i}. {link}\n    ₹{price:,.0f} <s>lowest before ₹{floor:,.0f}</s> (-{pct:.0f}%) · "
                         f"{e(names.get(plat, plat))}")
    else:
        lines.append("No product dropped that much below its usual lowest price today.")

    stats = db.kv_get(f"stats:{db.ist_date()}") or {}
    tracked = db.products_tracked()
    lines += ["", "🩺 <b>Platform health (today)</b>"]
    for plat in m.get("platforms", []):
        st = stats.get(plat, {})
        scans = st.get("scans", 0)
        if not scans:
            lines.append(f"⚪ {e(names.get(plat, plat))}: not scanned today")
            continue
        bad = st.get("blocked", 0) + st.get("failed", 0)
        icon = "🟢" if bad < scans / 2 else "🟠" if bad < scans else "🔴"
        extra = []
        if st.get("blocked"):
            extra.append(f"{st['blocked']} blocked")
        if st.get("candidates"):
            extra.append(f"{st['candidates']} candidate(s), {st.get('rejected', 0)} failed re-check")
        lines.append(f"{icon} {e(names.get(plat, plat))}: {scans} scans · {st.get('products', 0) // max(1, scans):,} "
                     f"products/scan · {tracked.get(plat, 0):,} tracked · {st.get('alerts', 0)} alert(s)"
                     + (f" · {', '.join(extra)}" if extra else ""))
    return "\n".join(lines)[:4000]


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
    for key, label in (("learning normal price", "learning-normal-price"), ("not an error", "not-an-error"),
                       ("usual price", "standing-price")):
        if key in reason:
            return label
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
