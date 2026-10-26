"""Morris Leslie Auctions sold price scraper.

Source: https://auction.morrisleslie.com/classic-auction-results
Results are on per-sale pages (DataTables, all rows in DOM) — needs Playwright.
Prices are "Hammer plus Fees" (hammer + buyer's premium already included).
~5 sales/year: Feb, May, Sep, Nov.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

RESULTS_INDEX = "https://auction.morrisleslie.com/classic-auction-results"
SIX_MONTHS_AGO = datetime.now(timezone.utc) - timedelta(days=183)

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_sale_date(url: str) -> datetime | None:
    """Parse month-year from URLs like /september-2026 or /may-2026."""
    slug = url.rstrip("/").split("/")[-1].lower()
    m = re.match(r"([a-z]+)-(\d{4})", slug)
    if not m:
        return None
    month = MONTH_MAP.get(m.group(1))
    year = int(m.group(2))
    if not month:
        return None
    return datetime(year, month, 15, tzinfo=timezone.utc)


def fetch_sold(page, make: str, keywords: list[str]) -> list[int]:
    """Fetch sold prices across recent Morris Leslie sales for a make/keywords.

    Args:
        page: Playwright page object (already initialised)
        make: Make to match in table col 2 (e.g. "Honda", "Subaru") — case-insensitive
        keywords: List of terms to match in model column (col 3) — all must appear

    Returns:
        List of GBP sold prices (hammer + fees) within last 6 months.
    """
    # Get index page to find sale URLs
    try:
        page.goto(RESULTS_INDEX, timeout=20_000, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
    except Exception as exc:
        log.warning("Morris Leslie index fetch failed: %s", exc)
        return []

    sale_links = page.evaluate("""
        () => [...document.querySelectorAll('a')].map(a => ({href: a.href, text: a.textContent.trim()}))
                .filter(a => /\\/(january|february|march|april|may|june|july|august|september|october|november|december)-\\d{4}/i.test(a.href))
    """)

    prices: list[int] = []
    make_lower = make.lower()
    kw_lower = [k.lower() for k in keywords]

    for link in sale_links:
        sale_date = _parse_sale_date(link["href"])
        if not sale_date or sale_date < SIX_MONTHS_AGO:
            continue

        log.info("  Morris Leslie: fetching %s (%s)", link["text"], link["href"])
        try:
            page.goto(link["href"], timeout=20_000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception as exc:
            log.warning("Morris Leslie sale fetch failed %s: %s", link["href"], exc)
            continue

        # Extract all table rows via DataTable (already fully loaded in DOM)
        rows = page.evaluate("""
            () => [...document.querySelectorAll('table tbody tr')]
                  .map(r => [...r.querySelectorAll('td')].map(td => td.innerText.trim()))
        """)

        for row in rows:
            if len(row) < 9:
                continue
            row_make = row[2].lower()
            row_model = row[3].lower()

            if make_lower not in row_make:
                continue
            combined = f"{row_make} {row_model}"
            if not all(kw in combined for kw in kw_lower):
                continue

            price_str = row[8]
            m = re.search(r"£([\d,]+)", price_str)
            if not m:
                continue
            try:
                gbp = int(m.group(1).replace(",", ""))
                if 500 < gbp < 500_000:
                    prices.append(gbp)
                    log.info("    Found: %s %s → £%s", row[2], row[3], f"{gbp:,}")
            except ValueError:
                continue

        time.sleep(1.0)

    return prices


def scrape_for_models(page, models: list[dict]) -> dict[str, list[int]]:
    """Scrape Morris Leslie for a list of model configs.

    Each model needs 'make' and either 'morris_leslie_search' or 'cc_search'.
    Returns {model_id: [sold_prices]}.
    """
    results: dict[str, list[int]] = {}
    for model in models:
        make = model.get("morris_leslie_make") or model.get("make", "")
        raw = model.get("morris_leslie_search") or model.get("cc_search", model["name"])
        keywords = [k for k in raw.lower().split() if len(k) > 2]
        results[model["id"]] = fetch_sold(page, make, keywords)
        time.sleep(1.0)
    return results
