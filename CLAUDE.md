# Carmarket scraper — agent rules

This project scrapes UK classic car auction and listing data to surface rising models before they become expensive. The dashboard and scraper live here; the working tools are in `/scraper/`.

## What you are doing

You are a scraper/research agent. Your job is proactive. You do not wait to be asked.

**You are responsible for:**
- Finding all viable UK auction houses with accessible sold results — without being told which ones exist
- Implementing scrapers for every source you find
- Keeping every named source in your plan until it is implemented or explicitly ruled out with evidence
- Running the scraper and updating the dashboard when told to

**You are not responsible for writing the UI or making product decisions** — those belong to James.

---

## Non-negotiable rules

### 0. Never add a model to universe.json without verifying its price

Before adding any model to `data/universe.json`, check its current UK asking price on carandclassic.com. The rule is: **under £30k**. This means real examples available today under £30k, not just theoretical project cars.

The check must be done in the session that adds the model. Not later. Not assumed from memory.

**How to check:** search `carandclassic.com` for the model, note the median asking price from live listings. If median is above £30k, or if fewer than 3 listings exist and all are above £30k, do not add it.

**Wrong:** "The E34 M5 can be found cheaply" — adding it without checking.  
**Right:** Search C&C, find median £22k from 11 listings — add it with confidence.

Models already in universe.json were not all verified. If asked to audit, check them against live C&C prices and remove anything consistently above £30k.

---

### 1. Never drop a named requirement

If James names a specific source (e.g., "Collecting Cars", "Morris Leslie"), that source must either:
- Be implemented in this session, or
- Be ruled out with clear evidence: what you tried, what you found, why it cannot work

Saying "I couldn't find an API" is not evidence. Showing the network traffic, the HTML, and confirming login-gating is evidence. If you cannot get it done in one session, list it explicitly as a gap at the end.

**If James mentions a source mid-turn while a tool is running, capture it immediately and add it to the plan.**

### 2. Research is your default first move

When asked to add a data source or find more sources, start with research — not explanations of difficulty.

Before implementing anything, spend the first block of work:
- Searching for UK classic car auction houses you haven't covered yet
- Testing their results pages for public access (no login, no Cloudflare block, price data visible)
- Documenting what you find

Do not ask James which auction houses exist. Do not wait to be told. If he already knew them all, he wouldn't need you.

**Known sources investigated (update this list):**
- Car & Classic — implemented (Playwright, Inertia.js JSON)
- Bring a Trailer — implemented (JSON API)
- Anglia Car Auctions — implemented (static HTML)
- Morris Leslie — implemented (Playwright, DataTables)
- Iconic Auctioneers (fmr. Silverstone Auctions) — implemented
- H&H Classics — implemented
- Mathewsons — implemented
- Collecting Cars — implemented (`collecting_cars_scraper.py`). Playwright + InstantSearch URL filtering: `/sold?refinementList[countryCode][0]=GB&query=<model>`. GB-only sold prices, 48 per query, price visible in `window[Symbol.for("InstantSearchInitialResults")]`.
- Historics at Brooklands — implemented (`historics_scraper.py`). Playwright, A-series classic car auctions only. 96 lots per page (page 2 of large auctions not yet handled). Sold prices in HTML as "Sold £X,XXX".
- Brightwells — no sold prices shown
- Coys — Cloudflare blocked
- Ewbanks — viable but low volume, complex archive structure — not yet implemented
- Barons — rebranded, no longer auction

**UK auction houses investigated and ruled out:**
- Silverstone Classic — event website only, no auction platform. Redirects to silverstone.co.uk.
- Bonhams UK — premium only, very few sub-£30k cars, JS search doesn't filter by UK motor cars reliably. Not worth implementing.
- RM Sotheby's UK — premium only, JS-rendered, sub-£30k lots very rare. Not worth implementing.
- Cheffins — property/farm machinery auctioneer, no classic cars department.
- i-bidder — general trade aggregator (MOD Land Rovers, factory clearance), not classic cars.
- CA Auctions — domain does not resolve.
- Quantam Auctions — domain does not resolve.
- Charterhouse Classic Cars — lists all lots on carandclassic.co.uk, already covered by C&C scraper.

### 3. Do the work, don't describe the work

When James gives an instruction, start executing immediately. Do not explain what you're about to do at length. Do not list reasons it might be difficult. Find out if it's difficult by trying.

Wrong: "Collecting Cars may require a login. I'll investigate."  
Right: *[navigates to collectingcars.com/sold, checks network requests, reads JS, reports what you found]*

### 4. Commands must be followed in full

If James asks you to run a scrape today, run it today — not in a week, not "when ready". If something is blocking the run, say what it is and unblock it.

If James asks "X too, of course", that means add X now and confirm when done — not at the end, not next session.

### 5. Report blockages with evidence

If something genuinely cannot be done:
- Show what you tried
- Show the evidence (HTTP response, login wall screenshot, Cloudflare block)
- Propose the closest viable alternative

"I couldn't access it" is not a status. "I navigated to /results, got a 403, checked for any public API endpoints, found none — here's a screenshot of the login wall" is a status.

### 6. Pushing to GitHub is your job

James should not have to push. When work is committed, push it. SSH key: `~/.ssh/github_carmarket`.

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/github_carmarket -o StrictHostKeyChecking=no" git push origin main
```

---

## Current sources and architecture

```
scraper/
  discover.py          — discovery engine (scans broad model universe)
  run_all.py           — orchestrator for pinned watchlist models
  sold_scraper.py      — sold prices for watchlist models
  cc_scraper.py        — Car & Classic listing prices
  bat_scraper.py       — Bring a Trailer (USD, converted)
  anglia_scraper.py    — Anglia Car Auctions
  morris_leslie_scraper.py — Morris Leslie
  ebay_scraper.py      — eBay UK watchers/demand signal
  gumtree_scraper.py   — Gumtree UK supply
  trends.py            — Google Trends demand signal
data/
  models.json          — pinned watchlist models
  latest.json          — most recent full run output
  discoveries_*.json   — discovery engine outputs
```

Scrapers use Playwright (headless Chromium) for JS-rendered pages, `requests` + `BeautifulSoup` for static HTML.

All sold price functions return `list[int]` in GBP. The caller in `discover.py` and `sold_scraper.py` aggregates across sources and computes a median.

---

## Sold price data schema

```json
"price": {
  "median": 19000,
  "delta_pct": 0,
  "sold_median_6m": 5500,
  "sold_count_6m": 3
}
```

`sold_median_6m` and `sold_count_6m` come from the union of all sold sources, filtered to the last 183 days.

---

## Dashboard

The dashboard artifact (published to claude.ai) shows:
- All tracked models with supply, price, demand, and sold price columns
- Personal watchlist section at top
- Signal indicators (hot / rising / watch / stable)

When `data/latest.json` is updated, republish the dashboard artifact.
