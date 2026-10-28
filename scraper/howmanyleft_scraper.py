"""How Many Left scraper — tracks UK licensed/SORN counts for all dashboard models.

Source: howmanyleft.co.uk (DVLA data, updated quarterly)
Runs monthly; saves snapshots to data/snapshots/howmanyleft_YYYY-MM-DD.json.

The DVLA registers vehicles by the model name on the V5, with no chassis code.
Each universe model maps to one or more howmanyleft vehicle slugs; counts are
summed across all matching variants (auto, manual, estate, etc.).
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://www.howmanyleft.co.uk"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Manual overrides: model_id → list of howmanyleft slugs.
# Keys here take precedence over auto-detection.
# Use when auto-detection picks the wrong variants or misses some.
SLUG_OVERRIDES: dict[str, list[str]] = {
    "mercedes-w210-e55":   ["mercedes_e55_amg_auto"],
    "mercedes-w211-e55":   ["mercedes_e55_amg_auto"],   # same DVLA name, all gens combined
    "mercedes-c36-amg":    ["mercedes_c36_amg", "mercedes_c36_amg_auto"],
    "mercedes-c43-amg":    ["mercedes_c43_amg", "mercedes_c43_amg_auto"],
    "mercedes-r129-sl":    ["mercedes_sl_320", "mercedes_sl_500", "mercedes_sl_600",
                            "mercedes_sl_320_auto", "mercedes_sl_500_auto", "mercedes_sl_600_auto"],
    "bmw-e34-m5":          ["bmw_m5", "bmw_m5_auto"],
    "bmw-e39-m5":          ["bmw_m5", "bmw_m5_auto"],
    "bmw-e36-m3":          ["bmw_m3", "bmw_m3_auto", "bmw_m3_convertible"],
    "bmw-e46-m3":          ["bmw_m3", "bmw_m3_auto", "bmw_m3_convertible"],
    "bmw-m635csi":         ["bmw_m635_csi", "bmw_m635csi"],
    "bmw-850i":            ["bmw_850i", "bmw_850ci", "bmw_850ci_auto"],
    "porsche-914":         ["porsche_914", "porsche_914_6"],
    "porsche-924-turbo":   ["porsche_924_turbo", "porsche_924_carrera_gt"],
    "ford-escort-rs2000":  ["ford_escort_rs_2000", "ford_escort_rs2000"],
    "ford-escort-rs-turbo":["ford_escort_rs_turbo"],
    "renault-5-gt-turbo":  ["renault_5_gt_turbo"],
    "renault-alpine-a610": ["renault_alpine_a610", "alpine_a610"],
    "vw-corrado-vr6":      ["volkswagen_corrado_vr6"],
    "vw-golf-gti-mk1":     ["volkswagen_golf_gti"],
    "vw-golf-gti-mk2":     ["volkswagen_golf_gti", "volkswagen_golf_gti_16v"],
    "vw-scirocco-mk2":     ["volkswagen_scirocco"],
    "toyota-ae86":         ["toyota_corolla_gt_coupe", "toyota_ae86"],
    "nissan-skyline-r32-gtst": ["nissan_skyline"],
    "honda-civic-ek9":     ["honda_civic_type_r", "honda_civic_type-r"],
    "honda-integra-dc2":   ["honda_integra_type_r", "honda_integra"],
    "alfa-75-v6":          ["alfa_romeo_75"],
    "alfa-brera":          ["alfa_romeo_brera", "alfa_romeo_brera_auto"],
    "lancia-montecarlo":   ["lancia_montecarlo", "lancia_beta_montecarlo"],
    "mazda-mx5-na":        ["mazda_mx-5", "mazda_mx-5_eunos"],
    "mazda-mx5-nb":        ["mazda_mx-5", "mazda_mx-5_auto"],
    "mazda-rx7-fc":        ["mazda_rx-7", "mazda_rx_7"],
    "mazda-rx7-fd":        ["mazda_rx-7", "mazda_rx-7_turbo"],
    "honda-crx":           ["honda_crx", "honda_cr-x"],
    "toyota-mr2-sw20":     ["toyota_mr2", "toyota_mr_2"],
    "toyota-gt86":         ["toyota_gt86"],
    "toyota-celica-gt4":   ["toyota_celica", "toyota_celica_gt_four"],
    "nissan-200sx-s14":    ["nissan_200sx", "nissan_200_sx"],
    "subaru-sti-blobeye":  ["subaru_impreza", "subaru_impreza_wrx_sti"],
    "subaru-impreza-wrx":  ["subaru_impreza", "subaru_impreza_wrx"],
    "mitsubishi-evo4":     ["mitsubishi_carisma", "mitsubishi_lancer_evolution"],
    "ford-puma":           ["ford_puma"],
    "vauxhall-calibra-turbo": ["vauxhall_calibra", "vauxhall_calibra_turbo_4x4"],
    "range-rover-classic": ["land_rover_range_rover"],
    "land-rover-discovery-1": ["land_rover_discovery"],
    "jeep-cherokee-xj":    ["jeep_cherokee"],
    "morgan-plus-8":       ["morgan_plus_8"],
    "morgan-plus-4":       ["morgan_plus_4"],
    "caterham-seven":      ["caterham_7", "caterham_seven"],
    "tvr-griffith-500":    ["tvr_griffith"],
    "tvr-chimaera":        ["tvr_chimaera"],
    "tvr-tuscan":          ["tvr_tuscan", "tvr_tuscan_s"],
    "triumph-tr6":         ["triumph_tr6"],
    "mg-mgb-gt":           ["mg_mgb_gt", "mg_mgb"],
    "mg-tf":               ["mg_tf"],
    "saab-900-turbo":      ["saab_900", "saab_900_turbo"],
    "saab-9000-aero":      ["saab_9000", "saab_9000_aero"],
    "pontiac-trans-am":    ["pontiac_firebird", "pontiac_trans_am"],
    "corvette-c4":         ["chevrolet_corvette", "corvette"],
    # Fixed from first-pass failures
    "mercedes-w201-190e":  ["mercedes_190e", "mercedes_190e_2.3_16", "mercedes_190e_2.5-16",
                            "mercedes_190e_2.3-16_auto", "mercedes_190e_2.5-16_auto"],
    "mercedes-w126-500se": ["mercedes_500_sec_auto"],
    "audi-b5-rs4":         ["audi_rs4_quattro"],
    "toyota-gt86":         ["toyota_gt86_d-4s", "toyota_gt86_aero_d-4s",
                            "toyota_gt86_aero_d-4s_auto"],
    "mitsubishi-evo4":     ["mitsubishi_lancer_evolution_ix_gt",
                            "mitsubishi_lancer_evolution_viii_gsr"],
    "honda-integra-dc2":   ["honda_integra_at", "honda_integra_mt"],
    "renault-clio-wms2":   ["renault_clio_16v_williams", "renault_clio_williams_2",
                            "renault_clio_williams_3"],
    "lotus-elise-s1":      ["lotus_elise", "lotus_elise_111r", "lotus_elise_111r_(touring)",
                            "lotus_elise_111r_sports"],
    "mg-mgb-gt":           ["mg_mgb_gt", "mg_mgb_gt_v8"],
    "alfa-spider-916":     ["alfa_romeo_spider", "alfa_romeo_spider_1.8_twin_spark",
                            "alfa_romeo_spider_2.0_twin_spark",
                            "alfa_romeo_spider_3.0_v6"],
    "vauxhall-cavalier-gsi": ["vauxhall_cavalier_gsi", "vauxhall_cavalier_gsi_16v",
                              "vauxhall_cavalier_gsi_4x4"],
    "ford-capri-280":      ["ford_capri_280", "ford_capri_2.8_injection_s"],
    "range-rover-classic": ["land_rover_range_rover", "land_rover_range_rover_auto",
                            "land_rover_range_rover_vogue", "land_rover_range_rover_vogue_auto"],
    "mercedes-slk-r170":   ["mercedes_slk200_kompressor", "mercedes_slk200_kompressor_auto",
                            "mercedes_slk200_kompressor_auto.", "mercedes_slk_320_auto"],
    "bmw-850i":            ["bmw_850_i", "bmw_850_i_auto", "bmw_850_ci", "bmw_850_ci_auto",
                            "bmw_850_csi"],
    "bmw-z3-m-roadster":   ["bmw_z3_m_roadster", "bmw_z3m_roadster", "bmw_z3_m_coupe"],
    "porsche-914":         ["porsche_914_2.0", "porsche_914_1.7", "porsche_914_1.8"],
    "jaguar-xjs":          ["jaguar_xjs", "jaguar_xjs_3.6", "jaguar_xjs_auto",
                            "jaguar_xjs_he_auto"],
    "toyota-land-cruiser-90": ["toyota_land_cruiser_4.2_d", "toyota_land_cruiser_4.2_d_auto",
                               "toyota_land_cruiser_3.0_d"],
    "suzuki-vitara":       ["suzuki_vitara", "suzuki_vitara_auto"],
    "peugeot-106-rallye":  ["peugeot_106_rallye", "peugeot_106_rallye_s1",
                            "peugeot_106_rallye_s2"],
    "renault-megane-rs-mk1": ["renault_megane_r-sport_230_f1_r26",
                              "renault_megane_ren-sport_2265_cup",
                              "renault_megane_renault_sport_225"],
    "suzuki-vitara":       ["suzuki_vitara_jlx", "suzuki_vitara_jx",
                            "suzuki_vitara_1.6_jlx", "suzuki_vitara_1.6_jx"],
    "alfa-gtv-916":        ["alfa_romeo_gtv", "alfa_romeo_gtv_2.0_twin_spark",
                            "alfa_romeo_gtv_2.0_ts_lusso"],
}


def _get(session: requests.Session, url: str) -> str | None:
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        log.warning("HML fetch failed %s: %s", url, e)
        return None


def _search_slugs(session: requests.Session, query: str, make_prefix: str) -> list[str]:
    """Search howmanyleft and return vehicle slugs matching the make."""
    html = _get(session, f"{BASE}/?q={requests.utils.quote(query)}")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    slugs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/vehicle/") and href[9:].startswith(make_prefix):
            slugs.append(href[9:])  # strip /vehicle/
    return slugs


def _fetch_vehicle_data(session: requests.Session, slug: str) -> list[dict] | None:
    """Fetch quarterly licensed/SORN data for a vehicle slug."""
    html = _get(session, f"{BASE}/vehicle/{slug}")
    if not html:
        return None
    m = re.search(r"var tax_data = (\[.*?\]);", html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _make_prefix(make: str) -> str:
    """Convert make name to howmanyleft slug prefix."""
    slug = make.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    # Common aliases
    aliases = {
        "volkswagen": "volkswagen",
        "vw": "volkswagen",
        "mercedes-benz": "mercedes",
        "mercedes_benz": "mercedes",
        "alfa romeo": "alfa_romeo",
        "land rover": "land_rover",
    }
    return aliases.get(slug, slug)


def _model_search_query(model: dict) -> tuple[str, str]:
    """Return (search_query, make_prefix) for a model."""
    make = model.get("make", "")
    name = model.get("name", "")
    make_prefix = _make_prefix(make)

    # Strip chassis codes and generation labels from name to get a clean search term
    clean = name
    chassis_re = re.compile(
        r"\b(W\d{2,3}|E\d{2}|R\d{2,3}|C\d{3}|S\d{3}|"
        r"mk\d|mk\.?\d|gen\d|phase\d|series\s*\d|"
        r"\b[A-Z]\d{2,3}\b)\b",
        re.IGNORECASE,
    )
    clean = chassis_re.sub("", clean).strip()
    clean = re.sub(r"\s{2,}", " ", clean).strip()

    # Use just the model part (after make) for the query, prepended with make
    if make and clean.lower().startswith(make.lower()):
        model_part = clean[len(make):].strip(" -")
    else:
        model_part = clean

    query = f"{make} {model_part}".strip()
    return query, make_prefix


def _best_slugs(
    all_slugs: list[str], model: dict, make_prefix: str
) -> list[str]:
    """Pick the most relevant slugs for a model from a search result list."""
    name = model.get("name", "").lower()
    mid = model.get("id", "").lower()

    # Extract key model tokens (ignore make, chassis codes, generation words)
    ignore = {make_prefix, "amg", "auto", "mk", "gen", "series", "turbo"}
    # Pull meaningful tokens from the model id
    tokens = [
        t for t in re.split(r"[-_\s]", mid)
        if len(t) > 1 and t not in ignore and not re.match(r"^[ew]\d{2,3}$", t, re.I)
    ]

    scored = []
    for slug in all_slugs:
        score = sum(1 for t in tokens if t in slug.lower())
        if score > 0:
            scored.append((score, slug))

    scored.sort(key=lambda x: -x[0])
    if not scored:
        return []

    top = scored[0][0]
    # Keep all slugs within 1 point of the top score (catches auto/manual variants)
    return [s for sc, s in scored if sc >= top - 1]


def scrape_all(models: list[dict]) -> dict[str, dict]:
    """Scrape howmanyleft for all models. Returns {model_id: result_dict}."""
    session = requests.Session()
    session.headers.update(HEADERS)

    results: dict[str, dict] = {}
    today = datetime.now().strftime("%Y-%m-%d")

    for model in models:
        mid = model["id"]
        name = model.get("name", mid)
        log.info("HML: %s", name)

        # Determine slugs
        if mid in SLUG_OVERRIDES:
            slugs = SLUG_OVERRIDES[mid]
            source = "override"
        else:
            query, make_prefix = _model_search_query(model)
            found = _search_slugs(session, query, make_prefix)
            slugs = _best_slugs(found, model, make_prefix)
            source = "auto"
            if not slugs:
                log.warning("HML: no slugs found for %s (query: %s)", mid, query)
                results[mid] = {
                    "name": name, "slugs": [], "licensed": None,
                    "sorn": None, "date": today, "source": source, "error": "no_match",
                }
                time.sleep(0.3)
                continue

        # Fetch data for each slug, sum counts
        total_licensed = 0
        total_sorn = 0
        matched_slugs = []
        quarterly: list[dict] = []

        for slug in slugs:
            data = _fetch_vehicle_data(session, slug)
            if not data:
                log.warning("HML: no data for slug %s", slug)
                continue
            matched_slugs.append(slug)
            # Latest quarter
            if data:
                latest = data[0]
                total_licensed += latest.get("licensed", 0) or 0
                total_sorn += latest.get("sorn", 0) or 0
                # Merge quarterly data (by period)
                for q in data:
                    period = q.get("period", "")
                    existing = next((x for x in quarterly if x["period"] == period), None)
                    if existing:
                        existing["licensed"] = (existing.get("licensed") or 0) + (q.get("licensed") or 0)
                        existing["sorn"] = (existing.get("sorn") or 0) + (q.get("sorn") or 0)
                    else:
                        quarterly.append(dict(q))
            time.sleep(0.4)

        results[mid] = {
            "name": name,
            "slugs": matched_slugs,
            "licensed": total_licensed,
            "sorn": total_sorn,
            "total": total_licensed + total_sorn,
            "pct_taxed": round(100 * total_licensed / (total_licensed + total_sorn), 1)
                         if (total_licensed + total_sorn) else None,
            "quarterly": sorted(quarterly, key=lambda x: x["period"], reverse=True)[:20],
            "date": today,
            "source": source,
        }
        log.info(
            "HML: %s → %d licensed, %d SORN (%s)",
            name, total_licensed, total_sorn, ", ".join(matched_slugs),
        )
        time.sleep(0.6)

    return results


def save_snapshot(results: dict, out_dir: Path | None = None) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    if out_dir is None:
        out_dir = Path(__file__).parent.parent / "data" / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"howmanyleft_{today}.json"
    out.write_text(json.dumps(results, indent=2))
    return out


def load_all_models() -> list[dict]:
    """Load models from both models.json and universe.json, deduplicated by id."""
    root = Path(__file__).parent.parent / "data"
    seen: set[str] = set()
    models: list[dict] = []

    for fname in ("models.json", "universe.json"):
        path = root / fname
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for m in data.get("models", []):
            mid = m.get("id", "")
            if mid and mid not in seen:
                seen.add(mid)
                models.append(m)

    return models


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    models = load_all_models()
    log.info("Loaded %d models", len(models))
    results = scrape_all(models)

    out = save_snapshot(results)
    log.info("Saved → %s", out)

    # Summary
    found = {mid: r for mid, r in results.items() if r.get("licensed") is not None}
    missing = [mid for mid, r in results.items() if r.get("licensed") is None]
    print(f"\n{'Model':<40} {'Licensed':>10} {'SORN':>8} {'%Taxed':>8}")
    print("-" * 70)
    for mid, r in sorted(found.items(), key=lambda x: -(x[1]["licensed"] or 0)):
        print(f"{r['name'][:39]:<40} {r['licensed']:>10,} {r['sorn']:>8,} {r['pct_taxed']:>7.0f}%")
    if missing:
        print(f"\nNo match found for {len(missing)} models: {', '.join(missing[:10])}")
