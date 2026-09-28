"""Configuration loading/saving.

All settings live in ``config.json`` (created by the setup wizard, chmod 600).
Any value can be overridden by an environment variable or a ``.env`` file,
which is handy for servers/Docker:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
PINCODE, LATITUDE, LONGITUDE, HEADLESS, INTERVAL_MINUTES, ...
"""
from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
SESSIONS_DIR = ROOT / "sessions"
DEBUG_DIR = ROOT / "debug"
LOG_DIR = ROOT / "logs"
DB_PATH = ROOT / "deals.db"

ALL_PLATFORMS = [
    "amazon",
    "flipkart",
    "flipkart_minutes",
    "blinkit",
    "zepto",
    "instamart",
    "jiomart",
]

DEFAULTS: Dict[str, Any] = {
    "notifier": {
        # "bot"      -> Telegram Bot API (recommended; supports a Buy button)
        # "telethon" -> your own Telegram account, alerts land in Saved Messages
        "mode": "bot",
        "bot_token": "",
        "chat_id": "",
        "api_id": "",
        "api_hash": "",
        "phone": "",
        # Optional second channel via CallMeBot (free, messages your own number)
        "whatsapp_enabled": False,
        "whatsapp_phone": "",
        "whatsapp_apikey": "",
    },
    "location": {
        "label": "",
        "pincode": "",
        "city": "",
        "latitude": None,
        "longitude": None,
    },
    "monitor": {
        "interval_minutes": 5,
        "min_discount_pct": 80.0,
        "glitch_price_max": 1.0,
        "dedupe_hours": 12,
        # Ignore cheap items: ₹12 -> ₹2 is technically 83% off but not interesting.
        "min_mrp": 99.0,
        "max_mrp": 1_000_000.0,
        # Suppress "permanent" 80% discounts (inflated MRP) once we have history.
        "inflated_mrp_check": True,
        # Printed-MRP platforms: an 80%+ discount there is meaningful on first sight.
        "trusted_mrp_platforms": ["blinkit", "zepto", "instamart", "jiomart", "flipkart_minutes"],
        # Other platforms (Amazon/Flipkart marketplaces) in "strict" mode need
        # >= extreme_discount_pct, price <= glitch_price_max, or an observed price
        # drop. Set to "all" to be alerted on every 80%+ listing (very noisy).
        "marketplace_mode": "strict",
        "extreme_discount_pct": 95.0,
        # First scan of each page records existing deals silently (no flood of stale
        # "permanent" discounts); new deals and real drops alert from then on.
        "baseline_first_scan": True,
        "blocklist_keywords": [
            "free gift", "freebie", "sample", "not for sale", "tester",
            "gift card", "voucher", "e-gift", "subscription", "membership",
            "cashback", "coupon",
        ],
        "platforms": ALL_PLATFORMS,
        "concurrency": 4,
        # Hard cap per platform per cycle so one hung page can't stall the monitor.
        "platform_timeout_s": 240,
        "max_category_pages": 3,
    },
    "browser": {
        "headless": True,
        "min_delay": 2.0,
        "max_delay": 5.0,
        "page_timeout_ms": 45000,
        "scrolls": 4,
        # Optional: "http://user:pass@host:port"
        "proxy": "",
    },
    # Extra URLs per platform (added to the built-in deal/category pages).
    "extra_urls": {p: [] for p in ALL_PLATFORMS},
}

# Set by CLI flags (e.g. --headful); applied on top of file + env.
RUNTIME_OVERRIDES: Dict[str, Any] = {}

ENV_MAP = {
    "TELEGRAM_BOT_TOKEN": ("notifier", "bot_token", str),
    "TELEGRAM_CHAT_ID": ("notifier", "chat_id", str),
    "NOTIFIER_MODE": ("notifier", "mode", str),
    "TELEGRAM_API_ID": ("notifier", "api_id", str),
    "TELEGRAM_API_HASH": ("notifier", "api_hash", str),
    "TELEGRAM_SESSION": ("notifier", "session_string", str),
    "WHATSAPP_PHONE": ("notifier", "whatsapp_phone", str),
    "WHATSAPP_APIKEY": ("notifier", "whatsapp_apikey", str),
    "PINCODE": ("location", "pincode", str),
    "CITY": ("location", "city", str),
    "LATITUDE": ("location", "latitude", float),
    "LONGITUDE": ("location", "longitude", float),
    "HEADLESS": ("browser", "headless", lambda v: v.strip().lower() in ("1", "true", "yes")),
    "PROXY": ("browser", "proxy", str),
    "INTERVAL_MINUTES": ("monitor", "interval_minutes", int),
    "MIN_DISCOUNT_PCT": ("monitor", "min_discount_pct", float),
    "MIN_MRP": ("monitor", "min_mrp", float),
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def config_exists() -> bool:
    if CONFIG_PATH.exists():
        return True
    load_dotenv(ROOT / ".env")
    bot = os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
    user = os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_HASH") and os.getenv("TELEGRAM_SESSION")
    return bool(bot or user)


def load_config() -> Dict[str, Any]:
    load_dotenv(ROOT / ".env")
    data: Dict[str, Any] = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    cfg = _deep_merge(DEFAULTS, data)
    for env_key, (section, key, cast) in ENV_MAP.items():
        raw = os.getenv(env_key)
        if raw not in (None, ""):
            try:
                cfg[section][key] = cast(raw)
            except ValueError:
                pass
    return _deep_merge(cfg, RUNTIME_OVERRIDES)


def save_config(cfg: Dict[str, Any]) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 600: secrets inside
    tmp.replace(CONFIG_PATH)


def ensure_dirs() -> None:
    for d in (SESSIONS_DIR, DEBUG_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
