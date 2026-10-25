"""Discovery engine — scans universe models via Car & Classic (Playwright),
scores by signal strength, writes data/discoveries.json for the dashboard.

Data sources:
  - Live listings:  carandclassic.com/search  (supply + asking prices)
  - Sold prices:    carandclassic.com/auctions/results  (C&C auction results, GBP, last 6m)
  - Sold prices:    bringatrailer.com/wp-json/bat/v1/search  (BaT results, USD→GBP, last 6m)

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

def _fetch_cc(page, make: str, query: str) -> dict:
    """Fetch listing count and asking prices from C&C search for a specific model."""
    make_param = make.lower().replace(" ", "+")
    query_param = query.replace(" ", "+")
    url = f"{CC_BASE}?make={make_param}&q={query_param}"

    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("C&C live fetch failed for '%s': %s", query, exc)
        return {"listing_count": 0, "prices": []}

    count = 0
    for h2 in page.query_selector_all("h2"):
        text = h2.inner_text()
        m = re.search(r"([\d,]+)\s+listings?", text, re.IGNORECASE)
        if m:
            count = int(m.group(1).replace(",", ""))
            break

    raw_prices: list[str] = page.evaluate(
        "() => [...document.querySelectorAll('*')]"
        ".filter(el => el.children.length === 0 && el.innerText"
        "  && /^£[\\d,]+$/.test(el.innerText.trim()))"
        ".map(el => el.innerText.trim())"
    )
    prices: list[int] = []
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

        for model in candidates:
            mid = model["id"]
            make = model.get("make", "")
            query = model.get("cc_search", model["name"])
            bat_query = model.get("bat_search", model.get("cc_search", model["name"]))

            log.info("Scanning C&C live: %s (%s)", mid, query)
            cc = _fetch_cc(page, make, query)
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

            all_sold = cc_sold + bat_sold
            sold_median = int(statistics.median(all_sold)) if all_sold else None
            sold_count_6m = len(all_sold)

            if sold_count_6m:
                log.info(
                    "  sold (6m): %d results, median £%s (C&C: %d, BaT: %d)",
                    sold_count_6m,
                    f"{sold_median:,}",
                    len(cc_sold),
                    len(bat_sold),
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
