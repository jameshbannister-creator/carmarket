"""Orchestrator — runs all scrapers and writes data/latest.json + a dated snapshot."""
from __future__ import annotations

import json
import logging
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import cc_scraper
import bat_scraper
import ebay_scraper
import gumtree_scraper
import trends as trends_scraper
import discover

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run_all")

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LATEST_JSON = DATA_DIR / "latest.json"
MODELS_JSON = DATA_DIR / "models.json"
GBP_PER_USD = 0.79  # approximate; update periodically


# ──────────────────────────────────────────────────────────────
# Load config
# ──────────────────────────────────────────────────────────────
def load_models() -> list[dict]:
    with MODELS_JSON.open() as f:
        return json.load(f)["models"]


# ──────────────────────────────────────────────────────────────
# Historical snapshot helpers
# ──────────────────────────────────────────────────────────────
def load_previous_snapshot() -> dict | None:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = sorted(SNAPSHOTS_DIR.glob("*.json"))
    if not snapshots:
        return None
    with snapshots[-1].open() as f:
        return json.load(f)


def get_prev_supply(prev: dict | None, model_id: str) -> int | None:
    if not prev:
        return None
    for m in prev.get("models", []):
        if m["id"] == model_id:
            return m["supply"]["current"]
    return None


def get_prev_price(prev: dict | None, model_id: str) -> int | None:
    if not prev:
        return None
    for m in prev.get("models", []):
        if m["id"] == model_id:
            return m["price"]["median"]
    return None


def get_prev_history(prev: dict | None, model_id: str) -> list[int]:
    if not prev:
        return []
    for m in prev.get("models", []):
        if m["id"] == model_id:
            return m["supply"].get("history", [])
    return []


# ──────────────────────────────────────────────────────────────
# Signal calculation
# ──────────────────────────────────────────────────────────────
def calc_signal(supply_now: int, prev_supply: int | None, price_delta_pct: float, demand_score: int) -> str:
    if prev_supply and prev_supply > 0:
        supply_change_pct = (supply_now - prev_supply) / prev_supply * 100
        if supply_change_pct < -10 and demand_score > 75:
            return "hot"
        if supply_change_pct < -5 or price_delta_pct > 10:
            return "rising"
        if supply_change_pct > 5:
            return "watch"
    return "stable"


# ──────────────────────────────────────────────────────────────
# Demand score
# ──────────────────────────────────────────────────────────────
def calc_demand_score(ebay_data: dict, trends_data: dict) -> int:
    """Combine eBay watcher density and Google Trends into a 0-100 score."""
    watched = ebay_data.get("watched_count", 0)
    total = ebay_data.get("total_count", 1) or 1
    watcher_ratio = min(watched / total, 1.0)  # 0-1

    trend_current = trends_data.get("current_interest", 0)
    trend_peak = trends_data.get("peak_12mo", 1) or 1
    trend_ratio = min(trend_current / trend_peak, 1.0)  # 0-1

    # Weighted blend: watchers 60%, trends 40%
    score = int((watcher_ratio * 0.60 + trend_ratio * 0.40) * 100)
    # If no watcher data, fall back to trends only with a moderate floor
    if watcher_ratio == 0 and trend_ratio == 0:
        return 30  # default when we have no signal data yet
    return max(10, min(score, 100))


# ──────────────────────────────────────────────────────────────
# US gap calculation
# ──────────────────────────────────────────────────────────────
def calc_us_gap(median_gbp: int, bat_median_usd: int) -> dict:
    if bat_median_usd == 0 or median_gbp == 0:
        return {"pct": 0, "dir": "uk_cheaper"}
    bat_gbp = int(bat_median_usd * GBP_PER_USD)
    gap_pct = round((bat_gbp - median_gbp) / median_gbp * 100)
    return {
        "pct": abs(gap_pct),
        "dir": "uk_cheaper" if gap_pct > 0 else "us_cheaper",
    }


