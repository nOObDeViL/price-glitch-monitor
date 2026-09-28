"""Interactive first-run setup: Telegram (bot or personal account), WhatsApp, location, rules."""
from __future__ import annotations

import asyncio
import getpass
import re
import sys
import time
from typing import Any, Dict, Optional, Tuple

import requests

from core.config import ALL_PLATFORMS, CONFIG_PATH, ensure_dirs, load_config, save_config
from core.notifier import TELETHON_SESSION, VERIFY_TEXT, tg_call, tg_send_message, whatsapp_send

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = DIM = GREEN = RED = YELLOW = RESET = ""


def say(msg: str = "") -> None:
    print(msg)


def ok(msg: str) -> None:
    print(f"{GREEN}✔ {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"{YELLOW}! {msg}{RESET}")


def err(msg: str) -> None:
    print(f"{RED}✘ {msg}{RESET}")


def ask(prompt: str, default: Optional[str] = None, secret: bool = False) -> str:
    suffix = f" {DIM}[{default}]{RESET}" if default not in (None, "") else ""
    while True:
        text = f"{BOLD}?{RESET} {prompt}{suffix}: "
        val = getpass.getpass(text) if secret else input(text)
        val = val.strip()
        if val:
            return val
        if default is not None:
            return str(default)


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"{BOLD}?{RESET} {prompt} {DIM}[{d}]{RESET}: ").strip().lower()
    return default if not val else val.startswith("y")


def choose(prompt: str, options: list, default: int = 1) -> int:
    for i, o in enumerate(options, 1):
        say(f"   {i}) {o}")
    while True:
        v = ask(prompt, str(default))
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v)


# ------------------------------------------------------------------ Telegram bot

def setup_bot(n: Dict[str, Any]) -> bool:
    say(f"""
{BOLD}Create your alert bot (takes ~1 minute):{RESET}
  1. Open Telegram and message {BOLD}@BotFather{RESET}  (https://t.me/BotFather)
  2. Send  /newbot  and pick any name + a username ending in 'bot'
  3. BotFather replies with a token like  {DIM}123456789:AAH...{RESET}
""")
    while True:
        token = ask("Paste the bot token", n.get("bot_token") or None, secret=False)
        me = tg_call(token, "getMe")
        if me.get("ok"):
            username = me["result"]["username"]
            ok(f"Token valid - bot is @{username}")
            break
        err(f"Telegram rejected that token ({me.get('description', 'unknown error')}). Try again.")
    n["bot_token"] = token

    chat_id = detect_chat(token, username)
    if not chat_id:
        chat_id = ask("Enter your chat id manually (message @userinfobot to get it)")
    n["chat_id"] = str(chat_id)

    res = tg_send_message(token, n["chat_id"], VERIFY_TEXT)
    if res.get("ok"):
        ok("Verification message sent - check Telegram!")
        return True
    err(f"Could not send the verification message: {res.get('description')}")
    return False


def detect_chat(token: str, username: str, timeout_s: int = 180) -> Optional[str]:
    tg_call(token, "deleteWebhook")  # getUpdates doesn't work while a webhook is set
    # Skip anything already queued so we lock onto *your* fresh message.
    pending = tg_call(token, "getUpdates", {"timeout": 0})
    offset = (pending.get("result") or [{}])[-1].get("update_id", -1) + 1 if pending.get("result") else None
    say(f"""
  Now open {BOLD}https://t.me/{username}{RESET} and press {BOLD}START{RESET} (or send it any message).
  {DIM}(Want alerts in a group instead? Add the bot to the group and send a message there.){RESET}
  Waiting up to {timeout_s // 60} minutes...""")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        payload: Dict[str, Any] = {"timeout": 20, "allowed_updates": ["message", "my_chat_member", "channel_post"]}
        if offset is not None:
            payload["offset"] = offset
        upd = tg_call(token, "getUpdates", payload, timeout=30)
        for u in upd.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                who = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username")
                ok(f"Detected chat: {who} (id {chat['id']})")
                tg_call(token, "getUpdates", {"offset": offset, "timeout": 0})  # acknowledge
                return str(chat["id"])
    warn("Didn't see a message in time.")
    return None


# ------------------------------------------------------- Telegram personal account

