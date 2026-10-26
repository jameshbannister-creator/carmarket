"""Historics Auctioneers sold price scraper.

Source: https://www.historics.co.uk/auction-results
Classic car auctions (A-series slugs) only. Scrapes up to 96 lots per auction
per page via Playwright (JS-rendered). Page 2 of auctions with >96 lots is
not yet implemented — covers the majority of relevant lots.

Prices shown as "Sold £X,XXX" on lot listings are hammer + buyer's premium.
Date of sale is parsed from the page text for each auction listing.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

SIX_MONTHS_AGO = datetime.now(timezone.utc) - timedelta(days=183)
MAX_PRICE = 30_000

AUCTION_RESULTS_URL = "https://www.historics.co.uk/auction-results"

# Classic car auctions: slug starts with a0 (A-series), e.g. a076, a075
_CLASSIC_SLUG_RE = re.compile(r"/auction/details/(a\d+[-])", re.IGNORECASE)

# Date patterns in auction listing text
_DATE_RE = re.compile(
    r"(?:Date|Ends):\s*"
    r"(?:\d+\w*\s+)?(\d{1,2})\w*\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\w*"
    r"[,\s]+(\d{4})",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

_JS_LOTS = r"""
() => {
    const text = document.body.innerText;
    const lots = [];
    // Match: "Lot NNN - Title ... Sold £X,XXX" blocks
    const lotRe = /Lot \d+ - ([\s\S]+?)(?=Lot \d+ - |Past lots|$)/g;
    let m;
    while ((m = lotRe.exec(text)) !== null) {
        const block = m[1];
        const soldM = block.match(/Sold £([\d,]+)/);
        if (!soldM) continue;
        const price = parseInt(soldM[1].replace(/,/g, ''), 10);
        lots.push({title: block.split('\n')[0].trim().toLowerCase(), price});
    }
    return lots;
}
"""

_JS_AUCTION_LINKS = r"""
() => {
    const seen = new Set();
    const auctions = [];
    document.querySelectorAll('a[href*="/auction/details/"]').forEach(a => {
        const href = a.href;
        const auM = href.match(/[?&]au=(\d+)/);
        if (!auM || seen.has(auM[1])) return;
        seen.add(auM[1]);
        // Only A-series (classic car) auctions
        if (!/\/auction\/details\/a\d+/i.test(href)) return;
        const container = a.closest('section, article, .col-12, .row, .card, div') || a.parentElement;
        const containerText = container ? container.innerText : '';
        auctions.push({au: auM[1], href: href.split('?')[0], text: containerText.substring(0, 300)});
    });
    return auctions;
}
"""


def _parse_auction_date(text: str) -> datetime | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = _MONTH_MAP.get(month_name)
    if not month:
        return None
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_auction_list(page) -> list[dict]:
    """Return recent classic car auctions from Historics as [{au, href, date}]."""
    try:
        page.goto(AUCTION_RESULTS_URL, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("Historics auction list fetch failed: %s", exc)
        return []

    try:
        auctions = page.evaluate(_JS_AUCTION_LINKS)
    except Exception as exc:
        log.warning("Historics auction link extraction failed: %s", exc)
        return []

    recent = []
    for auction in auctions:
        # Try to get date from text; fallback to including it if date is unclear
        date = _parse_auction_date(auction.get("text", ""))
        if date and date < SIX_MONTHS_AGO:
            continue
        recent.append({
            "au": auction["au"],
            "href": auction["href"],
            "date": date,
        })

    log.info("Historics: %d classic car auctions within scope", len(recent))
    return recent


_LOTS_CACHE: list[dict] | None = None
_LOTS_LOADED = False


def _load_lots(page) -> list[dict]:
    """Pre-fetch all lot titles + sold prices from recent Historics auctions."""
    global _LOTS_CACHE, _LOTS_LOADED
    if _LOTS_LOADED:
        return _LOTS_CACHE or []

    _LOTS_LOADED = True
    _LOTS_CACHE = []

    auctions = _load_auction_list(page)

    for auction in auctions:
        au_id = auction["au"]
        base_url = auction["href"]
        url = f"{base_url}?au={au_id}&lots_per_page=96"

        log.info("Historics: loading auction au=%s", au_id)
        try:
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception as exc:
            log.warning("Historics auction page failed au=%s: %s", au_id, exc)
            continue

        try:
            lots = page.evaluate(_JS_LOTS)
        except Exception as exc:
            log.warning("Historics lot extraction failed au=%s: %s", au_id, exc)
            continue

        _LOTS_CACHE.extend(lots)
        log.info("  au=%s: %d lots with sold prices", au_id, len(lots))
        time.sleep(2.0)

    log.info("Historics: %d total sold lots cached", len(_LOTS_CACHE))
    return _LOTS_CACHE


def search(page, make: str, keywords: list[str]) -> list[int]:
    """Return sold prices for a model from Historics.

    Args:
        page: Playwright page (pass the shared page object).
        make: Car make (e.g. "BMW", "Volkswagen").
        keywords: Keywords to match in lot title (e.g. ["e30", "325i"]).

    Returns:
        List of GBP sold prices.
    """
    lots = _load_lots(page)
    make_lower = make.lower()
    kw_lower = [k.lower() for k in keywords if len(k) > 2]

    prices = []
    for lot in lots:
        title = lot["title"]
        if make_lower not in title:
            continue
        if kw_lower and not all(kw in title for kw in kw_lower):
            continue
        price = lot["price"]
        if 500 < price < MAX_PRICE:
            prices.append(price)

    return prices
