"""Sold price scraper — fetches recent auction results for a list of models.

Uses the same logic as discover.py but packaged for use by run_all.py on
the pinned watchlist models.

Sources:
  - Car & Classic /auctions/results        (GBP, last 6 months)
  - Bring a Trailer JSON API               (USD→GBP, last 6 months)
  - Anglia Car Auctions                    (GBP, static HTML)
  - Morris Leslie Auctions                 (GBP, pre-fetched DataTables)
  - Iconic Auctioneers (fmr. Silverstone)  (GBP, pre-fetched)
  - H&H Classics                           (GBP, pre-fetched)
  - Mathewsons                             (GBP, pre-fetched)
"""
from __future__ import annotations

import logging
import statistics
import time

from discover import (
    _fetch_cc_sold, _fetch_bat_sold,
    _fetch_anglia_sold, _search_morris_leslie, _load_morris_leslie_rows,
    _search_iconic, _load_iconic_lots,
    _search_hnh, _load_hnh_lots,
    _search_mathewsons, _load_mathewsons_lots,
    CRAWL_DELAY,
)

log = logging.getLogger(__name__)


def scrape(models: list[dict]) -> dict[str, dict]:
    """Return sold price data for each model.

    Returns:
        {model_id: {"sold_median_6m": int|None, "sold_count_6m": int}}
    """
    from playwright.sync_api import sync_playwright

    results: dict[str, dict] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # Pre-fetch all auction-house caches once
        log.info("Pre-fetching Morris Leslie sale data…")
        _load_morris_leslie_rows(page)
        log.info("Pre-fetching Iconic Auctioneers lot data…")
        _load_iconic_lots(page)
        log.info("Pre-fetching H&H Classics lot data…")
        _load_hnh_lots(page)
        log.info("Pre-fetching Mathewsons lot data…")
        _load_mathewsons_lots(page)

        for model in models:
            mid = model["id"]
            make = model.get("make", "")
            query = model.get("cc_search", model["name"])
            bat_query = model.get("bat_search", query)
            keywords = query.lower().split()

            log.info("Sold prices (C&C): %s", mid)
            cc_sold = _fetch_cc_sold(page, make, query)
            time.sleep(CRAWL_DELAY)

            log.info("Sold prices (BaT): %s", mid)
            bat_sold = _fetch_bat_sold(page, bat_query)
            time.sleep(CRAWL_DELAY)

            log.info("Sold prices (Anglia): %s", mid)
            anglia_sold = _fetch_anglia_sold(make, keywords)
            time.sleep(1.0)

            ml_sold = _search_morris_leslie(page, make, keywords)
            iconic_sold = _search_iconic(make, keywords)
            hnh_sold = _search_hnh(make, keywords)
            mathewsons_sold = _search_mathewsons(make, keywords)

            all_sold = cc_sold + bat_sold + anglia_sold + ml_sold + iconic_sold + hnh_sold + mathewsons_sold
            sold_median = int(statistics.median(all_sold)) if all_sold else None

            results[mid] = {
                "sold_median_6m": sold_median,
                "sold_count_6m": len(all_sold),
            }

            if all_sold:
                log.info(
                    "  %s: %d sold results, median £%s "
                    "(C&C:%d BaT:%d Anglia:%d ML:%d Iconic:%d H&H:%d Math:%d)",
                    mid, len(all_sold),
                    f"{sold_median:,}" if sold_median else "n/a",
                    len(cc_sold), len(bat_sold), len(anglia_sold), len(ml_sold),
                    len(iconic_sold), len(hnh_sold), len(mathewsons_sold),
                )
            else:
                log.info("  %s: no sold results", mid)

        browser.close()

    return results
