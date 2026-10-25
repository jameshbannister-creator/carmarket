"""Discovery engine — scans the universe of candidate models, scores by signal strength,
and writes data/discoveries.json with the top 20 finds for the dashboard Radar section.

Pinned models (data/models.json) are excluded from output; they have their own tracked section.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UNIVERSE_JSON = DATA_DIR / "universe.json"
MODELS_JSON = DATA_DIR / "models.json"
DISCOVERIES_JSON = DATA_DIR / "discoveries.json"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

# Previous discoveries snapshot for delta calculations
PREV_DISCOVERIES_JSON = DATA_DIR / "discoveries_prev.json"

CC_SEARCH_URL = "https://www.carandclassic.com/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
CRAWL_DELAY = 2.0  # seconds between requests

TOP_N = 20          # models to surface on the radar
MAX_PRICE = 30_000  # only surface models with median price at or under this


# ──────────────────────────────────────────────────────────────
# Config loading
# ──────────────────────────────────────────────────────────────

def load_universe() -> list[dict]:
    with UNIVERSE_JSON.open() as f:
        return json.load(f)["models"]


def load_pinned_ids() -> set[str]:
    with MODELS_JSON.open() as f:
        return {m["id"] for m in json.load(f)["models"]}


def load_prev_discoveries() -> dict[str, dict]:
    """Returns {model_id: record} from last run's discoveries, for delta tracking."""
    for path in (PREV_DISCOVERIES_JSON, DISCOVERIES_JSON):
        if path.exists():
            try:
                with path.open() as f:
                    data = json.load(f)
                return {m["id"]: m for m in data.get("models", [])}
            except (json.JSONDecodeError, KeyError):
                pass
    return {}


# ──────────────────────────────────────────────────────────────
# Car & Classic scraper (lightweight version)
# ──────────────────────────────────────────────────────────────

def _fetch_cc(session: requests.Session, search_term: str) -> dict:
    """Returns {listing_count, prices: [int], median_price: int}"""
    params = {"make": "", "q": search_term}
    try:
        resp = session.get(CC_SEARCH_URL, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("C&C fetch failed for '%s': %s", search_term, exc)
        return {"listing_count": 0, "prices": [], "median_price": 0}

    soup = BeautifulSoup(resp.text, "html.parser")

    # Listing count
    count = 0
    for sel in (".search-count", ".results-count", "[data-testid='search-count']"):
        el = soup.select_one(sel)
        if el:
            import re
            m = re.search(r"[\d,]+", el.get_text())
            if m:
                count = int(m.group().replace(",", ""))
                break
    if count == 0:
        cards = soup.select(".listing-card, [data-listing-id], .search-result")
        count = len(cards)

    # Prices
    import re
    prices: list[int] = []
    for el in soup.select(".listing-price, .price, [class*='price']"):
        txt = el.get_text(strip=True)
        m = re.search(r"£([\d,]+)", txt)
        if m:
            val = int(m.group(1).replace(",", ""))
            if 500 < val < 2_000_000:
                prices.append(val)

    median_price = int(statistics.median(prices)) if prices else 0
    return {"listing_count": count, "prices": prices, "median_price": median_price}


# ──────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────

def score_model(current: dict, prev: dict | None) -> float:
    """
    Returns a 0–100 signal score.

    Weights:
      - Supply scarcity       (fewer listings = rarer car)         25 pts
      - Supply contraction    (supply falling vs last run)         35 pts
      - Price momentum        (median price rising vs last run)    25 pts
      - Absolute supply floor (very tight supply = extra urgency)  15 pts
    """
    supply = current["listing_count"]
    price = current["median_price"]

    # Supply scarcity score: <20 listings = 25pts, scales down to 200+
    scarcity = max(0.0, 1.0 - (supply / 200)) * 25

    # Supply contraction vs previous run
    contraction = 0.0
    if prev and prev.get("supply", {}).get("current", 0) > 0:
        prev_supply = prev["supply"]["current"]
        if prev_supply > 0:
            pct_change = (supply - prev_supply) / prev_supply
            if pct_change < 0:
                contraction = min(35.0, abs(pct_change) * 200)

    # Price momentum vs previous run
    momentum = 0.0
    if prev and prev.get("price", {}).get("median", 0) > 0 and price > 0:
        prev_price = prev["price"]["median"]
        pct_change = (price - prev_price) / prev_price
        if pct_change > 0:
            momentum = min(25.0, pct_change * 150)

    # Absolute supply floor bonus: <10 is critical
    floor_bonus = 15.0 if supply < 10 else (10.0 if supply < 20 else 0.0)

    total = scarcity + contraction + momentum + floor_bonus
    return round(min(100.0, total), 1)


def signal_label(score: float, supply: int) -> str:
    if score >= 60 or supply < 10:
        return "hot"
    if score >= 35 or supply < 30:
        return "rising"
    return "watch"


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def run() -> None:
    universe = load_universe()
    pinned_ids = load_pinned_ids()
    prev_map = load_prev_discoveries()

    log.info("Discovery engine: %d universe models, %d pinned (excluded)", len(universe), len(pinned_ids))

    session = requests.Session()
    session.headers.update(HEADERS)

    scored: list[dict] = []

    for model in universe:
        if model["id"] in pinned_ids:
            log.debug("Skipping pinned model: %s", model["id"])
            continue

        log.info("Scanning: %s (%s)", model["id"], model["cc_search"])
        cc = _fetch_cc(session, model["cc_search"])
        time.sleep(CRAWL_DELAY)

        # Skip if median price is known and above the ceiling
        if cc["median_price"] and cc["median_price"] > MAX_PRICE:
            log.info("Skipping %s: median £%d > £%d ceiling", model["id"], cc["median_price"], MAX_PRICE)
            continue

        prev = prev_map.get(model["id"])
        sc = score_model(cc, prev)

        prev_supply = prev["supply"]["current"] if prev else None
        supply_delta = (cc["listing_count"] - prev_supply) if prev_supply is not None else 0
        prev_price = prev["price"]["median"] if prev else None
        price_delta_pct = (
            round((cc["median_price"] - prev_price) / prev_price * 100)
            if prev_price and prev_price > 0 and cc["median_price"] > 0
            else 0
        )

        scored.append({
            "id": model["id"],
            "name": model["name"],
            "make": model["make"],
            "years": f"{model['years'][0]}–{model['years'][1]}",
            "signal": signal_label(sc, cc["listing_count"]),
            "score": sc,
            "supply": {
                "current": cc["listing_count"],
                "delta_4w": supply_delta,
            },
            "price": {
                "median": cc["median_price"],
                "delta_pct": price_delta_pct,
            },
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:TOP_N]

    # Preserve previous discoveries before overwriting
    if DISCOVERIES_JSON.exists():
        import shutil
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
    log.info(
        "Discovery: scanned %d models, top %d surfaced. %d hot, %d rising.",
        len(scored), len(top), hot, rising,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    run()
