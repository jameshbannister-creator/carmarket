"""Discovery engine — scans the universe of candidate models, scores by signal strength,
and writes data/discoveries.json for the dashboard Radar section.

Uses AutoTrader UK via Playwright (JS-rendered, no bot-check). Pinned models
(data/models.json) are excluded from output; they have their own tracked section.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UNIVERSE_JSON = DATA_DIR / "universe.json"
MODELS_JSON = DATA_DIR / "models.json"
DISCOVERIES_JSON = DATA_DIR / "discoveries.json"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
PREV_DISCOVERIES_JSON = DATA_DIR / "discoveries_prev.json"

AT_SEARCH_URL = "https://www.autotrader.co.uk/car-search"
AT_POSTCODE   = "SW1A1AA"
AT_RADIUS     = "1500"   # nationwide
CRAWL_DELAY   = 2.5      # seconds between Playwright page loads


def _at_make(make: str) -> str:
    """Convert universe make name to AutoTrader URL format (uppercase, spaces to +)."""
    return make.upper().replace(" ", "+")

TOP_N     = 999        # surface all scored models — dashboard filters by signal
MAX_PRICE = 30_000     # only surface models with median price at or under this


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
# AutoTrader scraper via Playwright
# ──────────────────────────────────────────────────────────────

def _fetch_at(page, make: str, year_from: int, year_to: int) -> dict:
    """Fetch listing count and prices from AutoTrader using make + year range.

    AutoTrader's keyword search doesn't work reliably; make + year range does.
    """
    at_make = _at_make(make)
    url = (
        f"{AT_SEARCH_URL}"
        f"?make={at_make}&year-from={year_from}&year-to={year_to}"
        f"&postcode={AT_POSTCODE}&radius={AT_RADIUS}"
        f"&include-delivery-option=on"
    )
    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("AutoTrader page load failed for '%s' %s-%s: %s", make, year_from, year_to, exc)
        return {"listing_count": 0, "prices": [], "median_price": 0}

    txt = page.inner_text("body")

    # Listing count — AutoTrader renders e.g. "342 results"
    count = 0
    for pat in [r"([\d,]+)\s+results?", r"([\d,]+)\s+cars?\s+found"]:
        m = re.search(pat, txt, re.I)
        if m:
            count = int(m.group(1).replace(",", ""))
            break

    # Prices — "£18,995" style (listing prices only, 500–500k range)
    prices: list[int] = []
    for m in re.finditer(r"£([\d,]+)", txt):
        val = int(m.group(1).replace(",", ""))
        if 500 < val < 500_000:
            prices.append(val)
    prices = sorted(set(prices))

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
    from playwright.sync_api import sync_playwright

    universe = load_universe()
    pinned_ids = load_pinned_ids()
    prev_map = load_prev_discoveries()

    log.info("Discovery engine: %d universe models, %d pinned (excluded)", len(universe), len(pinned_ids))

    scored: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        for model in universe:
            if model["id"] in pinned_ids:
                log.debug("Skipping pinned model: %s", model["id"])
                continue

            make = model["make"]
            year_from, year_to = model["years"][0], model["years"][1]
            log.info("Scanning: %s (%s %s-%s)", model["id"], make, year_from, year_to)
            cc = _fetch_at(page, make, year_from, year_to)
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

        browser.close()

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
