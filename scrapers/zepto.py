from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .base import BaseScraper


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "item"


class ZeptoScraper(BaseScraper):
    name = "zepto"
    display = "Zepto"
    home_url = "https://www.zepto.com/"
    seed_urls = []
    product_link_pattern = r"zepto\.com/pn/[^/?#]+/pvid/([0-9a-f-]{36})"
    category_link_pattern = r"zepto\.com/(?:cn/[^?#]+/cid/[0-9a-f-]{36}/scid/[0-9a-f-]{36}|pip/[^?#]+)"
    json_url_pattern = r"zepto\.com/|zeptonow\.com/"
    price_divisor = 100.0  # Zepto's APIs return paise

    def build_url(self, ctx: Dict[str, Any]) -> Optional[str]:
        pid = str(ctx.get("id") or "")
        if re.fullmatch(r"[0-9a-f-]{36}", pid):
            return f"https://www.zepto.com/pn/{ctx.get('slug') or _slugify(ctx['title'])}/pvid/{pid}"
        return self.search_url(ctx["title"])

    def search_url(self, title: str) -> str:
        return f"https://www.zepto.com/search?query={self.q(title)}"
