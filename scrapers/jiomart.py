from __future__ import annotations

import re
from typing import Any, Dict, Optional

from playwright.async_api import BrowserContext, Page

from .base import BaseScraper


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "item"


class JioMartScraper(BaseScraper):
    name = "jiomart"
    display = "JioMart"
    home_url = "https://www.jiomart.com/"
    seed_urls = [
        "https://www.jiomart.com/sections/all-offers",
        "https://www.jiomart.com/sections/deal-of-the-day",
    ]
    product_link_pattern = r"jiomart\.com/(?:product/[a-z0-9-]*?-(\d{5,})$|p/[^?#]+/(\d{5,}))"
    category_link_pattern = r"jiomart\.com/(?:c/(?!products-view-all)[^?#]+|sections/[^?#]+|collection/[^?#]+)"
    json_url_pattern = r"jiomart\.com/(?:ext/|api/service/application/(?:catalog|theme|content))"

    def build_url(self, ctx: Dict[str, Any]) -> Optional[str]:
        # JioMart runs on Fynd: product pages are /product/<slug>
        slug = ctx.get("slug")
        if slug and re.fullmatch(r"[a-z0-9-]+", str(slug)):
            return f"https://www.jiomart.com/product/{slug}"
        url = ctx.get("url")
        if isinstance(url, str) and ("/product/" in url or "/p/" in url):
            return url if url.startswith("http") else "https://www.jiomart.com" + ("" if url.startswith("/") else "/") + url
        return self.search_url(ctx["title"])

    def search_url(self, title: str) -> str:
        return f"https://www.jiomart.com/search?q={self.q(title)}"

    async def seed_location(self, ctx: BrowserContext) -> None:
        pincode = str(self.cfg["location"].get("pincode") or "").strip()
        if not pincode:
            return
        await ctx.add_cookies([
            {"name": "nms_mgo_pincode", "value": pincode, "domain": ".jiomart.com", "path": "/"},
            {"name": "nms_mgo_city", "value": (self.cfg["location"].get("city") or "").upper(), "domain": ".jiomart.com", "path": "/"},
        ])

    async def prepare_location(self, ctx: BrowserContext, page: Page) -> bool:
        return False
