"""Car and Classic scraper — collects listing counts and prices per model."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = "https://www.carandclassic.com/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


def _get_page(session: requests.Session, url: str, params: dict) -> BeautifulSoup | None:
    try:
        resp = session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        log.warning("C&C fetch failed: %s", exc)
        return None


def _parse_price(text: str) -> int | None:
    """Extract integer GBP price from strings like '£18,995' or '£18995'."""
    cleaned = re.sub(r"[£,\s]", "", text)
    # skip if it's in euros or dollars
    if text.strip().startswith(("€", "$")):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _extract_listing_count(soup: BeautifulSoup) -> int:
    """Parse total result count from the page."""
    # C&C shows "Showing X listings" in h2.text-title-xs (as of Oct 2026)
    for el in soup.find_all("h2"):
        text = el.get_text(strip=True)
        m = re.search(r"([\d,]+)\s+listing", text, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))

    # Fallback: any element with "X listings" or "X results"
    for el in soup.find_all(string=re.compile(r"\d[\d,]*\s+(listing|result)", re.IGNORECASE)):
        m = re.search(r"([\d,]+)\s+(listing|result)", el, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))

    # Last resort: count article cards
    return len(soup.find_all("article"))


def _extract_prices(soup: BeautifulSoup) -> list[int]:
    """Pull all visible GBP prices from listing cards."""
    prices: list[int] = []
    # C&C uses span with class 'text-body-md-emphasized' for prices (as of Oct 2026)
    for el in soup.find_all("span", class_=re.compile(r"text-body-md-emphasized|text-title-sm")):
        text = el.get_text(strip=True)
        if "£" in text:
            p = _parse_price(text)
            if p and 1_000 < p < 2_000_000:
                prices.append(p)

    # Fallback: any span/div containing a GBP price
    if not prices:
        for el in soup.find_all(["span", "div", "p"]):
            if el.find():
                continue  # skip elements with children
            text = el.get_text(strip=True)
            if text.startswith("£"):
                p = _parse_price(text)
                if p and 1_000 < p < 2_000_000:
                    prices.append(p)

    return prices


def scrape(models: list[dict[str, Any]]) -> dict[str, dict]:
    session = requests.Session()
    session.headers.update(HEADERS)
    results: dict[str, dict] = {}

    for model in models:
        model_id: str = model["id"]
        query: str = model.get("cc_search", model["name"])
        make: str = model.get("make", "").lower()

        log.info("C&C scraping: %s (%s)", model_id, query)
        params = {"make": make, "q": query} if make else {"q": query}

        soup = _get_page(session, BASE_URL, params)
        if soup is None:
            log.error("C&C: no page for %s", model_id)
            results[model_id] = {
                "listing_count": 0,
                "prices": [],
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "error": "fetch_failed",
            }
            time.sleep(1)
            continue

        count = _extract_listing_count(soup)
        prices = _extract_prices(soup)

        results[model_id] = {
            "listing_count": count,
            "prices": prices,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info("C&C: %s → %d listings, %d prices found", model_id, count, len(prices))
        time.sleep(1.5)  # polite crawl delay

    return results
