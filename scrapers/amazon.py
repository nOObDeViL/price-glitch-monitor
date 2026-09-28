from __future__ import annotations

import re
from typing import Any, Dict, Optional

from playwright.async_api import BrowserContext, Page

from core.browser import BrowserManager

from .base import BaseScraper


class AmazonScraper(BaseScraper):
    name = "amazon"
    display = "Amazon"
    home_url = "https://www.amazon.in/deals"
    # Deal hub + "80% off or more" filtered listings across broad departments.
    seed_urls = [
        "https://www.amazon.in/s?i=electronics&rh=n%3A976419031&pct-off=80-&s=date-desc-rank",
        "https://www.amazon.in/s?i=computers&rh=n%3A976392031&pct-off=80-&s=date-desc-rank",
        "https://www.amazon.in/s?i=kitchen&rh=n%3A976442031&pct-off=80-&s=date-desc-rank",
        "https://www.amazon.in/s?i=apparel&rh=n%3A1571271031&pct-off=80-&s=date-desc-rank",
        "https://www.amazon.in/s?i=beauty&rh=n%3A1355016031&pct-off=80-&s=date-desc-rank",
        "https://www.amazon.in/s?i=shoes&rh=n%3A1571283031&pct-off=80-&s=date-desc-rank",
        "https://www.amazon.in/s?i=grocery&rh=n%3A2454178031&pct-off=80-&s=date-desc-rank",
        "https://www.amazon.in/s?i=toys&rh=n%3A1350380031&pct-off=80-&s=date-desc-rank",
    ]
    product_link_pattern = r"amazon\.in/(?:[^/?#]+/)?(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})"
    category_link_pattern = None  # seed list rotates instead (see below)
    json_url_pattern = r"amazon\.in/(?:d2b/api|api/)"
    discover_on_home = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # 8 filtered listings is too many for one 5-minute cycle: rotate through them.
        pool = list(self.seed_urls)
        n = max(2, int(self.cfg["monitor"]["max_category_pages"]))
        self.seed_urls = self._rotate(pool, n)

    def build_url(self, ctx: Dict[str, Any]) -> Optional[str]:
        pid = str(ctx.get("id") or "")
        if re.fullmatch(r"[A-Z0-9]{10}", pid):
            return f"https://www.amazon.in/dp/{pid}"
        for d in ctx.get("chain", []):
            asin = d.get("asin") if isinstance(d, dict) else None
            if isinstance(asin, str) and re.fullmatch(r"[A-Z0-9]{10}", asin):
                return f"https://www.amazon.in/dp/{asin}"
        return super().build_url(ctx)

    def clean_url(self, url: str) -> str:
        m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url)
        return f"https://www.amazon.in/dp/{m.group(1)}" if m else url

    def search_url(self, title: str) -> str:
        return f"https://www.amazon.in/s?k={self.q(title)}"

    async def prepare_location(self, ctx: BrowserContext, page: Page) -> bool:
        pincode = str(self.cfg["location"].get("pincode") or "").strip()
        meta = BrowserManager.read_meta(self.name)
        if not pincode or meta.get("pincode_set") == pincode:
            return False
        try:
            await page.click("#nav-global-location-popover-link", timeout=5000)
            box = page.locator("#GLUXZipUpdateInput")
            await box.wait_for(timeout=6000)
            await box.fill(pincode)
            await page.click("#GLUXZipUpdate input, #GLUXZipUpdate", timeout=4000)
            await page.wait_for_timeout(2500)
            for sel in ("#GLUXConfirmClose", ".a-popover-footer #GLUXConfirmClose", "button[name='glowDoneButton']"):
                try:
                    await page.click(sel, timeout=1500)
                    break
                except Exception:  # noqa: BLE001
                    continue
            meta["pincode_set"] = pincode
            BrowserManager.write_meta(self.name, meta)
            self.log.info("delivery pincode set to %s", pincode)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.debug("could not set Amazon pincode: %s", exc)
            return False
