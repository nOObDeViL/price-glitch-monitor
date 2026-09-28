"""Stealth Playwright browser manager.

* One persistent Chromium profile per platform in ``./sessions/<platform>/``
  (cookies, localStorage and chosen delivery address survive between runs).
* Uses the real Google Chrome if installed (much harder to fingerprint than the
  bundled Chromium), otherwise Playwright's Chromium.
* playwright-stealth (v2 or v1 API) + an extra init script, randomized
  viewport, India locale/timezone, geolocation, and user-agent rotation that
  stays consistent with the real browser version (a UA claiming Chrome 120
  while client hints report Chrome 140 is itself a bot signal).

This lowers the chance of getting flagged; it does not solve CAPTCHAs. When a
challenge page is detected the scraper backs off and asks you to open the site
once with ``python run.py --login <platform>`` and clear it by hand.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform as _platform
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .config import SESSIONS_DIR

log = logging.getLogger("browser")

VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1600, 900), (1680, 1050), (1920, 1080), (1280, 800)]

_UA_TEMPLATES = {
    "mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36",
    "win": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36",
    "linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36",
}

STEALTH_FALLBACK_JS = """
if (navigator.webdriver) {
  Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});
}
window.chrome = window.chrome || {runtime: {}};
"""

BLOCK_MARKERS = [
    (re.compile(r"just a moment|checking your browser|cf-chl|challenge-platform", re.I), "Cloudflare challenge"),
    (re.compile(r"captcha-delivery|datadome", re.I), "DataDome challenge"),
    (re.compile(r"px-captcha|perimeterx|press (?:&amp; |& )?hold", re.I), "PerimeterX challenge"),
    (re.compile(r"enter the characters you see below|type the characters you see|api-services-support@amazon", re.I), "Amazon robot check"),
    (re.compile(r"access denied|request blocked|you have been blocked|unusual traffic", re.I), "Access denied"),
]


def _host_os() -> str:
    s = _platform.system().lower()
    return "mac" if s == "darwin" else "win" if s.startswith("win") else "linux"


def rotate_user_agent(browser_version: str) -> str:
    """Random UA that matches the real Chrome major version and host OS."""
    major = browser_version.split(".")[0] if browser_version else "140"
    v = f"{major}.0.{random.randint(6000, 7400)}.{random.randint(40, 220)}"
    return _UA_TEMPLATES[_host_os()].format(v=v)


class BrowserManager:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.b = cfg["browser"]
        self._pw: Optional[Playwright] = None
        self._channel: Optional[str] = None
        self._version: str = ""
        self.fingerprints: Dict[str, tuple] = {}  # platform -> (ua, viewport) of the last context

    async def __aenter__(self) -> "BrowserManager":
        self._pw = await async_playwright().start()
        await self._probe()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._pw:
            await self._pw.stop()

    async def _probe(self) -> None:
        """Prefer installed Google Chrome; remember the browser version for UA generation."""
        for channel in ("chrome", None):
            try:
                kwargs = {"headless": True}
                if channel:
                    kwargs["channel"] = channel
                br = await self._pw.chromium.launch(**kwargs)
                self._version = br.version
                await br.close()
                self._channel = channel
                log.debug("Using %s %s", channel or "chromium", self._version)
                return
            except Exception:  # noqa: BLE001 - channel not installed
                continue
        raise RuntimeError("No Chromium available. Run: python -m playwright install chromium")

    # ------------------------------------------------------------------ meta
    @staticmethod
    def profile_dir(platform: str) -> Path:
        return SESSIONS_DIR / platform

    @classmethod
    def read_meta(cls, platform: str) -> Dict[str, Any]:
        f = cls.profile_dir(platform) / "profile.json"
        if f.exists():
            try:
                return json.loads(f.read_text())
            except ValueError:
                pass
        return {}

    @classmethod
    def write_meta(cls, platform: str, meta: Dict[str, Any]) -> None:
        d = cls.profile_dir(platform)
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.json").write_text(json.dumps(meta, indent=2))

    @classmethod
    def is_logged_in(cls, platform: str) -> bool:
        return bool(cls.read_meta(platform).get("logged_in"))

    # --------------------------------------------------------------- context
    async def open_context(self, platform: str, headless: Optional[bool] = None) -> BrowserContext:
        loc = self.cfg["location"]
        meta = self.read_meta(platform)
        logged_in = bool(meta.get("logged_in"))

        # Logged-in profiles keep a stable fingerprint (UA/viewport changes can
        # invalidate sessions); guest profiles rotate every run.
        if logged_in and meta.get("user_agent", "").find(f"Chrome/{self._version.split('.')[0]}.") != -1:
            ua, vp = meta["user_agent"], tuple(meta.get("viewport", random.choice(VIEWPORTS)))
        else:
            ua, vp = rotate_user_agent(self._version), random.choice(VIEWPORTS)
            if logged_in:
                meta.update(user_agent=ua, viewport=list(vp))
                self.write_meta(platform, meta)

        kwargs: Dict[str, Any] = dict(
            user_data_dir=str(self.profile_dir(platform)),
            headless=self.b["headless"] if headless is None else headless,
            user_agent=ua,
            viewport={"width": vp[0], "height": vp[1]},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7"},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
        )
        if self._channel:
            kwargs["channel"] = self._channel
        if loc.get("latitude") is not None and loc.get("longitude") is not None:
            kwargs["geolocation"] = {"latitude": float(loc["latitude"]), "longitude": float(loc["longitude"]), "accuracy": 50}
            kwargs["permissions"] = ["geolocation"]
        if self.b.get("proxy"):
            kwargs["proxy"] = {"server": self.b["proxy"]}

        self.fingerprints[platform] = (ua, vp)
        self.profile_dir(platform).mkdir(parents=True, exist_ok=True)
        ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        ctx.set_default_timeout(int(self.b["page_timeout_ms"]))
        ctx.set_default_navigation_timeout(int(self.b["page_timeout_ms"]))
        await self._apply_stealth(ctx, ua)
        if self.b.get("block_media", True):
            await ctx.route(re.compile(r"\.(woff2?|ttf|otf|mp4|webm|m3u8|mp3)(\?|$)", re.I), lambda r: r.abort())
        return ctx

    async def _apply_stealth(self, ctx: BrowserContext, ua: str) -> None:
        applied = False
        nav_platform = {"mac": "MacIntel", "win": "Win32", "linux": "Linux x86_64"}[_host_os()]
        try:
            from playwright_stealth import Stealth  # v2.x

            await Stealth(
                navigator_languages_override=("en-IN", "en"),
                navigator_platform_override=nav_platform,  # default 'Win32' would contradict a Mac UA
                # These three patches are themselves fingerprintable: Swiggy's AWS WAF
                # blocks on each of them (tested). The UA is already set consistently
                # for both the HTTP header and navigator.userAgent via user_agent=.
                chrome_load_times=False,
                media_codecs=False,
                navigator_user_agent=False,
                init_scripts_only=True,
            ).apply_stealth_async(ctx)
            applied = True
        except ImportError:
            try:
                from playwright_stealth import stealth_async  # v1.x

                for pg in ctx.pages:
                    await stealth_async(pg)
                ctx.on("page", lambda pg: asyncio.ensure_future(stealth_async(pg)))
                applied = True
            except ImportError:
                pass
        except TypeError:
            from playwright_stealth import Stealth

            await Stealth().apply_stealth_async(ctx)
            applied = True
        if not applied:
            log.debug("playwright-stealth not installed; using built-in fallback script")
        await ctx.add_init_script(STEALTH_FALLBACK_JS)

    async def get_page(self, ctx: BrowserContext) -> Page:
        return ctx.pages[0] if ctx.pages else await ctx.new_page()


# ---------------------------------------------------------------------------
# Human-ish behaviour helpers
# ---------------------------------------------------------------------------

async def jitter(cfg: Dict[str, Any]) -> None:
    b = cfg["browser"]
    await asyncio.sleep(random.uniform(float(b["min_delay"]), float(b["max_delay"])))


async def human_scroll(page: Page, times: int) -> None:
    try:
        vp = page.viewport_size or {"width": 1366, "height": 768}
        await page.mouse.move(random.randint(100, vp["width"] - 100), random.randint(100, vp["height"] - 100))
        for _ in range(times):
            await page.mouse.wheel(0, random.randint(int(vp["height"] * 0.6), int(vp["height"] * 1.2)))
            await asyncio.sleep(random.uniform(0.6, 1.6))
    except Exception:  # noqa: BLE001 - page may navigate/close mid-scroll
        pass


async def detect_block(page: Page) -> Optional[str]:
    try:
        title = await page.title()
        body = await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 3000)")
        html_head = (await page.content())[:20000]
    except Exception:  # noqa: BLE001
        return None
    hay = f"{title}\n{body}"
    for rx, name in BLOCK_MARKERS:
        if rx.search(hay) or (name != "Access denied" and rx.search(html_head)):
            # Real pages occasionally mention these words; require a mostly-empty page.
            if len(body) < 2500:
                return name
    return None


def now_ts() -> float:
    return time.time()
