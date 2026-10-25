"""Google Trends — interest_over_time for each model (UK, last 12 months)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from pytrends.request import TrendReq

log = logging.getLogger(__name__)

SLEEP_BETWEEN = 3  # seconds — Google Trends rate-limits aggressively


def scrape(models: list[dict[str, Any]]) -> dict[str, dict]:
    pytrends = TrendReq(hl="en-GB", tz=0, timeout=(10, 30), retries=2, backoff_factor=0.5)
    results: dict[str, dict] = {}

    for model in models:
        model_id: str = model["id"]
        query: str = model.get("bat_search", model["name"])  # reuse bat_search term — usually good

        log.info("Trends: %s (%s)", model_id, query)
        try:
            pytrends.build_payload(
                kw_list=[query],
                cat=610,          # category: Vehicles
                timeframe="today 12-m",
                geo="GB",
            )
            df = pytrends.interest_over_time()

            if df.empty or query not in df.columns:
                log.warning("Trends: no data for %s", model_id)
                results[model_id] = _empty(model_id)
                time.sleep(SLEEP_BETWEEN)
                continue

            series = df[query].tolist()
            current = int(series[-1]) if series else 0
            peak = int(max(series)) if series else 0

            results[model_id] = {
                "trend_data": series,
                "current_interest": current,
                "peak_12mo": peak,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
            log.info("Trends: %s → current=%d, peak=%d", model_id, current, peak)

        except Exception as exc:
            log.error("Trends error for %s: %s", model_id, exc)
            results[model_id] = _empty(model_id)

        time.sleep(SLEEP_BETWEEN)

    return results


def _empty(model_id: str) -> dict:
    return {
        "trend_data": [],
        "current_interest": 0,
        "peak_12mo": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "error": "no_data",
    }
