# Classic Car Market Intelligence — Tool Specification

## What this tool is for

A personal market intelligence dashboard for a buyer/collector of classic and sports cars in the UK.
The job is to spot cars before they become expensive: models where supply is quietly contracting,
where sold prices are rising, and where UK asking prices are still below where the market is heading.

Not a price guide. Not a broad database. A signal engine for opportunistic buying.

---

## Data sources

### 1. Car & Classic — live listings
- URL: `https://www.carandclassic.com/search?make={make}&q={query}`
- Scrape with Playwright (site is JS-rendered)
- Capture: listing count, all asking prices (GBP only)
- Run weekly

### 2. Car & Classic — sold auction results
- URL: `https://www.carandclassic.com/auctions/results?make={make}&q={query}`
- Data is embedded as inline Inertia JSON in a `<script>` tag
- Extract: `props.searchResults.data[]`
- Fields: `isSold`, `price.value` (pence — divide by 100), `price.currency.name`, `auction.endDatetime`, `auction.hideSoldPrice`
- Filter: `isSold=true`, `currency.name="GBP"`, not `hideSoldPrice`, within 6 months
- Captures what UK buyers actually paid at auction

### 3. Bring a Trailer — sold auction results
- API: `https://bringatrailer.com/wp-json/bat/v1/search?title={query}&per_page=50&auction_concluded=true`
- JSON response: `data.auctions[]` or direct list
- Fields: `sold_price` or `price` (USD), `date_closed` or `end_date`
- Convert USD to GBP at a fixed rate (update quarterly — currently 0.79)
- Captures the US collector market, which often leads UK pricing by 12–18 months
- Rate limit: be conservative, 2–3 second delays between calls

### 4. Gumtree — private seller listings
- URL: `https://www.gumtree.com/cars/{make}/{model}`
- Private sellers list lower than dealers; a Gumtree median below the C&C median signals
  motivated sellers or cars trading below dealer markup
- Requires different scraping approach (location-based results)

### 5. Pistonheads Classified
- Dealer and private listings, strong UK coverage for performance cars
- Supplement C&C where coverage is thin

### NOT authorised
- AutoTrader (never use — not approved)
- eBay (session cookie required, too fragile)

---

## Model universe

Defined in `data/universe.json`. Each model entry:

```json
{
  "id": "honda-integra-type-r-dc2",
  "name": "Honda Integra Type R DC2",
  "make": "honda",
  "years": [1995, 2001],
  "cc_search": "integra type r",
  "bat_search": "honda integra type r dc2",
  "gumtree_search": "honda integra type r",
  "notes": "JDM version not eligible for 25yr rule yet"
}
```

Two categories:
1. **Pinned models** (`data/models.json`): James's personal watchlist, tracked with full detail
2. **Discovery universe** (`data/universe.json`): ~140 candidates scanned for signals, top results shown

---

## Output schema per model

```json
{
  "id": "honda-integra-type-r-dc2",
  "name": "Honda Integra Type R DC2",
  "make": "honda",
  "years": "1995–2001",
  "signal": "hot",
  "score": 72.4,
  "supply": {
    "current": 3,
    "delta_4w": -2,
    "history": [8, 6, 5, 4, 3]
  },
  "price": {
    "median_ask": 19000,
    "ask_delta_pct": 12,
    "sold_median_6m": 14500,
    "sold_count_6m": 8,
    "sold_delta_pct": 18,
    "ask_vs_sold_pct": 31
  },
  "bat": {
    "median_usd": 22000,
    "median_gbp": 17380,
    "count_6m": 5,
    "uk_vs_bat_pct": 9
  },
  "scraped_at": "2026-10-25T08:00:00Z"
}
```

Key fields:
- `ask_vs_sold_pct`: how much asking prices exceed recent sold prices (positive = asking above sold)
- `uk_vs_bat_pct`: how much UK asking compares to BaT GBP equivalent (negative = UK cheaper than US)
- `sold_delta_pct`: change in sold median vs previous period

---

## Signal scoring (0–100)

Current weights (tunable):

