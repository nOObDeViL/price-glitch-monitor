"""Scraper registry."""
from __future__ import annotations

from typing import Dict, Type

from .amazon import AmazonScraper
from .base import BaseScraper, ScrapeResult
from .blinkit import BlinkitScraper
from .flipkart import FlipkartScraper
from .flipkart_minutes import FlipkartMinutesScraper
from .instamart import InstamartScraper
from .jiomart import JioMartScraper
from .zepto import ZeptoScraper

SCRAPERS: Dict[str, Type[BaseScraper]] = {
    s.name: s
    for s in (
        AmazonScraper,
        FlipkartScraper,
        FlipkartMinutesScraper,
        BlinkitScraper,
        ZeptoScraper,
        InstamartScraper,
        JioMartScraper,
    )
}

__all__ = ["SCRAPERS", "BaseScraper", "ScrapeResult"]
