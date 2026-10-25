"""Bring a Trailer scraper — collects recent auction results (USD) per model."""
from __future__ import annotations

import logging
import re
import statistics
import time
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SEARCH_URL = "https://bringatrailer.com/search/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_RESULTS_PER_MODEL = 10


def _get(session: requests.Session, url: str, **kwargs) -> BeautifulSoup | None:
    try:
        resp = session.get(url, timeout=15, **kwargs)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        log.warning("BaT fetch failed %s: %s", url, exc)
        return None


def _parse_usd(text: str) -> int | None:
    cleaned = re.sub(r"[$,\s]", "", text)
    try:
        val = int(cleaned)
        return val if 1_000 < val < 5_000_000 else None
    except ValueError:
        return None


def _search_bat(session: requests.Session, query: str) -> list[dict]:
    """Search BaT and return sold listing data."""
    soup = _get(session, SEARCH_URL, params={"s": query})
    if soup is None:
        return []

    sales: list[dict] = []

    # BaT search results — sold listings have a hammer price in the card
    for card in soup.select(".listing-card, .list-item, article"):
        title_el = card.select_one("h3, h2, .listing-title, .title")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        # Find sold price (often in a span with "Sold for" or a .sold-price element)
        price_usd: int | None = None
        for price_el in card.select(".sold-price, .hammer-price, [class*='sold'], [class*='price']"):
            text = price_el.get_text(strip=True)
            if "$" in text or re.search(r"\d{4,}", text):
                price_usd = _parse_usd(text)
                if price_usd:
                    break

        # Bid count
        bids = 0
        bid_el = card.select_one(".bid-count, [class*='bid'], [class*='bids']")
        if bid_el:
            m = re.search(r"\d+", bid_el.get_text())
            if m:
                bids = int(m.group())

        # Date
        date_el = card.select_one("time, .date, [class*='date']")
        date_str = date_el.get("datetime", date_el.get_text(strip=True)) if date_el else ""

        if price_usd:
            sales.append({"title": title, "price_usd": price_usd, "bids": bids, "date": date_str})

        if len(sales) >= MAX_RESULTS_PER_MODEL:
            break

    return sales


def scrape(models: list[dict[str, Any]]) -> dict[str, dict]:
    session = requests.Session()
    session.headers.update(HEADERS)
    results: dict[str, dict] = {}

    for model in models:
        model_id: str = model["id"]
        query: str = model.get("bat_search", model["name"])

        log.info("BaT scraping: %s (%s)", model_id, query)
        sales = _search_bat(session, query)

        prices = [s["price_usd"] for s in sales if s["price_usd"]]
        median_usd = int(statistics.median(prices)) if prices else 0

        results[model_id] = {
            "recent_sales": sales[:MAX_RESULTS_PER_MODEL],
            "median_usd": median_usd,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info("BaT: %s → %d sales, median $%d", model_id, len(sales), median_usd)
        time.sleep(2)

    return results
