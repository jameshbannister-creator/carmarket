"""Gumtree UK scraper — private/enthusiast listing counts and prices per model.

Gumtree captures private-seller supply that doesn't always appear on Car & Classic.
The C&C vs Gumtree price gap is a useful signal: if private prices lead dealer prices,
demand is pulling ahead of stock.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.gumtree.com/search"
BASE_PARAMS = {
    "search_category": "cars",
    "search_location": "uk",
    "distance": "",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
CRAWL_DELAY = 2.0


def _parse_price(text: str) -> int | None:
    cleaned = re.sub(r"[£,\s]", "", text)
    m = re.search(r"\d+", cleaned)
    if not m:
        return None
    val = int(m.group())
    return val if 500 < val < 2_000_000 else None


def _scrape(session: requests.Session, query: str) -> dict:
    params = {**BASE_PARAMS, "q": query}
    try:
        resp = session.get(SEARCH_URL, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Gumtree fetch failed for '%s': %s", query, exc)
        return {"listing_count": 0, "prices": [], "median_price": 0}

    soup = BeautifulSoup(resp.text, "html.parser")

    # Result count — Gumtree renders "X ads" or "X results"
    count = 0
    for sel in (
        ".page-title",
        "[data-q='search-results-count']",
        ".results-title",
        "h1",
    ):
        el = soup.select_one(sel)
        if el:
            m = re.search(r"([\d,]+)\s*(ad|result|car|listing)", el.get_text(), re.IGNORECASE)
            if m:
                count = int(m.group(1).replace(",", ""))
                break

    # Fall back to counting listing cards
    if count == 0:
        cards = soup.select(
            "article.listing-maxi, li.listing-results-result, "
            "[data-q='listing'], .natural"
        )
        count = len(cards)

    # Prices
    prices: list[int] = []
    for el in soup.select(
        ".listing-price, [data-testid='listing-price'], "
        ".price-amount, .listing-price strong"
    ):
        txt = el.get_text(strip=True)
        val = _parse_price(txt)
        if val:
            prices.append(val)

    import statistics
    median_price = int(statistics.median(prices)) if prices else 0

    return {
        "listing_count": count,
        "prices": prices,
        "median_price": median_price,
    }


def scrape(models: list[dict[str, Any]]) -> dict[str, dict]:
    session = requests.Session()
    session.headers.update(HEADERS)
    results: dict[str, dict] = {}

    for model in models:
        model_id: str = model["id"]
        # Reuse the ebay_search term — tends to be well-formed for UK market
        query: str = model.get("ebay_search", model.get("cc_search", model["name"]))

        log.info("Gumtree scraping: %s (%s)", model_id, query)
        data = _scrape(session, query)
        data["scraped_at"] = datetime.now(timezone.utc).isoformat()

        results[model_id] = data
        log.info(
            "Gumtree: %s → %d listings, median £%s",
            model_id, data["listing_count"],
            f"{data['median_price']:,}" if data["median_price"] else "n/a",
        )
        time.sleep(CRAWL_DELAY)

    return results
