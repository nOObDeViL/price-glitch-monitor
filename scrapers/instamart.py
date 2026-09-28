from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

from playwright.async_api import BrowserContext, Page

from .base import BaseScraper


class InstamartScraper(BaseScraper):
    name = "instamart"
    display = "Swiggy Instamart"
    home_url = "https://www.swiggy.com/instamart"
    seed_urls = []
    product_link_pattern = r"swiggy\.com/instamart/item/([A-Za-z0-9]+)"
    category_link_pattern = r"swiggy\.com/instamart/(?:category-listing|collection-listing)\?[^#]+"
    json_url_pattern = r"swiggy\.com/(?:api/)?instamart/"

    def build_url(self, ctx: Dict[str, Any]) -> Optional[str]:
        # Item pages are keyed by productId (the parent of the sku/variation ids).
        for d in ctx.get("chain", []):
            pid = d.get("productId") if isinstance(d, dict) else None
            if isinstance(pid, str) and pid:
                return f"https://www.swiggy.com/instamart/item/{pid}"
        return self.search_url(ctx["title"])

    def links_from_payloads(self) -> List[str]:
        blob = json.dumps(self._json_payloads)
        urls: List[str] = []
        for m in re.finditer(r'swiggy://stores/instamart/((?:category-listing|campaign-collection/listing)\?[^"\\]+)', blob):
            path = unquote(m.group(1)).replace(" ", "%20")
            if "showAgeConsent=true" in path:  # tobacco etc.
                continue
            url = f"https://www.swiggy.com/instamart/{path}"
            if url not in urls:
                urls.append(url)
        return urls

    def search_url(self, title: str) -> str:
        return f"https://www.swiggy.com/instamart/search?custom_back=true&query={self.q(title)}"

    async def seed_location(self, ctx: BrowserContext) -> None:
        loc = self.cfg["location"]
        if loc.get("latitude") is None:
            return
        value = json.dumps({
            "address": loc.get("label") or loc.get("city") or "",
            "lat": float(loc["latitude"]),
            "lng": float(loc["longitude"]),
            "id": "",
            "annotation": "",
            "name": "",
        }, separators=(",", ":"))
        await ctx.add_cookies([
            {"name": "userLocation", "value": quote(value), "domain": ".swiggy.com", "path": "/"},
            {"name": "lat", "value": str(loc["latitude"]), "domain": ".swiggy.com", "path": "/"},
            {"name": "lng", "value": str(loc["longitude"]), "domain": ".swiggy.com", "path": "/"},
        ])
