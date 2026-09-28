from __future__ import annotations

import re
from typing import Any, Dict, Optional

from playwright.async_api import BrowserContext, Page

from .base import BaseScraper


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "item"


class BlinkitScraper(BaseScraper):
    name = "blinkit"
    display = "Blinkit"
    home_url = "https://blinkit.com/"
    seed_urls = []
    product_link_pattern = r"blinkit\.com/prn/[^/?#]+/prid/(\d+)"
    category_link_pattern = r"blinkit\.com/(?:cn/[^?#]+/cid/\d+/\d+|dc/[^?#]+)"
    json_url_pattern = r"blinkit\.com/(?:v\d+/layout|v\d+/listing|v\d+/search|feed|v\d+/products?)"

    def build_url(self, ctx: Dict[str, Any]) -> Optional[str]:
        pid = str(ctx.get("id") or "")
        if pid.isdigit():
            return f"https://blinkit.com/prn/{ctx.get('slug') or _slugify(ctx['title'])}/prid/{pid}"
        return self.search_url(ctx["title"])

    def search_url(self, title: str) -> str:
        return f"https://blinkit.com/s/?q={self.q(title)}"

    async def seed_location(self, ctx: BrowserContext) -> None:
        loc = self.cfg["location"]
        if loc.get("latitude") is not None:
            await ctx.add_cookies([
                {"name": "gr_1_lat", "value": str(loc["latitude"]), "domain": ".blinkit.com", "path": "/"},
                {"name": "gr_1_lon", "value": str(loc["longitude"]), "domain": ".blinkit.com", "path": "/"},
            ])

    async def prepare_location(self, ctx: BrowserContext, page: Page) -> bool:
        # Location modal still showing -> use browser geolocation (= configured coordinates).
        return await self.click_detect_location(page)
