from __future__ import annotations

from .flipkart import FlipkartScraper


class FlipkartMinutesScraper(FlipkartScraper):
    """Flipkart's quick-commerce service lives on flipkart.com with marketplace=HYPERLOCAL."""

    name = "flipkart_minutes"
    display = "Flipkart Minutes"
    marketplace = "HYPERLOCAL"
    home_url = "https://www.flipkart.com/flipkart-minutes-store?marketplace=HYPERLOCAL"
    seed_urls = []
    category_pool = []
    category_link_pattern = r"flipkart\.com/[^#]*marketplace=HYPERLOCAL"
    discover_on_home = True