# ──────────────────────────────────────────────────────────────
# Alerts generation
# ──────────────────────────────────────────────────────────────
def generate_alerts(models_out: list[dict]) -> list[dict]:
    alerts = []
    for m in models_out:
        sig = m["signal"]
        name = m["name"]
        supply = m["supply"]["current"]

        if sig == "hot":
            alerts.append({
                "type": "hot",
                "text": f"<strong>{name}:</strong> Supply at {supply} listings. Demand score {m['demand']['score']}. Monitor closely.",
                "time": "Today",
            })
        elif sig == "rising":
            gap = m["us_gap"]["pct"]
            if gap > 20 and m["us_gap"]["dir"] == "uk_cheaper":
                alerts.append({
                    "type": "rising",
                    "text": f"<strong>{name}:</strong> Supply tightening. US market {gap}% above UK asking.",
                    "time": "Today",
                })
            else:
                alerts.append({
                    "type": "rising",
                    "text": f"<strong>{name}:</strong> Supply tightening. Price up {m['price']['delta_6mo']}% over 6 months.",
                    "time": "Today",
                })
        elif sig == "watch":
            delta = m["supply"]["delta_4w"]
            if delta > 0:
                alerts.append({
                    "type": "watch",
                    "text": f"<strong>{name}:</strong> {delta} new listings added over 4 weeks vs supply absorption. Seller pressure building.",
                    "time": "Today",
                })

    return alerts


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
def main() -> None:
    models_cfg = load_models()
    log.info("Loaded %d models from config", len(models_cfg))

    prev = load_previous_snapshot()
    if prev:
        log.info("Loaded previous snapshot for delta calculations")
    else:
        log.info("No previous snapshot — first run, deltas will be zero")

    # Run scrapers
    log.info("Running Car & Classic scraper…")
    cc_data = cc_scraper.scrape(models_cfg)

    log.info("Running Bring a Trailer scraper…")
    bat_data = bat_scraper.scrape(models_cfg)

    log.info("Running eBay UK scraper…")
    ebay_data = ebay_scraper.scrape(models_cfg)

    log.info("Running Gumtree UK scraper…")
    gumtree_data = gumtree_scraper.scrape(models_cfg)

    log.info("Running Google Trends…")
    trends_data = trends_scraper.scrape(models_cfg)

    # Build output models
    models_out: list[dict] = []

    for model in models_cfg:
        mid = model["id"]
        cc = cc_data.get(mid, {})
        bat = bat_data.get(mid, {})
        ebay = ebay_data.get(mid, {})
        gumtree = gumtree_data.get(mid, {})
        trend = trends_data.get(mid, {})

        # Supply — combine C&C and Gumtree counts
        cc_supply = cc.get("listing_count", 0)
        gumtree_supply = gumtree.get("listing_count", 0)
        supply_now = cc_supply + gumtree_supply
        prev_supply = get_prev_supply(prev, mid)
        prev_history = get_prev_history(prev, mid)
        history = (prev_history + [supply_now])[-8:]  # keep last 8 weeks
        delta_4w = supply_now - prev_history[-4] if len(prev_history) >= 4 else 0

        # Price — blend C&C and Gumtree prices; Gumtree skews private-seller
        prices = [p for p in cc.get("prices", []) if p > 0]
        gumtree_prices = [p for p in gumtree.get("prices", []) if p > 0]
        all_prices = prices + gumtree_prices
        median_gbp = int(statistics.median(all_prices)) if all_prices else get_prev_price(prev, mid) or 0
        gumtree_median = gumtree.get("median_price", 0)
        prev_price = get_prev_price(prev, mid) or median_gbp
        price_delta_pct = round((median_gbp - prev_price) / prev_price * 100) if prev_price else 0

        # Demand
        demand_score = calc_demand_score(ebay, trend)

        # US gap
        bat_median_usd = bat.get("median_usd", 0)
        us_gap = calc_us_gap(median_gbp, bat_median_usd)

        # Signal
        signal = calc_signal(supply_now, prev_supply, price_delta_pct, demand_score)

        # Tags
        tags: list[str] = []
        if model.get("us_rare"):
            tags.append("25yr")

        # DOM — not directly available from scraping; carry forward or default
        prev_dom = 0
        if prev:
            for pm in prev.get("models", []):
                if pm["id"] == mid:
                    prev_dom = pm["dom"]["median"]
                    break
        dom_trend = "stable"  # would need listing-level timestamps to compute

        models_out.append({
            "id": mid,
            "name": model["name"],
            "years": f"{model['years'][0]}–{model['years'][1]}",
            "tags": tags,
            "personal": model.get("personal", False),
            "signal": signal,
            "supply": {
                "current": supply_now,
                "cc": cc_supply,
                "gumtree": gumtree_supply,
                "delta_4w": delta_4w,
                "history": history,
            },
            "gumtree_median": gumtree_median,
            "price": {
                "median": median_gbp,
                "delta_6mo": price_delta_pct,
            },
            "dom": {
                "median": prev_dom,
                "trend": dom_trend,
            },
            "demand": {
                "score": demand_score,
            },
            "us_gap": us_gap,
            "alerts": [],
        })

    alerts = generate_alerts(models_out)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models": models_out,
        "alerts": alerts,
    }

    # Write latest.json
    with LATEST_JSON.open("w") as f:
        json.dump(output, f, indent=2)
    log.info("Wrote %s", LATEST_JSON)

    # Write dated snapshot
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_path = SNAPSHOTS_DIR / f"{today}.json"
    with snapshot_path.open("w") as f:
        json.dump(output, f, indent=2)
    log.info("Wrote snapshot %s", snapshot_path)

    hot_count = sum(1 for m in models_out if m["signal"] == "hot")
    rising_count = sum(1 for m in models_out if m["signal"] == "rising")
    log.info("Done. %d models. %d hot, %d rising, %d alerts.", len(models_out), hot_count, rising_count, len(alerts))

    # Run discovery engine across the broader universe
    log.info("Running discovery engine…")
    try:
        discover.run()
    except Exception as exc:
        log.error("Discovery engine failed (non-fatal): %s", exc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        sys.exit(1)