def setup_telethon(n: Dict[str, Any]) -> bool:
    say(f"""
{BOLD}Alerts from your own account into "Saved Messages":{RESET}
  Telegram requires a free API key for this:
  1. Go to https://my.telegram.org  -> log in -> "API development tools"
  2. Create an app (any name) and copy the {BOLD}api_id{RESET} and {BOLD}api_hash{RESET}
  {DIM}Note: accounts can't send buttons, so alerts will have a plain link instead.{RESET}
""")
    n["api_id"] = ask("api_id", n.get("api_id") or None)
    n["api_hash"] = ask("api_hash", n.get("api_hash") or None)
    n["phone"] = ask("Phone number with country code (e.g. +9198xxxxxxx)", n.get("phone") or None)
    try:
        return asyncio.run(_telethon_login(n))
    except Exception as exc:  # noqa: BLE001
        err(f"Telegram login failed: {exc}")
        return False


async def _telethon_login(n: Dict[str, Any]) -> bool:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    ensure_dirs()
    client = TelegramClient(str(TELETHON_SESSION), int(n["api_id"]), n["api_hash"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            await client.send_code_request(n["phone"])
            code = ask("Enter the login code Telegram just sent you")
            try:
                await client.sign_in(n["phone"], code)
            except SessionPasswordNeededError:
                pw = ask("Two-step verification password", secret=True)
                await client.sign_in(password=pw)
        me = await client.get_me()
        ok(f"Logged in as {me.first_name} (@{me.username or '-'})")
        await client.send_message("me", VERIFY_TEXT)
        ok("Verification message sent to your Saved Messages!")
        return True
    finally:
        await client.disconnect()


# ------------------------------------------------------------------- WhatsApp

def setup_whatsapp(n: Dict[str, Any]) -> None:
    say(f"""
{BOLD}WhatsApp (optional, via the free CallMeBot service):{RESET}
  Telegram is the easier and richer option (buy button, instant, no limits).
  WhatsApp has no free official API for personal numbers; CallMeBot relays
  messages to *your own* number:
  1. Follow https://www.callmebot.com/blog/free-api-whatsapp-messages/
     (save their number, send them "I allow callmebot to send me messages")
  2. They reply with your personal apikey.
""")
    if not ask_yes("Set up WhatsApp alerts too?", default=False):
        n["whatsapp_enabled"] = False
        return
    n["whatsapp_phone"] = ask("Your WhatsApp number with country code (e.g. +9198xxxxxxx)", n.get("whatsapp_phone") or None)
    n["whatsapp_apikey"] = ask("CallMeBot apikey", n.get("whatsapp_apikey") or None)
    if whatsapp_send(n["whatsapp_phone"], n["whatsapp_apikey"], VERIFY_TEXT):
        ok("WhatsApp test sent.")
        n["whatsapp_enabled"] = True
    else:
        warn("WhatsApp test failed - leaving it disabled (you can rerun --setup).")
        n["whatsapp_enabled"] = False


# ------------------------------------------------------------------- Location

def geocode(query: str) -> Optional[Tuple[float, float, str]]:
    params: Dict[str, Any] = {"format": "json", "limit": 1, "countrycodes": "in", "addressdetails": 1}
    if re.fullmatch(r"\d{6}", query):
        params["postalcode"] = query
    else:
        params["q"] = query
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params=params,
            headers={"User-Agent": "price-glitch-monitor/1.0 (personal use setup wizard)"},
            timeout=20,
        )
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not data:
        return None
    hit = data[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", query)


def reverse_pincode(lat: float, lon: float) -> Tuple[str, str]:
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "json", "lat": lat, "lon": lon, "addressdetails": 1},
            headers={"User-Agent": "price-glitch-monitor/1.0 (personal use setup wizard)"},
            timeout=20,
        )
        addr = r.json().get("address", {})
        return addr.get("postcode", ""), addr.get("city") or addr.get("town") or addr.get("state_district") or ""
    except (requests.RequestException, ValueError):
        return "", ""


def setup_location(loc: Dict[str, Any]) -> None:
    say(f"""
{BOLD}Delivery location{RESET}
  Quick-commerce apps (Blinkit, Zepto, Instamart, Flipkart Minutes, JioMart)
  price by nearby dark store, so this should be where you'd order to.
  Enter a {BOLD}6-digit pincode{RESET}, an {BOLD}area/city{RESET} (e.g. "Koramangala, Bengaluru"),
  or exact {BOLD}coordinates{RESET} "12.9352,77.6245".""")
    current = loc.get("pincode") or loc.get("label") or None
    while True:
        q = ask("Location", current)
        m = re.fullmatch(r"\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*", q)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            pin, city = reverse_pincode(lat, lon)
            label = f"{city} {pin}".strip() or q
        else:
            say(f"  {DIM}looking up '{q}'...{RESET}")
            hit = geocode(q)
            if not hit:
                warn("Couldn't find that. Try a pincode, a bigger area name, or lat,lng.")
                continue
            lat, lon, label = hit
            pin, city = (q, "") if re.fullmatch(r"\d{6}", q) else ("", "")
            rp, rc = reverse_pincode(lat, lon)
            pin, city = pin or rp, city or rc
        say(f"  -> {label}\n     ({lat:.5f}, {lon:.5f})  pincode: {pin or '?'}")
        if ask_yes("Is that right?"):
            if not pin:
                pin = ask("Pincode (used by Amazon/Flipkart/JioMart)", "")
            loc.update(label=label[:120], pincode=pin, city=city, latitude=round(lat, 6), longitude=round(lon, 6))
            ok("Location saved.")
            return


