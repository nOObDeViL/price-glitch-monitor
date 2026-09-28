from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import urljoin

from .base import BaseScraper

# Flipkart's own "70% or more" discount facet, the highest bucket it offers;
# our detector then applies the real >=80% rule.
DISCOUNT_FACET = "&p%5B%5D=facets.discount_range_v1%255B%255D%3D70%2525%2Bor%2Bmore&sort=recency_desc"


class FlipkartScraper(BaseScraper):
    name = "flipkart"
    display = "Flipkart"
    home_url = "https://www.flipkart.com/offers-store"
    seed_urls = [
        "https://www.flipkart.com/offers-list/recommended-for-you",
    ]
    # Category listing feeds where the "70% or more" facet is verified to apply
    # (Flipkart's facet key differs per category; unfiltered ones just waste a page).
    # Rotated a few per cycle. Add more via config.json -> extra_urls.flipkart
    category_pool = [
        "https://www.flipkart.com/mobile-accessories/pr?sid=tyy,4mr" + DISCOUNT_FACET,
        "https://www.flipkart.com/clothing-and-accessories/pr?sid=clo" + DISCOUNT_FACET,
        "https://www.flipkart.com/beauty-and-grooming/pr?sid=g9b" + DISCOUNT_FACET,
        "https://www.flipkart.com/bags-wallets-belts/pr?sid=reh" + DISCOUNT_FACET,
        "https://www.flipkart.com/search?q=shoes" + DISCOUNT_FACET,
        "https://www.flipkart.com/search?q=toys" + DISCOUNT_FACET,
    ]
    product_link_pattern = r"flipkart\.com/[^?#]*/p/(itm[0-9a-z]+)"
    json_url_pattern = r"flipkart\.com/(?:api/)?\d+/page/fetch|rome\.api\.flipkart\.com"
    discover_on_home = False
    marketplace = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seed_urls = list(self.seed_urls) + self._rotate(
            list(self.category_pool), int(self.cfg["monitor"]["max_category_pages"])
        )

    def id_from_url(self, url: str) -> Optional[str]:
        m = re.search(r"[?&]pid=([A-Z0-9]{10,20})", url)
        if m:
            return m.group(1)
        return super().id_from_url(url)

    def build_url(self, ctx: Dict[str, Any]) -> Optional[str]:
        url = ctx.get("url")
        if isinstance(url, str) and ("/p/" in url or url.startswith("http")):
            return urljoin("https://www.flipkart.com", url)
        return self.search_url(ctx["title"])

    def clean_url(self, url: str) -> str:
        m = re.match(r"(https://www\.flipkart\.com/[^?#]*/p/itm[0-9a-z]+)", url)
        if not m:
            return url
        keep = [kv for kv in re.findall(r"[?&]((?:pid|marketplace)=[^&#]+)", url)]
        return m.group(1) + ("?" + "&".join(keep) if keep else "")

    def search_url(self, title: str) -> str:
        extra = f"&marketplace={self.marketplace}" if self.marketplace else ""
        return f"https://www.flipkart.com/search?q={self.q(title)}{extra}"
