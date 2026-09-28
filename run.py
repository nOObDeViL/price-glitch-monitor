#!/usr/bin/env python3
"""Price Glitch Monitor - main entry point.

  python run.py                   first run -> setup wizard, then monitor every 5 min
  python run.py --setup           (re)run the setup wizard
  python run.py --login blinkit   open a visible browser to log in / set address / clear a bot check
  python run.py --once            run a single scan and exit
  python run.py --once --dry-run --show-top 5 --platforms zepto,amazon
  python run.py --test-alert      send a sample alert to Telegram
  python run.py --status          show session state and recent alerts
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

MIN_PY = (3, 9)
if sys.version_info < MIN_PY:
    sys.exit("Python 3.9+ is required.")

try:
    from core.config import ALL_PLATFORMS, DB_PATH, LOG_DIR, config_exists, ensure_dirs, load_config
except ImportError as exc:  # dependencies missing
    sys.exit(f"Missing dependency ({exc}). Run:  pip install -r requirements.txt && python -m playwright install chromium")


def setup_logging(debug: bool) -> None:
    ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    fileh = RotatingFileHandler(LOG_DIR / "monitor.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fileh.setFormatter(fmt)
    root.handlers[:] = [console, fileh]
    for noisy in ("apscheduler", "asyncio", "telethon", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_platforms(value: str) -> list:
    items = [v.strip().lower().replace("-", "_").replace(" ", "_") for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in ALL_PLATFORMS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown platform(s): {', '.join(bad)}. Choose from: {', '.join(ALL_PLATFORMS)}")
    return items


# ---------------------------------------------------------------- commands

async def cmd_login(platform: str) -> None:
    from core.browser import BrowserManager
    from scrapers import SCRAPERS

    cfg = load_config()
    scraper_cls = SCRAPERS[platform]
    async with BrowserManager(cfg) as bm:
        ctx = await bm.open_context(platform, headless=False)
        page = await bm.get_page(ctx)
        closed = asyncio.Event()
        ctx.on("close", lambda *_: closed.set())
        try:
            await page.goto(scraper_cls.home_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            print(f"(page load issue: {exc}) - you can navigate manually.")
        print(
            f"\n A browser window opened for {scraper_cls.display}.\n"
            "  1. Log in (enter the OTP) - optional but more reliable\n"
            "  2. Set your DELIVERY ADDRESS / pincode (important for quick-commerce prices)\n"
            "  3. Solve any 'verify you are human' check if shown\n"
            "\n Then press ENTER here (or close the browser window) to save the session."
        )
        enter = asyncio.ensure_future(asyncio.to_thread(sys.stdin.readline))
        closer = asyncio.ensure_future(closed.wait())
        await asyncio.wait({enter, closer}, return_when=asyncio.FIRST_COMPLETED)
        ua, vp = bm.fingerprints[platform]
        meta = BrowserManager.read_meta(platform)
        meta.update(logged_in=True, user_agent=ua, viewport=list(vp), saved_at=datetime.now().isoformat(timespec="seconds"))
        if not closed.is_set():
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        BrowserManager.write_meta(platform, meta)
        # A manual visit also clears any bot-check cooldown.
        from core.database import Database

        db = Database(DB_PATH)
        db.kv_set(f"cooldown:{platform}", {"until": 0, "strikes": 0})
        db.close()
        print(f"\n ✅ Session saved in sessions/{platform}/ - the monitor will reuse it (headless).")
        if not enter.done():
            enter.cancel()
            print(" (press ENTER to exit)")


def cmd_reset_session(platform: str) -> None:
    from core.browser import BrowserManager

    d = BrowserManager.profile_dir(platform)
    if d.exists():
        shutil.rmtree(d)
        print(f"Deleted sessions/{platform}/ - {platform} will run as a fresh guest.")
    else:
        print(f"No saved session for {platform}.")


async def cmd_test_alert() -> None:
    from core.models import Product
    from core.notifier import Notifier

    cfg = load_config()
    n = Notifier(cfg)
    await n.start()
    sample = Product(
        platform="zepto",
        product_id="TEST",
        title="TEST ALERT - Sample Product 1kg (not a real deal)",
        price=49,
        mrp=499,
        url="https://www.zepto.com/",
    )
    ok = await n.send_alert(sample, "Zepto", "drop")
    await n.stop()
    print("✅ Test alert sent." if ok else "❌ Sending failed - check logs/monitor.log or rerun --setup.")


def cmd_export_cloud() -> None:
    """Print the values to paste into GitHub -> Settings -> Secrets and variables -> Actions."""
    cfg = load_config()
    n, loc = cfg["notifier"], cfg["location"]
    secrets = {"NOTIFIER_MODE": n["mode"]}
    if n["mode"] == "telethon":
        from telethon.sessions import SQLiteSession, StringSession

        from core.notifier import TELETHON_SESSION

        if not TELETHON_SESSION.with_suffix(".session").exists():
            sys.exit("No Telegram session found - run: python run.py --setup")
        sq = SQLiteSession(str(TELETHON_SESSION))
        string = StringSession.save(sq)  # reads auth key + DC from the local session file
        sq.close()
        secrets.update(TELEGRAM_API_ID=str(n["api_id"]), TELEGRAM_API_HASH=n["api_hash"], TELEGRAM_SESSION=string)
    else:
        secrets.update(TELEGRAM_BOT_TOKEN=n["bot_token"], TELEGRAM_CHAT_ID=str(n["chat_id"]))
    secrets.update(PINCODE=str(loc.get("pincode") or ""), CITY=loc.get("city") or "",
                   LATITUDE=str(loc.get("latitude") or ""), LONGITUDE=str(loc.get("longitude") or ""))
    print("\n  ⚠️  These are secrets. TELEGRAM_SESSION = full access to your Telegram account.")
    print("  Paste each one as a *Repository secret* on GitHub, then clear this terminal.\n")
    for k, v in secrets.items():
        print(f"  {k}\n    {v}\n")


def cmd_status() -> None:
    from core.browser import BrowserManager
    from core.database import Database

    cfg = load_config()
    print(f"Notifier: {cfg['notifier']['mode']}  |  Location: {cfg['location'].get('label') or cfg['location'].get('pincode') or 'not set'}")
    print(f"Rule: >= {cfg['monitor']['min_discount_pct']}% off or price <= ₹{cfg['monitor']['glitch_price_max']}, MRP >= ₹{cfg['monitor']['min_mrp']}")
    print("\nPlatforms:")
    db = Database(DB_PATH)
    for p in ALL_PLATFORMS:
        enabled = "on " if p in cfg["monitor"]["platforms"] else "off"
        meta = BrowserManager.read_meta(p)
        mode = f"logged-in ({meta.get('saved_at', '')})" if meta.get("logged_in") else "guest"
        cd = db.kv_get(f"cooldown:{p}") or {}
        cool = f"  COOLDOWN until {datetime.fromtimestamp(cd['until']).strftime('%H:%M')}" if cd.get("until", 0) > time.time() else ""
        print(f"  [{enabled}] {p:<17} {mode}{cool}")
    rows = db.recent_alerts(15)
    print(f"\nRecent alerts ({len(rows)}):")
    for plat, _pid, price, mrp, title, url, ts in rows:
        print(f"  {datetime.fromtimestamp(ts).strftime('%d %b %H:%M')}  {plat:<16} ₹{price:g} (MRP ₹{mrp:g})  {title[:60]}")
    db.close()


def run_scheduler(args) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    from core.monitor import run_cycle

    log = logging.getLogger("scheduler")
    cfg = load_config()
    interval = int(cfg["monitor"]["interval_minutes"])

    def job() -> None:
        # Config is re-read every cycle so edits to config.json apply without a restart.
        try:
            asyncio.run(run_cycle(load_config(), args.platforms, args.dry_run, args.debug, args.show_top))
        except Exception:  # noqa: BLE001
            log.exception("cycle crashed - will retry next interval")

    sched = BlockingScheduler(timezone="Asia/Kolkata")
    sched.add_job(
        job, "interval", minutes=interval, next_run_time=datetime.now().astimezone(),
        max_instances=1, coalesce=True, misfire_grace_time=120, id="scan",
    )

    def stop(*_):
        log.info("stopping...")
        sched.shutdown(wait=False)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    plats = ", ".join(args.platforms or cfg["monitor"]["platforms"])
    log.info("monitoring [%s] every %d min%s. Ctrl+C to stop.", plats, interval, " (DRY RUN)" if args.dry_run else "")
    sched.start()


def main() -> None:
    ap = argparse.ArgumentParser(description="80%+ price drop & pricing-glitch monitor with Telegram alerts")
    ap.add_argument("--setup", action="store_true", help="run the interactive setup wizard")
    ap.add_argument("--login", metavar="PLATFORM", choices=ALL_PLATFORMS, help="open a visible browser to log in / set address")
    ap.add_argument("--reset-session", metavar="PLATFORM", choices=ALL_PLATFORMS, help="delete a saved browser profile")
    ap.add_argument("--once", action="store_true", help="single scan then exit")
    ap.add_argument("--platforms", type=parse_platforms, help="comma list, e.g. zepto,blinkit")
    ap.add_argument("--dry-run", action="store_true", help="detect but don't send alerts")
    ap.add_argument("--show-top", type=int, default=0, metavar="N", help="print the N biggest discounts per platform")
    ap.add_argument("--test-alert", action="store_true", help="send a sample alert")
    ap.add_argument("--status", action="store_true", help="show sessions, cooldowns and recent alerts")
    ap.add_argument("--export-cloud-secrets", action="store_true", help="print the values for GitHub Actions secrets")
    ap.add_argument("--headful", action="store_true", help="show the browser while scanning")
    ap.add_argument("--debug", action="store_true", help="verbose logs + save screenshots/JSON to ./debug")
    args = ap.parse_args()
    setup_logging(args.debug)

    if args.setup or (not config_exists() and not (args.login or args.reset_session)):
        from wizard import run_wizard

        try:
            run_wizard()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled. Run again any time:  python run.py --setup")
            return
        if args.setup:
            return

    if args.login:
        asyncio.run(cmd_login(args.login))
        return
    if args.reset_session:
        cmd_reset_session(args.reset_session)
        return
    if args.test_alert:
        asyncio.run(cmd_test_alert())
        return
    if args.status:
        cmd_status()
        return
    if args.export_cloud_secrets:
        cmd_export_cloud()
        return

    if args.headful:
        import core.config

        core.config.RUNTIME_OVERRIDES["browser"] = {"headless": False}

    if args.once:
        from core.monitor import run_cycle

        summary = asyncio.run(run_cycle(load_config(), args.platforms, args.dry_run, args.debug, args.show_top))
        sys.exit(0 if summary["platforms"] else 1)
    run_scheduler(args)


if __name__ == "__main__":
    main()
