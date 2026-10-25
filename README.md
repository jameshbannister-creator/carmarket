# Classic Market

UK classic car market intelligence. Tracks real signals — not magazine lists.

## What it tracks

| Signal | Source | Why it matters |
|---|---|---|
| Listing velocity | Car and Classic | Supply tightening = price pressure incoming |
| eBay watcher counts | eBay UK | Leading indicator: buyers watching before they buy |
| Auction results | Bring a Trailer | US market price discovery, export arbitrage |
| Search trends | Google Trends | Soft demand signal, early momentum |
| 25-year pipeline | Calculated | US import eligibility = demand catalyst |

## Stack

- **Scrapers:** Python + Playwright (for JS-heavy sites), runs on GitHub Actions
- **Database:** JSON files committed to this repo (zero infrastructure)
- **Dashboard:** Static HTML served via GitHub Pages (zero cost)
- **Alerts:** GitHub Actions sends email on spike events

**Total cost: £0/month.**

## Setup

### 1. Fork this repo on GitHub

### 2. Enable GitHub Pages
Settings → Pages → Source: `main` branch, `/dashboard` folder.

Your dashboard will be at `https://yourusername.github.io/carmarket/`.

### 3. Add eBay session cookie (optional, for watcher counts)
Settings → Secrets → New: `EBAY_SESSION_COOKIE`

Sign into eBay, copy your session cookie from browser DevTools → Application → Cookies.
Without this, eBay watcher data is skipped (everything else still runs).

### 4. Run manually to test
Actions tab → "Weekly market scrape" → Run workflow.

After it runs, `data/latest.json` is updated and committed. Dashboard pulls from that file.

## Adding a model to track

Edit `data/models.json` and add an entry:

```json
{
  "id": "your-model-id",
  "name": "Make Model Variant",
  "make": "Make",
  "years": [1990, 2000],
  "cc_search": "search term for carandclassic.com",
  "ebay_search": "eBay search string",
  "bat_search": "bring a trailer search term",
  "us_rare": true,
  "notes": "Any context"
}
```

## Local development

```bash
cd scraper
pip install -r requirements.txt
playwright install chromium
python run_all.py
```

Opens `dashboard/index.html` in your browser to see results.

## Data freshness

Scrapers run every Monday 08:00 UTC via GitHub Actions. Historical snapshots in `data/snapshots/` accumulate over time, which is what powers the trend and velocity calculations.
