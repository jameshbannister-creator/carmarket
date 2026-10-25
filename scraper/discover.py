"""Discovery engine — scans universe models via Car & Classic (Playwright),
scores by signal strength, writes data/discoveries.json for the dashboard.

Uses the cc_search field in universe.json for model-specific queries.
Pinned models (data/models.json) are excluded — they have their own tracked section.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
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
PREV_DISCOVERIES_JSON = DATA_DIR / "discoveries_prev.json"

CC_BASE = "https://www.carandclassic.com/search"
CRAWL_DELAY = 3.0
TOP_N = 999
MAX_PRICE = 30_000


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


def _fetch_cc(page, make: str, query: str) -> dict:
    """Fetch listing count and prices from Car & Classic for a specific model."""
    make_param = make.lower().replace(" ", "+")
    query_param = query.replace(" ", "+")
    url = f"{CC_BASE}?make={make_param}&q={query_param}"

    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("C&C load failed for '%s': %s", query, exc)
        return {"listing_count": 0, "prices": []}

    # Listing count — C&C shows "Showing X listings for Make" in an h2
    count = 0
    for h2 in page.query_selector_all("h2"):
        text = h2.inner_text()
        m = re.search(r"([\d,]+)\s+listings?", text, re.IGNORECASE)
        if m:
            count = int(m.group(1).replace(",", ""))
            break

    # Prices — pull leaf elements whose full text is a £ amount
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

            log.info("Scanning C&C: %s (%s)", mid, query)
            cc = _fetch_cc(page, make, query)
            time.sleep(CRAWL_DELAY)

            supply = cc["listing_count"]
            raw_prices = cc["prices"]

            # True median before ceiling filter — used to decide whether to skip
            true_median = int(statistics.median(raw_prices)) if raw_prices else 0
            if true_median and true_median > MAX_PRICE:
                log.info(
                    "Skipping %s: median £%d > £%d ceiling", mid, true_median, MAX_PRICE
                )
                continue

            # Display median uses only sub-ceiling prices
            display_prices = [p for p in raw_prices if p <= MAX_PRICE]
            median_price = (
                int(statistics.median(display_prices)) if display_prices else true_median
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
                "price": {"median": median_price, "delta_pct": price_delta_pct},
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })
            log.info(
                "  → %d listings, median £%s, score %.1f (%s)",
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
    log.info(
        "Discovery: scored %d models. %d hot, %d rising.",
        len(top), hot, rising,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    run()
