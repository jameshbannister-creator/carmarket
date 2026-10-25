"""eBay UK scraper — active listing counts and visible watcher data per model."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.ebay.co.uk/sch/i.html"
# sacat=9801 is eBay's "Classic & Vintage Cars" category
BASE_PARAMS = {
    "LH_ItemCondition": "3000",  # Used
    "_sacat": "9801",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    cookie = os.environ.get("EBAY_SESSION_COOKIE")
    if cookie:
        session.cookies.set("ebay", cookie, domain=".ebay.co.uk")
        log.info("eBay: using session cookie for completed listings")
    else:
        log.warning("eBay: EBAY_SESSION_COOKIE not set — completed/sold data unavailable")
    return session


def _parse_gbp(text: str) -> float | None:
    cleaned = re.sub(r"[£,\s]", "", text)
    try:
        val = float(cleaned)
        return val if 100 < val < 2_000_000 else None
    except ValueError:
        return None


def _parse_watchers(text: str) -> int:
    """Extract watcher count from strings like '12 watching', '1 watcher'."""
    m = re.search(r"(\d+)\s+watch", text, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _scrape_active(session: requests.Session, query: str) -> tuple[list[dict], int]:
    params = {**BASE_PARAMS, "_nkw": query}
    try:
        resp = session.get(SEARCH_URL, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("eBay fetch failed for '%s': %s", query, exc)
        return [], 0

    soup = BeautifulSoup(resp.text, "html.parser")
    listings: list[dict] = []

    # Total result count
    count = 0
    count_el = soup.select_one(".srp-controls__count-heading, h1.srp-controls__count-heading")
    if count_el:
        m = re.search(r"[\d,]+", count_el.get_text())
        if m:
            count = int(m.group().replace(",", ""))

    for item in soup.select(".s-item, li.s-item"):
        # Skip the promoted/placeholder first item eBay inserts
        if "s-item__pl-on-bottom" in item.get("class", []):
            continue

        title_el = item.select_one(".s-item__title, h3")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if title.lower() == "shop on ebay":
            continue

        price_el = item.select_one(".s-item__price")
        price_gbp: float | None = None
        if price_el:
            price_gbp = _parse_gbp(price_el.get_text(strip=True))

        watchers = 0
        for el in item.select(".s-item__watchcount, .s-item__hotness, [class*='watch']"):
            w = _parse_watchers(el.get_text(strip=True))
            if w:
                watchers = w
                break

        listings.append({"title": title, "price_gbp": price_gbp, "watchers": watchers})

    return listings, count


def scrape(models: list[dict[str, Any]]) -> dict[str, dict]:
    session = _build_session()
    results: dict[str, dict] = {}

    for model in models:
        model_id: str = model["id"]
        query: str = model.get("ebay_search", model["name"])

        log.info("eBay scraping: %s (%s)", model_id, query)
        active, total_count = _scrape_active(session, query)

        total_watchers = sum(l["watchers"] for l in active)
        watched_listings = [l for l in active if l["watchers"] > 0]

        results[model_id] = {
            "active_listings": active,
            "total_count": total_count or len(active),
            "total_watchers": total_watchers,
            "watched_count": len(watched_listings),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info(
            "eBay: %s → %d listings, %d total watchers on %d listings",
            model_id, total_count, total_watchers, len(watched_listings),
        )
        time.sleep(2)

    return results