| Signal | Weight | Description |
|---|---|---|
| Supply scarcity | 25 | Fewer listings = rarer |
| Supply contraction | 35 | Supply falling vs last run |
| Price momentum (ask) | 15 | Asking prices rising |
| Sold price momentum | 15 | Sold prices rising (stronger signal) |
| Floor bonus | 10 | Under 10 listings = urgency |

Signal labels:
- **hot** (scarce): score ≥ 60, or supply < 10
- **rising** (thinning): score ≥ 35, or supply 10–29
- **watch** (available): everything else

---

## Dashboard requirements

### Visual design
- Editorial cream/paper background, not a dark dashboard
- `--bg: #F0ECE4` light, `#0E0C09` dark
- Masthead: Instrument Serif italic, 80px, bold
- Numbers: Space Mono monospace
- Signals: brick red (hot), forest green (rising), navy (watch)
- No gradients, no icons, no card shadows — typographic hierarchy only

### Sections

1. **Stats band**: models tracked / scarce / thinning / universe size
2. **Feature strips**: top 6 scarcest models with supply number + asking + sold + premium/discount
3. **Full radar table**: all models with signal filter + search
   - Columns: Model, Listings, Asking, Sold (6m), US (BaT), Signal
4. **Personal watchlist**: pinned models, always shown regardless of signal

### Sold price display
- Show sold median if ≥ 3 results in 6 months (otherwise "—")
- Tooltip: "N sales in last 6 months"
- Ask vs sold premium: "+18% vs sold" in red if asking above sold (sellers holding firm)
  or "-5% vs sold" in green if asking below sold (buying opportunity)

### US market comparison
- Show BaT GBP equivalent where ≥ 3 BaT results
- "+12% US" means UK asking is 12% cheaper than US recent sales (import arbitrage signal)
- "-8% US" means UK market has already caught up or exceeded US

---

## Run schedule

- Weekly: Monday 08:00 UTC via GitHub Actions
- Full scan: all universe models (takes ~45 minutes with crawl delays)
- Output: `data/discoveries.json` (discovery universe) + `data/latest.json` (pinned models)

---

## What makes a good signal

A model is worth watching when:
1. Supply is below 15 on C&C AND contracting week-on-week
2. Sold prices from the last 6 months are rising vs the 6 months before
3. Asking prices are still below or close to sold prices (sellers haven't caught up yet)
4. BaT prices are substantially above UK asking (international demand pulling)
5. The model is approaching or has crossed the 25-year import eligibility threshold (US-spec cars)

The ideal buy window is when: supply is tightening, sold prices are moving, but asking prices
haven't caught up yet. This gap between sold and asking is the key metric.

---

## File structure

```
/data/
  universe.json          model definitions (~140 candidates)
  models.json            pinned watchlist models
  discoveries.json       latest discovery scan output
  discoveries_prev.json  previous scan for delta calculations
  latest.json            pinned model scan output
  snapshots/             weekly snapshots for historical tracking

/scraper/
  discover.py            discovery engine (Playwright, C&C + BaT)
  cc_scraper.py          C&C live listings (requests + BeautifulSoup)
  bat_scraper.py         BaT sold prices
  gumtree_scraper.py     Gumtree private listings
  run_all.py             orchestrator

/.github/workflows/
  scrape.yml             weekly cron via GitHub Actions
```

---

## Known gaps / next improvements

1. **Sold prices for pinned models**: `run_all.py` does not yet call `_fetch_cc_sold` / `_fetch_bat_sold`
   for the watchlist. Pinned models show "—" for sold prices until this is added.

2. **BaT returning no results**: The BaT API may require different query terms or have rate-limited
   the Playwright user agent. Test with curl before assuming no results.

3. **Sold price trend**: Currently only shows 6-month median. Add period-on-period comparison
   (6m vs prior 6m) to show direction of travel.

4. **DOM (days on market)**: Would require tracking individual listing IDs across scrape runs.
   A listing that re-appears after disappearing signals a failed sale.

5. **Price band analysis**: The median hides the shape. A bimodal distribution (lots of cheap
   project cars + a few pristine examples) needs a different display — show P25, median, P75.

6. **Alert emails**: Weekly digest of biggest movers, new hot signals, approaching 25yr windows.
