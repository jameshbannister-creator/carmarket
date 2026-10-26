"""Discovery engine — scans universe models via Car & Classic (Playwright),
scores by signal strength, writes data/discoveries.json for the dashboard.

Data sources:
  - Live listings:  carandclassic.com/search              (supply + asking prices)
  - Sold prices:    carandclassic.com/auctions/results    (C&C auction results, GBP, last 6m)
  - Sold prices:    bringatrailer.com/wp-json/bat/v1/search (BaT results, USD→GBP, last 6m)
  - Sold prices:    angliacarauctions.co.uk/results/classic/{Make} (UK regional, static HTML)
  - Sold prices:    auction.morrisleslie.com               (Scottish, DataTables, pre-fetched)
  - Sold prices:    iconicauctioneers.com/auction/archive-auctions-cars (fmr. Silverstone, pre-fetched)
  - Sold prices:    handh.co.uk/auctions/auction-results   (H&H Classics, pre-fetched)
  - Sold prices:    mathewsons.co.uk/auction-results       (Mathewsons, pre-fetched)
  - Sold prices:    collectingcars.com/sold                (UK-only, Playwright + InstantSearch)
  - Sold prices:    historics.co.uk/auction-results        (A-series auctions, Playwright)

Uses cc_search field in universe.json for model-specific queries.
Pinned models (data/models.json) are excluded — they have their own tracked section.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import statistics
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UNIVERSE_JSON = DATA_DIR / "universe.json"
MODELS_JSON = DATA_DIR / "models.json"
DISCOVERIES_JSON = DATA_DIR / "discoveries.json"
PREV_DISCOVERIES_JSON = DATA_DIR / "discoveries_prev.json"

CC_BASE = "https://www.carandclassic.com/search"
CC_SOLD_BASE = "https://www.carandclassic.com/auctions/results"
BAT_API = "https://bringatrailer.com/wp-json/bat/v1/search"

CRAWL_DELAY = 3.0
TOP_N = 999
MAX_PRICE = 30_000
SIX_MONTHS_AGO = datetime.now(timezone.utc) - timedelta(days=183)

# Rough GBP/USD rate — updated manually, good enough for indicative sold prices
USD_TO_GBP = 0.79


def load_universe() -> list[dict]:
    with UNIVERSE_JSON.open() as f:
        return json.load(f)["models"]


def load_pinned_ids() -> set[str]:
    with MODELS_JSON.open() as f:
        return {m["id"] for m in json.load(f)["models"]}


def load_prev_discoveries() -> dict[str, dict]:
    for path in (PREV_DISCOVERIES_JSON, DISCOVERIES_JSON):
        if path.exists():
            try:
                with path.open() as f:
                    data = json.load(f)
                return {m["id"]: m for m in data.get("models", [])}
            except (json.JSONDecodeError, KeyError):
                pass
    return {}


# ── Live listings ─────────────────────────────────────────────────────────────

def _fetch_cc(page, make: str, query: str, year_from: int | None = None, year_to: int | None = None) -> dict:
    """Fetch listing count and asking prices from C&C search for a specific model.

    Strips the make name from the query (it is already handled by make=) and adds
    country=GB. Reads the supply count from the first model-facet link in the
    sidebar, which gives the model-filtered count rather than the inflated page
    total (e.g. "Showing 62 listings for BMW" vs the facet "3 Series: 22").
    """
    # C&C make slug: spaces → hyphens, lowercase; "Mercedes-Benz" → "mercedes"
    make_slug = make.lower().replace(" ", "-")
    make_slug = re.sub(r"-benz$", "", make_slug)
    # Strip make name from front of query using the original display name (with spaces)
    q = query.strip()
    for prefix in (make.lower(), make_slug):
        if q.lower().startswith(prefix):
            q = q[len(prefix):].strip()
            break
    q_param = q.replace(" ", "+")
    url = f"{CC_BASE}?make={make_slug}&q={q_param}&country=GB"

    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("C&C live fetch failed for '%s': %s", query, exc)
        return {"listing_count": 0, "prices": []}

    # Read count from the first model facet link in the sidebar.
    # These links carry the model-filtered total, e.g. "3 Series\n22".
    # Fall back to the H2 "Showing X listings" if no facet is found.
    count = 0
    for el in page.query_selector_all("a[href*='model=']"):
        text = el.inner_text().strip()
        m = re.search(r"\n(\d[\d,]*)$", text)
        if m:
            count = int(m.group(1).replace(",", ""))
            break

    if count == 0:
        for h2 in page.query_selector_all("h2"):
            text = h2.inner_text()
            m = re.search(r"([\d,]+)\s+listings?", text, re.IGNORECASE)
            if m:
                count = int(m.group(1).replace(",", ""))
                break

    if year_from or year_to:
        # Pair listing h2 headings (starting with a 4-digit year) with price leaf
        # elements positionally. C&C renders both in the same DOM order.
        raw_pairs: list[dict] = page.evaluate(
            "() => {"
            "  const titles = [...document.querySelectorAll('h2')]"
            "    .filter(el => /^\\d{4} /.test(el.innerText?.trim()));"
            "  const priceEls = [...document.querySelectorAll('*')]"
            "    .filter(el => el.children.length === 0"
            "      && /^£[\\d,]+$/.test(el.innerText?.trim()));"
            "  const results = [];"
            "  for (let i = 0; i < Math.min(titles.length, priceEls.length); i++) {"
            "    const yr = parseInt(titles[i].innerText.match(/^(\\d{4})/)[1]);"
            "    const price = parseInt(priceEls[i].innerText.replace('£','').replace(/,/g,''));"
            "    if (price > 500 && price < 2000000) results.push({year: yr, price});"
            "  }"
            "  return results;"
            "}"
        )
        prices: list[int] = []
        for pair in raw_pairs:
            yr = pair.get("year")
            price = pair.get("price", 0)
            if year_from and yr and yr < year_from:
                continue
            if year_to and yr and yr > year_to:
                continue
            if 500 < price < 2_000_000:
                prices.append(price)
    else:
        raw_prices: list[str] = page.evaluate(
            "() => [...document.querySelectorAll('*')]"
            ".filter(el => el.children.length === 0 && el.innerText"
            "  && /^£[\\d,]+$/.test(el.innerText.trim()))"
            ".map(el => el.innerText.trim())"
        )
        prices = []
        for p_str in raw_prices:
            try:
                val = int(p_str.replace("£", "").replace(",", ""))
                if 500 < val < 2_000_000:
                    prices.append(val)
            except ValueError:
                pass

    return {"listing_count": count, "prices": prices}


# ── Sold prices: C&C auctions ─────────────────────────────────────────────────

def _fetch_cc_sold(page, make: str, query: str) -> list[int]:
    """
    Fetch sold auction prices from C&C auctions/results for last 6 months, GBP only.
    C&C stores page data as inline Inertia JSON; prices are in pence.
    Returns a list of sold prices in GBP.
    """
    make_param = make.lower().replace(" ", "+")
    query_param = query.replace(" ", "+")
    url = f"{CC_SOLD_BASE}?make={make_param}&q={query_param}"

    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    except Exception as exc:
        log.warning("C&C sold fetch failed for '%s': %s", query, exc)
        return []

    raw_json = page.evaluate(
        "() => { for (const s of document.querySelectorAll('script:not([src])')) {"
        "  const t = s.textContent.trim();"
        "  if (t.startsWith('{') && t.includes('searchResults')) return t;"
        "} return null; }"
    )
    if not raw_json:
        return []

    try:
        data = json.loads(raw_json)
        results = data["props"]["searchResults"]["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []

    prices: list[int] = []
    for item in results:
        if not item.get("isSold"):
            continue
        if item.get("auction", {}).get("hideSoldPrice"):
            continue

        price_data = item.get("price", {})
        if price_data.get("currency", {}).get("name") != "GBP":
            continue

        price_pence = price_data.get("value", 0)
        if not price_pence:
            continue

        end_str = item.get("auction", {}).get("endDatetime", "")
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        if end_dt < SIX_MONTHS_AGO:
            continue

        price_gbp = price_pence // 100
        if 500 < price_gbp < MAX_PRICE:
            prices.append(price_gbp)

    return prices


# ── Sold prices: Bring a Trailer ──────────────────────────────────────────────

def _fetch_bat_sold(page, bat_search: str) -> list[int]:
    """
    Fetch sold auction prices from Bring a Trailer for last 6 months.
    BaT has a public JSON API. Prices are in USD; converted to GBP at USD_TO_GBP.
    Returns a list of sold prices in GBP.
    """
    if not bat_search:
        return []

    query_param = bat_search.replace(" ", "+")
    # BaT API: returns auctions sorted by date, includes sold/unsold status
    url = (
        f"{BAT_API}?title={query_param}"
        f"&per_page=50&auction_type=auctions&auction_concluded=true"
    )

    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
    except Exception as exc:
        log.warning("BaT fetch failed for '%s': %s", bat_search, exc)
        return []

    try:
        body = page.evaluate("() => document.body.innerText")
        data = json.loads(body)
    except (json.JSONDecodeError, Exception):
        return []

    # BaT API response: {"auctions": [...]} or direct list
    auctions = data if isinstance(data, list) else data.get("auctions", [])

    prices: list[int] = []
    for auction in auctions:
        # Only sold (reserve met) results
        if not auction.get("sold_price") and not auction.get("price"):
            continue

        # Date check
        closed_str = auction.get("date_closed") or auction.get("end_date", "")
        try:
            closed_dt = datetime.fromisoformat(closed_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        if closed_dt < SIX_MONTHS_AGO:
            continue

        # Price in USD — convert to GBP
        price_usd = auction.get("sold_price") or auction.get("price", 0)
        if not price_usd:
            continue
        try:
            price_usd = int(str(price_usd).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            continue

        price_gbp = int(price_usd * USD_TO_GBP)
        if 500 < price_gbp < MAX_PRICE:
            prices.append(price_gbp)

    return prices


# ── Sold prices: Anglia Car Auctions ─────────────────────────────────────────

def _fetch_anglia_sold(make: str, keywords: list[str]) -> list[int]:
    """Fetch sold prices from Anglia Car Auctions (static HTML, requests).

    Prices are hammer + buyer's premium already included.
    """
    import requests
    from bs4 import BeautifulSoup

    url = f"https://angliacarauctions.co.uk/results/classic/{make.replace(' ', '%20')}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Anglia fetch failed for '%s': %s", make, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    containers = soup.find_all("div", class_=re.compile(r"col-12.*d-flex.*flex-column"))
    kw_lower = [k.lower() for k in keywords]

    prices: list[int] = []
    for container in containers:
        a_tag = container.find("a", href=re.compile(r"/auctions/\d+-\d+-[A-Za-z]+-\d{4}/"))
        if not a_tag:
            continue

        href = a_tag.get("href", "")
        m = re.search(r"/auctions/\d+-(\d+)-([A-Za-z]+)-(\d{4})/", href)
        if not m:
            continue
        month_map = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        day = int(m.group(1))
        mon = month_map.get(m.group(2).lower()[:3])
        year = int(m.group(3))
        if not mon:
            continue
        try:
            lot_date = datetime(year, mon, day, tzinfo=timezone.utc)
        except ValueError:
            continue
        if lot_date < SIX_MONTHS_AGO:
            continue

        lot_slug = href.rstrip("/").split("/")[-1].lower()
        if not all(kw in lot_slug for kw in kw_lower):
            continue

        price_span = container.find("span", class_="fw-bold")
        if not price_span:
            continue
        m2 = re.search(r"Result inc Premium:\s*£([\d,]+)", price_span.get_text())
        if not m2:
            continue
        try:
            gbp = int(m2.group(1).replace(",", ""))
            if 500 < gbp < MAX_PRICE:
                prices.append(gbp)
        except ValueError:
            continue

    return prices


# ── Sold prices: Morris Leslie ────────────────────────────────────────────────

_ML_ROWS_CACHE: list[list[str]] | None = None
_ML_CACHE_LOADED = False

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _load_morris_leslie_rows(page) -> list[list[str]]:
    """Load all rows from recent Morris Leslie sales into memory (called once)."""
    global _ML_ROWS_CACHE, _ML_CACHE_LOADED
    if _ML_CACHE_LOADED:
        return _ML_ROWS_CACHE or []

    _ML_CACHE_LOADED = True
    _ML_ROWS_CACHE = []

    try:
        page.goto(
            "https://auction.morrisleslie.com/classic-auction-results",
            timeout=20_000, wait_until="domcontentloaded",
        )
        page.wait_for_timeout(1000)
    except Exception as exc:
        log.warning("Morris Leslie index fetch failed: %s", exc)
        return []

    sale_links = page.evaluate(
        "() => [...document.querySelectorAll('a')]"
        ".map(a => ({href: a.href, text: a.textContent.trim()}))"
        ".filter(a => /(january|february|march|april|may|june|july|august|september|october|november|december)-\\d{4}/i.test(a.href))"
    )

    for link in sale_links:
        href = link["href"]
        slug = href.rstrip("/").split("/")[-1].lower()
        m = re.match(r"([a-z]+)-(\d{4})", slug)
        if not m:
            continue
        month = MONTH_NAMES.get(m.group(1))
        year = int(m.group(2))
        if not month:
            continue
        try:
            sale_date = datetime(year, month, 15, tzinfo=timezone.utc)
        except ValueError:
            continue
        if sale_date < SIX_MONTHS_AGO:
            continue

        log.info("Morris Leslie: loading %s", link.get("text", href))
        try:
            page.goto(href, timeout=20_000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception as exc:
            log.warning("Morris Leslie sale fetch failed %s: %s", href, exc)
            continue

        rows = page.evaluate(
            "() => [...document.querySelectorAll('table tbody tr')]"
            ".map(r => [...r.querySelectorAll('td')].map(td => td.innerText.trim()))"
        )
        _ML_ROWS_CACHE.extend(rows)
        log.info("  loaded %d rows from %s", len(rows), link.get("text", href))
        time.sleep(CRAWL_DELAY)

    log.info("Morris Leslie: %d total rows cached", len(_ML_ROWS_CACHE))
    return _ML_ROWS_CACHE


def _search_morris_leslie(page, make: str, keywords: list[str]) -> list[int]:
    """Search pre-fetched Morris Leslie rows by make and keywords."""
    rows = _load_morris_leslie_rows(page)
    make_lower = make.lower()
    kw_lower = [k.lower() for k in keywords if len(k) > 2]

    prices: list[int] = []
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
        m = re.search(r"£([\d,]+)", row[8])
        if not m:
            continue
        try:
            gbp = int(m.group(1).replace(",", ""))
            if 500 < gbp < MAX_PRICE:
                prices.append(gbp)
        except ValueError:
            continue

    return prices


# ── Sold prices: Iconic Auctioneers (formerly Silverstone Auctions) ───────────

_ICONIC_LOTS: list[dict] | None = None
_ICONIC_LOADED = False

ICONIC_ARCHIVE = "https://www.iconicauctioneers.com/auction/archive-auctions-cars"


def _load_iconic_lots(page) -> list[dict]:
    """Pre-fetch all classic car lots from Iconic Auctioneers within last 6 months."""
    global _ICONIC_LOTS, _ICONIC_LOADED
    if _ICONIC_LOADED:
        return _ICONIC_LOTS or []

    _ICONIC_LOADED = True
    _ICONIC_LOTS = []

    try:
        page.goto(ICONIC_ARCHIVE, timeout=20_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("Iconic archive fetch failed: %s", exc)
        return []

    auction_links = page.evaluate(
        "() => [...document.querySelectorAll('a[href*=\"/ipp-\"]')]"
        ".map(a => a.href)"
    )

    for link in auction_links:
        # Date is in URL: /{slug}/{YYYY-MM-DD}/ipp-N
        date_m = re.search(r"/(\d{4}-\d{2}-\d{2})/ipp-", link)
        if not date_m:
            continue
        try:
            sale_date = datetime.fromisoformat(date_m.group(1)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if sale_date < SIX_MONTHS_AGO:
            continue

        # Ensure we request ipp-100 (all lots per page)
        url = re.sub(r"/ipp-\d+", "/ipp-100", link)
        log.info("Iconic Auctioneers: loading %s", url)
        try:
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception as exc:
            log.warning("Iconic lot page failed %s: %s", url, exc)
            continue

        text = page.evaluate("() => document.body.innerText")
        # Each lot block: title line then "Sold for: £N,NNN incl. premium"
        for m in re.finditer(
            r"Lot Number: \d+\n+(.+?)\n.*?Sold for: £([\d,]+)",
            text, re.DOTALL,
        ):
            title = m.group(1).strip()
            try:
                price = int(m.group(2).replace(",", ""))
            except ValueError:
                continue
            if 500 < price < MAX_PRICE:
                _ICONIC_LOTS.append({"title": title.lower(), "price": price})

        log.info("  %d lots loaded so far", len(_ICONIC_LOTS))
        time.sleep(CRAWL_DELAY)

    log.info("Iconic Auctioneers: %d total lots cached", len(_ICONIC_LOTS))
    return _ICONIC_LOTS


def _search_iconic(make: str, keywords: list[str]) -> list[int]:
    """Search pre-fetched Iconic Auctioneers lots by make and keywords."""
    lots = _ICONIC_LOTS or []
    make_lower = make.lower()
    kw_lower = [k.lower() for k in keywords if len(k) > 2]

    prices = []
    for lot in lots:
        title = lot["title"]
        if make_lower not in title:
            continue
        if not all(kw in title for kw in kw_lower):
            continue
        prices.append(lot["price"])
    return prices


# ── Sold prices: H&H Classics ─────────────────────────────────────────────────

_HNH_LOTS: list[dict] | None = None
_HNH_LOADED = False

HNH_ARCHIVE = (
    "https://handh.co.uk/auctions/auction-results/"
    "?sale_type=motor-cars&auction_year={year}&posts_per_page=24"
)


def _load_hnh_lots(page) -> list[dict]:
    """Pre-fetch all H&H Classics car lots within last 6 months."""
    global _HNH_LOTS, _HNH_LOADED
    if _HNH_LOADED:
        return _HNH_LOTS or []

    _HNH_LOADED = True
    _HNH_LOTS = []

    MONTH_ABBR = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    # Collect auction URLs from current and prior year (spans 6-month window)
    auction_urls: list[str] = []
    current_year = datetime.now(timezone.utc).year
    for yr in (current_year, current_year - 1):
        try:
            page.goto(
                HNH_ARCHIVE.format(year=yr),
                timeout=30_000, wait_until="load",
            )
            page.wait_for_timeout(3000)
        except Exception as exc:
            log.warning("H&H archive fetch failed (%d): %s", yr, exc)
            # Try the page without year filter as fallback
            try:
                page.goto(
                    "https://handh.co.uk/auctions/auction-results/"
                    f"?sale_type=motor-cars&posts_per_page=24",
                    timeout=30_000, wait_until="load",
                )
                page.wait_for_timeout(3000)
            except Exception as exc2:
                log.warning("H&H archive fallback also failed: %s", exc2)
                continue

        try:
            entries = page.evaluate(
                "() => [...document.querySelectorAll('a[href*=\"/auction/\"]')]"
                ".filter(a => a.textContent.trim() === 'View Auction Results')"
                ".map(a => {"
                "  const parent = a.closest('section') || a.parentElement?.parentElement?.parentElement;"
                "  const dateMatch = parent?.innerText?.match(/Date:\\s*(.+?)[\\n\\r]/);"
                "  return {href: a.href, dateText: dateMatch?.[1] || ''};"
                "})"
            )
        except Exception as exc:
            log.warning("H&H archive evaluate failed: %s", exc)
            continue

        for entry in entries:
            date_text = entry.get("dateText", "")
            # Parse "14th Oct, 2026 - 12:00pm"
            dm = re.search(r"(\d+)\w*\s+(\w{3}),?\s+(\d{4})", date_text)
            if not dm:
                continue
            mon = MONTH_ABBR.get(dm.group(2).lower()[:3])
            if not mon:
                continue
            try:
                sale_date = datetime(int(dm.group(3)), mon, int(dm.group(1)), tzinfo=timezone.utc)
            except ValueError:
                continue
            if sale_date < SIX_MONTHS_AGO:
                continue
            # Skip motorcycle-only events (title clue in URL)
            if "motorcycle" in entry["href"].lower():
                continue
            auction_urls.append(entry["href"])

    for url in auction_urls:
        full_url = f"{url}?pgn=1&order_by=lot&posts_per_page=300"
        log.info("H&H Classics: loading %s", url)
        try:
            page.goto(full_url, timeout=30_000, wait_until="load")
            page.wait_for_timeout(3000)
        except Exception as exc:
            log.warning("H&H lot page failed %s: %s", url, exc)
            continue

        try:
            text = page.evaluate("() => document.body.innerText")
        except Exception as exc:
            log.warning("H&H lot page evaluate failed %s: %s", url, exc)
            continue
        # Format: "{Year} {Make} {Model}\n...\nSold for\n£X,XXX"
        for m in re.finditer(r"(\d{4}\s+[A-Za-z].{5,80}?)\n.*?Sold for\s*\n\s*£([\d,]+)", text, re.DOTALL):
            title = m.group(1).strip()
            try:
                price = int(m.group(2).replace(",", ""))
            except ValueError:
                continue
            if 500 < price < MAX_PRICE:
                _HNH_LOTS.append({"title": title.lower(), "price": price})

        log.info("  %d lots loaded so far", len(_HNH_LOTS))
        time.sleep(CRAWL_DELAY)

    log.info("H&H Classics: %d total lots cached", len(_HNH_LOTS))
    return _HNH_LOTS


def _search_hnh(make: str, keywords: list[str]) -> list[int]:
    """Search pre-fetched H&H Classics lots by make and keywords."""
    lots = _HNH_LOTS or []
    make_lower = make.lower()
    kw_lower = [k.lower() for k in keywords if len(k) > 2]

    prices = []
    for lot in lots:
        title = lot["title"]
        if make_lower not in title:
            continue
        if not all(kw in title for kw in kw_lower):
            continue
        prices.append(lot["price"])
    return prices


# ── Sold prices: Mathewsons ────────────────────────────────────────────────────

_MATHEWSONS_LOTS: list[dict] | None = None
_MATHEWSONS_LOADED = False

MATHEWSONS_ARCHIVE = "https://www.mathewsons.co.uk/auction-results/"


def _load_mathewsons_lots(page) -> list[dict]:
    """Pre-fetch all Mathewsons classic vehicle lots within last 6 months."""
    global _MATHEWSONS_LOTS, _MATHEWSONS_LOADED
    if _MATHEWSONS_LOADED:
        return _MATHEWSONS_LOTS or []

    _MATHEWSONS_LOADED = True
    _MATHEWSONS_LOTS = []

    try:
        page.goto(MATHEWSONS_ARCHIVE, timeout=20_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("Mathewsons archive fetch failed: %s", exc)
        return []

    # Get "View Results" links for "Classic Vehicles" sales (not timed/other)
    entries = page.evaluate(
        "() => [...document.querySelectorAll('a')].filter(a => a.textContent.trim() === 'View Results'"
        " && /classic-vehicles|memorabilia/i.test(a.href))"
        ".map(a => {"
        "  const parent = a.closest('[class*=card], article, section') || a.parentElement?.parentElement?.parentElement?.parentElement;"
        "  const text = parent?.innerText || '';"
        "  return {href: a.href, ctx: text.substring(0,200)};"
        "})"
    )

    MONTH_ABBR = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    for entry in entries:
        # Date in ctx: "23rd Sep, 2026 10:00"
        dm = re.search(r"(\d+)\w*\s+(\w{3}),?\s+(\d{4})", entry["ctx"])
        if dm:
            mon = MONTH_ABBR.get(dm.group(2).lower()[:3])
            if mon:
                try:
                    sale_date = datetime(int(dm.group(3)), mon, int(dm.group(1)), tzinfo=timezone.utc)
                    if sale_date < SIX_MONTHS_AGO:
                        continue
                except ValueError:
                    pass

        # Replace results_per_page to get 96 per page (all vehicles category)
        href = entry["href"]
        au_m = re.search(r"au=(\d+)", href)
        if not au_m:
            continue
        au_id = au_m.group(1)
        base_slug = href.split("?")[0]
        url = f"{base_slug}?au={au_id}&results_per_page=96&category=Vehicles"

        log.info("Mathewsons: loading %s", url)
        page_no = 1
        while True:
            try:
                fetch_url = f"{url}&page={page_no}" if page_no > 1 else url
                page.goto(fetch_url, timeout=30_000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            except Exception as exc:
                log.warning("Mathewsons lot page failed %s: %s", url, exc)
                break

            text = page.evaluate("() => document.body.innerText")
            matches = re.findall(r"(\d{4}\s+[A-Z][A-Z\s\-]{3,60}?)\n+Sold for £([\d,]+)", text)
            if not matches:
                break
            for title, price_str in matches:
                try:
                    price = int(price_str.replace(",", ""))
                except ValueError:
                    continue
                if 500 < price < MAX_PRICE:
                    _MATHEWSONS_LOTS.append({"title": title.strip().lower(), "price": price})

            # Check if there's a next page (96 results = possibly more)
            if len(matches) < 96:
                break
            page_no += 1
            time.sleep(1.0)

        log.info("  %d lots loaded so far", len(_MATHEWSONS_LOTS))
        time.sleep(CRAWL_DELAY)

    log.info("Mathewsons: %d total lots cached", len(_MATHEWSONS_LOTS))
    return _MATHEWSONS_LOTS


def _search_mathewsons(make: str, keywords: list[str]) -> list[int]:
    """Search pre-fetched Mathewsons lots by make and keywords."""
    lots = _MATHEWSONS_LOTS or []
    make_lower = make.lower()
    kw_lower = [k.lower() for k in keywords if len(k) > 2]

    prices = []
    for lot in lots:
        title = lot["title"]
        if make_lower not in title:
            continue
        if not all(kw in title for kw in kw_lower):
            continue
        prices.append(lot["price"])
    return prices


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_model(current: dict, prev: dict | None) -> float:
    """
    Returns a 0–100 signal score.

    Weights:
      - Supply scarcity       (fewer listings = rarer car)         25 pts
      - Supply contraction    (supply falling vs last run)         35 pts
      - Price momentum        (median asking price rising)         25 pts
      - Absolute supply floor (very tight supply = extra urgency)  15 pts
    """
    supply = current["listing_count"]
    price = current["median_price"]

    scarcity = max(0.0, 1.0 - (supply / 200)) * 25

    contraction = 0.0
    if prev and prev.get("supply", {}).get("current", 0) > 0:
        prev_supply = prev["supply"]["current"]
        pct_change = (supply - prev_supply) / prev_supply
        if pct_change < 0:
            contraction = min(35.0, abs(pct_change) * 200)

    momentum = 0.0
    if prev and prev.get("price", {}).get("median", 0) > 0 and price > 0:
        prev_price = prev["price"]["median"]
        pct_change = (price - prev_price) / prev_price
        if pct_change > 0:
            momentum = min(25.0, pct_change * 150)

    floor_bonus = 15.0 if supply < 10 else (10.0 if supply < 20 else 0.0)

    return round(min(100.0, scarcity + contraction + momentum + floor_bonus), 1)


def signal_label(score: float, supply: int) -> str:
    if score >= 60 or supply < 10:
        return "hot"
    if score >= 35 or supply < 30:
        return "rising"
    return "watch"


# ── Main run ──────────────────────────────────────────────────────────────────

def run() -> None:
    from playwright.sync_api import sync_playwright

    universe = load_universe()
    pinned_ids = load_pinned_ids()
    prev_map = load_prev_discoveries()

    candidates = [m for m in universe if m["id"] not in pinned_ids]
    log.info(
        "Discovery: %d candidates (%d universe, %d pinned excluded)",
        len(candidates), len(universe), len(pinned_ids),
    )

    scored: list[dict] = []

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

        # Pre-load caches for all auction-house sources (once per run)
        log.info("Pre-loading Morris Leslie sale data…")
        _load_morris_leslie_rows(page)
        log.info("Pre-loading Iconic Auctioneers lot data…")
        _load_iconic_lots(page)
        log.info("Pre-loading H&H Classics lot data…")
        _load_hnh_lots(page)
        log.info("Pre-loading Mathewsons lot data…")
        _load_mathewsons_lots(page)
        log.info("Pre-loading Historics Auctioneers lot data…")
        import historics_scraper as _historics
        _historics._load_lots(page)

        for model in candidates:
            mid = model["id"]
            make = model.get("make", "")
            query = model.get("cc_search", model["name"])
            bat_query = model.get("bat_search", model.get("cc_search", model["name"]))

            if model.get("cc_skip"):
                log.info("Scanning C&C live: %s — skipped (cc_skip=true)", mid)
                cc = {"listing_count": 0, "prices": []}
            else:
                log.info("Scanning C&C live: %s (%s)", mid, query)
                cc = _fetch_cc(page, make, query,
                               year_from=model.get("cc_year_from"),
                               year_to=model.get("cc_year_to"))
                time.sleep(CRAWL_DELAY)

            supply = cc["listing_count"]
            raw_prices = cc["prices"]

            true_median = int(statistics.median(raw_prices)) if raw_prices else 0
            if true_median and true_median > MAX_PRICE:
                log.info("Skipping %s: true median £%d > ceiling", mid, true_median)
                continue

            display_prices = [p for p in raw_prices if p <= MAX_PRICE]
            median_price = (
                int(statistics.median(display_prices)) if display_prices else true_median
            )

            # Sold prices: C&C auctions
            log.info("Scanning C&C sold: %s", mid)
            cc_sold = _fetch_cc_sold(page, make, query)
            time.sleep(CRAWL_DELAY)

            # Sold prices: Bring a Trailer
            log.info("Scanning BaT: %s (%s)", mid, bat_query)
            bat_sold = _fetch_bat_sold(page, bat_query)
            time.sleep(CRAWL_DELAY)

            # Sold prices: Anglia Car Auctions (static HTML, no Playwright needed)
            keywords = query.lower().split()
            anglia_sold = _fetch_anglia_sold(make, keywords)

            # Sold prices: Morris Leslie (pre-fetched cache)
            ml_sold = _search_morris_leslie(page, make, keywords)

            # Sold prices: Iconic Auctioneers (pre-fetched cache)
            iconic_sold = _search_iconic(make, keywords)

            # Sold prices: H&H Classics (pre-fetched cache)
            hnh_sold = _search_hnh(make, keywords)

            # Sold prices: Mathewsons (pre-fetched cache)
            mathewsons_sold = _search_mathewsons(make, keywords)

            # Sold prices: Collecting Cars (UK only, Playwright)
            import collecting_cars_scraper as _cc_cars
            cc_cars_sold = _cc_cars.fetch_sold(page, query, keywords)
            time.sleep(2.0)

            # Sold prices: Historics Auctioneers (pre-fetched cache)
            historics_sold = _historics.search(page, make, keywords)

            all_sold = cc_sold + bat_sold + anglia_sold + ml_sold + iconic_sold + hnh_sold + mathewsons_sold + cc_cars_sold + historics_sold
            sold_median = int(statistics.median(all_sold)) if all_sold else None
            sold_count_6m = len(all_sold)

            if sold_count_6m:
                log.info(
                    "  sold (6m): %d results, median £%s "
                    "(C&C:%d BaT:%d Anglia:%d ML:%d Iconic:%d H&H:%d Math:%d CC:%d Hist:%d)",
                    sold_count_6m,
                    f"{sold_median:,}",
                    len(cc_sold), len(bat_sold), len(anglia_sold), len(ml_sold),
                    len(iconic_sold), len(hnh_sold), len(mathewsons_sold),
                    len(cc_cars_sold), len(historics_sold),
                )

            prev = prev_map.get(mid)
            sc = score_model({"listing_count": supply, "median_price": median_price}, prev)

            prev_supply = prev["supply"]["current"] if prev else None
            supply_delta = (supply - prev_supply) if prev_supply is not None else 0
            prev_price = prev["price"]["median"] if prev else None
            price_delta_pct = (
                round((median_price - prev_price) / prev_price * 100)
                if prev_price and prev_price > 0 and median_price > 0
                else 0
            )

            scored.append({
                "id": mid,
                "name": model["name"],
                "make": make,
                "years": f"{model['years'][0]}–{model['years'][1]}",
                "signal": signal_label(sc, supply),
                "score": sc,
                "supply": {"current": supply, "delta_4w": supply_delta},
                "price": {
                    "median": median_price,
                    "delta_pct": price_delta_pct,
                    "sold_median_6m": sold_median,
                    "sold_count_6m": sold_count_6m,
                },
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })
            log.info(
                "  → supply %d, asking £%s, score %.1f (%s)",
                supply,
                f"{median_price:,}" if median_price else "n/a",
                sc,
                signal_label(sc, supply),
            )

        browser.close()

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:TOP_N]

    if DISCOVERIES_JSON.exists():
        shutil.copy(DISCOVERIES_JSON, PREV_DISCOVERIES_JSON)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe) - len(pinned_ids),
        "models": top,
    }

    with DISCOVERIES_JSON.open("w") as f:
        json.dump(output, f, indent=2)

    hot = sum(1 for m in top if m["signal"] == "hot")
    rising = sum(1 for m in top if m["signal"] == "rising")
    log.info("Discovery: scored %d models. %d hot, %d rising.", len(top), hot, rising)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    run()
