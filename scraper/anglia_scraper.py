"""Anglia Car Auctions sold price scraper.

Source: https://angliacarauctions.co.uk/results/classic/{Make}
Static HTML — no Playwright needed.
Prices are "Result inc Premium" (hammer + buyer's premium).
Date is embedded in the lot URL: /auctions/{id}-{DD}-{Mon}-{YYYY}/...
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://angliacarauctions.co.uk/results/classic"
SIX_MONTHS_AGO = datetime.now(timezone.utc) - timedelta(days=183)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_lot_date(href: str) -> datetime | None:
    """Extract date from Anglia lot URL like /auctions/2752-11-Apr-2026/..."""
    m = re.search(r"/auctions/\d+-(\d+)-([A-Za-z]+)-(\d{4})/", href)
    if not m:
        return None
    day, mon_str, year = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
    month = MONTH_MAP.get(mon_str)
    if not month:
        return None
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_sold(make: str, keywords: list[str]) -> list[int]:
    """Fetch sold prices for a make, filtering by keywords in the lot title.

    Args:
        make: Make name as it appears on Anglia's site (e.g. "Honda", "Subaru")
        keywords: List of keywords to match in lot title (e.g. ["integra", "type r"])
                  All keywords must appear (case-insensitive) for a lot to match.

    Returns:
        List of GBP sold prices (inc. buyer's premium) within 6 months.
    """
    url = f"{BASE}/{make.replace(' ', '%20')}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Anglia fetch failed for '%s': %s", make, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    containers = soup.find_all("div", class_=re.compile(r"col-12.*d-flex.*flex-column"))

    prices: list[int] = []
    for container in containers:
        a = container.find("a", href=re.compile(r"/auctions/"))
        if not a:
            continue

        href = a.get("href", "")
        lot_date = _parse_lot_date(href)
        if not lot_date or lot_date < SIX_MONTHS_AGO:
            continue

        # Lot title from URL slug (last path segment, after the lot id prefix)
        lot_slug = href.rstrip("/").split("/")[-1].lower()
        if not all(kw.lower() in lot_slug for kw in keywords):
            continue

        # Check for sold price
        price_span = container.find("span", class_="fw-bold")
        if not price_span:
            continue
        text = price_span.get_text(strip=True)
        m = re.search(r"Result inc Premium:\s*£([\d,]+)", text)
        if not m:
            continue

        try:
            gbp = int(m.group(1).replace(",", ""))
            if 500 < gbp < 500_000:
                prices.append(gbp)
        except ValueError:
            continue

    return prices


def scrape(models: list[dict]) -> dict[str, dict]:
    """Scrape Anglia sold prices for a list of model configs.

    Each model needs an 'anglia_make' field (the make as Anglia spells it,
    e.g. "Honda") and 'anglia_keywords' (list of strings to match in lot slug).
    Falls back to 'make' and splitting 'cc_search' if not set.

    Returns:
        {model_id: {"anglia_sold": [prices], "anglia_count": N}}
    """
    results: dict[str, dict] = {}
    for model in models:
        make = model.get("anglia_make") or model.get("make", "")
        raw_search = model.get("anglia_search") or model.get("cc_search", model["name"])
        keywords = raw_search.lower().split()

        prices = fetch_sold(make, keywords)
        results[model["id"]] = {
            "anglia_sold": prices,
            "anglia_count": len(prices),
        }

        if prices:
            log.info(
                "Anglia %s: %d results %s",
                model["id"], len(prices), prices,
            )

        time.sleep(1.0)

    return results
