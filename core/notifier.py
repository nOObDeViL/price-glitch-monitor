"""Alert delivery: Telegram Bot API, Telegram user account (Telethon), WhatsApp (CallMeBot)."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from .config import SESSIONS_DIR
from .models import Product

log = logging.getLogger("notifier")

TG_API = "https://api.telegram.org/bot{token}/{method}"
TELETHON_SESSION = SESSIONS_DIR / "telegram_user"
VERIFY_TEXT = "✅ Connected! Price glitch alerts will appear here."


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def format_alert_html(p: Product, platform_name: str, kind: str, threshold: float = 80) -> str:
    def e(text: str) -> str:
        return html.escape(text, quote=False)

    header = f"🚨 <b>{threshold:g}%+ PRICE DROP / GLITCH DETECTED!</b>" if threshold else "🚨 <b>PRICE ERROR / GLITCH DETECTED!</b>"
    label = "Glitch Price"
    lines = [
        header,
        "",
        f"📦 <b>Product:</b> {e(p.title[:300])}",
        f"🏢 <b>Platform:</b> {e(platform_name)}",
        f"💰 <b>{label}:</b> ₹{_fmt_money(p.price)} (MRP: ₹{_fmt_money(p.mrp)})",
        f"🔥 <b>Discount:</b> {p.discount_pct:.0f}% OFF",
    ]
    if p.extra.get("note"):
        lines.append(e(p.extra["note"]))
    lines += [
        "",
        f'🔗 <b>Buy Link:</b> <a href="{html.escape(p.url, quote=True)}">{e(p.url[:200])}</a>',
    ]
    return "\n".join(lines)


def format_alert_plain(p: Product, platform_name: str, kind: str, threshold: float = 80) -> str:
    label = "Glitch Price"
    return "\n".join([
        f"🚨 *{threshold:g}%+ PRICE DROP / GLITCH DETECTED!*" if threshold else "🚨 *PRICE ERROR / GLITCH DETECTED!*",
        "",
        f"📦 Product: {p.title[:300]}",
        f"🏢 Platform: {platform_name}",
        f"💰 {label}: ₹{_fmt_money(p.price)} (MRP: ₹{_fmt_money(p.mrp)})",
        f"🔥 Discount: {p.discount_pct:.0f}% OFF",
        p.extra.get("note", ""),
        "",
        f"🔗 Buy Link: {p.url}",
    ]).replace("\n\n\n", "\n\n")


# --------------------------------------------------------------------------
# Telegram Bot API (sync helpers, also used by the wizard)
# --------------------------------------------------------------------------

def tg_call(token: str, method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
    for attempt in range(4):
        try:
            r = requests.post(TG_API.format(token=token, method=method), json=payload or {}, timeout=timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 3:
                return {"ok": False, "description": str(exc)}
            time.sleep(2 ** attempt)
            continue
        if data.get("ok"):
            return data
        retry = (data.get("parameters") or {}).get("retry_after")
        if data.get("error_code") == 429 and retry:
            time.sleep(float(retry) + 0.5)
            continue
        return data
    return {"ok": False, "description": "retries exhausted"}


def tg_send_message(token: str, chat_id: str, text: str, button_url: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if button_url and button_url.startswith(("http://", "https://")):
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "🛒 Buy Now", "url": button_url}]]}
    res = tg_call(token, "sendMessage", payload)
    if not res.get("ok") and "reply_markup" in payload:
        # A URL Telegram dislikes shouldn't cost us the alert.
        payload.pop("reply_markup")
        res = tg_call(token, "sendMessage", payload)
    return res


def whatsapp_send(phone: str, apikey: str, text: str) -> bool:
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": text, "apikey": apikey},
            timeout=30,
        )
        ok = r.status_code == 200 and "error" not in r.text.lower()[:300]
        if not ok:
            log.warning("WhatsApp send failed: %s %s", r.status_code, r.text[:200])
        return ok
    except requests.RequestException as exc:
        log.warning("WhatsApp send failed: %s", exc)
        return False


# --------------------------------------------------------------------------
# Unified async notifier used by the monitor
# --------------------------------------------------------------------------

class Notifier:
    def __init__(self, cfg: Dict[str, Any]):
        self.n = cfg["notifier"]
        self.threshold = float(cfg["monitor"]["min_discount_pct"])
        if cfg["monitor"].get("alert_mode", "errors") in ("glitch", "errors"):
            self.threshold = 0  # header becomes "PRICE ERROR / GLITCH DETECTED!"
        self._client = None  # Telethon client
        self._send_lock: Optional[asyncio.Lock] = None  # created inside the running loop (py3.9)

    async def start(self) -> None:
        if self.n["mode"] == "telethon":
            from telethon import TelegramClient

            session: object = str(TELETHON_SESSION)
            if self.n.get("session_string"):  # cloud runs (GitHub Actions): session from a secret
                from telethon.sessions import StringSession

                session = StringSession(self.n["session_string"])
            self._client = TelegramClient(session, int(self.n["api_id"]), self.n["api_hash"])
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise RuntimeError("Telegram user session not authorized. Run: python run.py --setup")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    async def send_text(self, text_html: str, button_url: Optional[str] = None, plain: Optional[str] = None) -> bool:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            ok = await self._send_telegram(text_html, button_url)
            if self.n.get("whatsapp_enabled") and self.n.get("whatsapp_phone") and self.n.get("whatsapp_apikey"):
                await asyncio.to_thread(
                    whatsapp_send, self.n["whatsapp_phone"], self.n["whatsapp_apikey"], plain or text_html
                )
            await asyncio.sleep(1.1)  # stay well under Telegram's per-chat rate limit
            return ok

    async def _send_telegram(self, text_html: str, button_url: Optional[str]) -> bool:
        if self.n["mode"] == "telethon":
            if self._client is None:
                await self.start()
            try:
                await self._client.send_message("me", text_html, parse_mode="html", link_preview=True)
                return True
            except Exception as exc:  # noqa: BLE001 - network/flood errors
                log.error("Telethon send failed: %s", exc)
                return False
        if not self.n.get("bot_token") or not self.n.get("chat_id"):
            log.error("Telegram bot not configured. Run: python run.py --setup")
            return False
        res = await asyncio.to_thread(tg_send_message, self.n["bot_token"], str(self.n["chat_id"]), text_html, button_url)
        if not res.get("ok"):
            log.error("Telegram send failed: %s", res.get("description"))
        return bool(res.get("ok"))

    async def send_alert(self, p: Product, platform_name: str, kind: str) -> bool:
        return await self.send_text(
            format_alert_html(p, platform_name, kind, self.threshold),
            button_url=p.url,
            plain=format_alert_plain(p, platform_name, kind, self.threshold),
        )

    async def send_many(self, items: List[tuple]) -> int:
        sent = 0
        for p, name, kind in items:
            if await self.send_alert(p, name, kind):
                sent += 1
        return sent