# ------------------------------------------------------------------ Monitoring

def setup_rules(cfg: Dict[str, Any]) -> None:
    m, b = cfg["monitor"], cfg["browser"]
    say(f"\n{BOLD}Which platforms should be monitored?{RESET}  {DIM}({', '.join(ALL_PLATFORMS)}){RESET}")
    while True:
        raw = ask("Comma-separated list or 'all'", "all" if set(m["platforms"]) == set(ALL_PLATFORMS) else ",".join(m["platforms"]))
        chosen = ALL_PLATFORMS if raw.lower() == "all" else [p.strip().lower() for p in raw.split(",") if p.strip()]
        bad = [p for p in chosen if p not in ALL_PLATFORMS]
        if not bad:
            m["platforms"] = list(chosen)
            break
        warn(f"Unknown: {', '.join(bad)}")

    if ask_yes("Keep the default rules (>=80% off or price <= ₹1, MRP >= ₹99, every 5 min)?"):
        return
    m["min_discount_pct"] = float(ask("Minimum discount %", str(m["min_discount_pct"])))
    m["min_mrp"] = float(ask("Ignore items with MRP below ₹", str(m["min_mrp"])))
    m["interval_minutes"] = max(1, int(ask("Scan every N minutes", str(m["interval_minutes"]))))
    m["dedupe_hours"] = float(ask("Don't repeat the same product+price within N hours", str(m["dedupe_hours"])))
    b["headless"] = ask_yes("Run the browser hidden (headless)?", bool(b["headless"]))


# ------------------------------------------------------------------------ main

def run_wizard() -> Dict[str, Any]:
    ensure_dirs()
    cfg = load_config()
    say(f"""
{BOLD}══════════════════════════════════════════════════════{RESET}
{BOLD}  🚨 Price Glitch Monitor - setup{RESET}
{BOLD}══════════════════════════════════════════════════════{RESET}
  Scans Amazon, Flipkart, Flipkart Minutes, Blinkit, Zepto,
  Instamart and JioMart for 80%+ drops and pricing errors,
  and pings you on Telegram. Press Enter to accept [defaults].
""")
    n = cfg["notifier"]
    have = n.get("mode") == "bot" and n.get("bot_token") and n.get("chat_id")
    have = have or (n.get("mode") == "telethon" and TELETHON_SESSION.with_suffix(".session").exists())
    if have and not ask_yes("Telegram is already connected. Reconfigure it?", default=False):
        pass
    else:
        say(f"{BOLD}Where should alerts go?{RESET}")
        pick = choose("Choose", [
            "Telegram bot  (recommended: 1-minute setup, 'Buy Now' button)",
            "My own Telegram account -> Saved Messages  (needs api_id from my.telegram.org)",
        ])
        n["mode"] = "bot" if pick == 1 else "telethon"
        while not (setup_bot(n) if pick == 1 else setup_telethon(n)):
            if not ask_yes("Try again?"):
                warn("Continuing without a working Telegram connection - rerun: python run.py --setup")
                break
    save_config(cfg)  # checkpoint so a later Ctrl+C doesn't lose the token

    setup_whatsapp(n)
    save_config(cfg)
    setup_location(cfg["location"])
    save_config(cfg)
    setup_rules(cfg)
    save_config(cfg)
    ok(f"Configuration saved to {CONFIG_PATH.name} (readable only by you).")

    say(f"""
{BOLD}Recommended next step for quick-commerce apps:{RESET}
  Log in once / pick your address in a real browser window, e.g.
     python run.py --login zepto
     python run.py --login blinkit
     python run.py --login instamart
  Sessions are saved in ./sessions/ and reused automatically.

{BOLD}Start monitoring:{RESET}  python run.py
""")
    return cfg


if __name__ == "__main__":
    try:
        run_wizard()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
