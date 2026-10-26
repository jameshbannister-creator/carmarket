"""Collecting Cars sold price scraper.

Source: https://collectingcars.com/sold
Uses Playwright + InstantSearch initial state embedded in page JS.
Filters by countryCode=GB and a text query per model.
Returns sold prices in GBP for UK lots only, within last 6 months.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

SIX_MONTHS_AGO = datetime.now(timezone.utc) - timedelta(days=183)
MAX_PRICE = 30_000

CC_SOLD_URL = (
    "https://collectingcars.com/sold"
    "?refinementList[countryCode][0]=GB&query={query}"
)

_JS_EXTRACT = """
() => {
    const key = Object.getOwnPropertySymbols(window)
        .find(s => s.toString().includes('InstantSearch'));
    if (!key) return null;
    const data = window[key];
    const results = data?.['production_listings']?.results?.[0];
    if (!results) return null;
    return results.hits.map(h => ({
        name: h.title || h.name || '',
        make: (h.productMake || '').toLowerCase(),
        priceSold: h.priceSold,
        hidden: h.isSoldPriceHidden,
        country: h.countryCode,
        soldDate: h.dtSoldUTC || h.dtAuctionEndedUTC || '',
    }));
}
"""


def _parse_sold_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    # Format: "2026-10-25 19:20:06" or ISO with T
    date_str = date_str.replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fetch_sold(page, query: str, keywords: list[str]) -> list[int]:
    """Fetch sold prices for a model from Collecting Cars UK sold results.

    Args:
        page: Playwright page object.
        query: Search query (e.g. "E30 325i", "Golf GTI").
        keywords: Additional keywords all of which must appear in the listing
                  title (case-insensitive). If empty, query match is enough.

    Returns:
        List of GBP sold prices within 6 months.
    """
    url = CC_SOLD_URL.format(query=query.replace(" ", "+"))
    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("Collecting Cars fetch failed for '%s': %s", query, exc)
        return []

    try:
        hits = page.evaluate(_JS_EXTRACT)
    except Exception as exc:
        log.warning("Collecting Cars JS extract failed for '%s': %s", query, exc)
        return []

    if not hits:
        return []

    kw_lower = [k.lower() for k in keywords if len(k) > 2]
    prices: list[int] = []

    for hit in hits:
        if hit.get("hidden"):
            continue
        if hit.get("country") != "GB":
            continue

        sold_dt = _parse_sold_date(hit.get("soldDate", ""))
        if sold_dt and sold_dt < SIX_MONTHS_AGO:
            continue

        name_lower = (hit.get("name") or "").lower()
        if kw_lower and not all(kw in name_lower for kw in kw_lower):
            continue

        price = hit.get("priceSold")
        if not price:
            continue
        try:
            price = int(price)
        except (TypeError, ValueError):
            continue

        if 500 < price < MAX_PRICE:
            prices.append(price)

    return prices


def scrape(page, models: list[dict]) -> dict[str, list[int]]:
    """Scrape Collecting Cars for a list of models.

    Each model should have a 'cc_search' field (or falls back to 'name').
    Returns {model_id: [sold_prices]}.
    """
    results: dict[str, list[int]] = {}

    for model in models:
        mid = model["id"]
        query = model.get("cc_search", model["name"])
        keywords = query.lower().split()

        prices = fetch_sold(page, query, keywords)
        results[mid] = prices

        if prices:
            log.info(
                "Collecting Cars %s: %d results %s",
                mid, len(prices), prices,
            )
        time.sleep(2.0)

    return results
