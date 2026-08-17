# Kestrel Provisions Control Tower

A Streamlit dashboard answering Divya's brief: service (fill rate, OTIF),
cold chain, money (freight cost, returns), and price position — one screen,
worst performers first, no clicking required to find them.

See `DECISIONS.md` for what was built, what wasn't, and why. See


---

## Prerequisites

- Python 3.10+ (tested on 3.13)
- Everything below runs on one machine, no accounts or API keys needed —
  the "external" sources are a local static site and a local mock API,
  both included in this pack.

Install dependencies:
```powershell
pip install pandas streamlit requests beautifulsoup4 fastapi uvicorn
```

---

## Cold start — one command

Just to see the Dashboard Run:
```powershell
streamlit run app.py
```


To Build clean database and scrape data from Site and API then build Dashboard run this:
```powershell
python run_pipeline.py
```

This runs the entire pipeline in order (starts the mock API and scrape
target in the background as needed, builds all cleaned data and views,
fetches real freight cost, scrapes competitor prices, matches SKUs, stops
the background servers once their data is pulled, then launches the
dashboard). Takes a few minutes total — most of that is the freight API's
built-in rate limiting (see `DECISIONS.md`).

Useful flags:
```powershell
python run_pipeline.py --skip-external   # core dashboard only, skips freight/scraper (fast)
python run_pipeline.py --data-only       # build everything, don't launch Streamlit
python run_pipeline.py --no-cache        # force a fresh freight pull instead of using the cache
```

If this fails partway, it stops immediately and tells you which step failed
— fix that step using the manual instructions below, then re-run
`run_pipeline.py` (it's safe to re-run; every step is idempotent, and
already-running servers are detected and reused rather than restarted).

---

## Cold start — manual, step by step

Only needed if you want to run/inspect each stage individually, or if
`run_pipeline.py` reports a failure you're debugging. Run these **in
order**, from the pack root. Each step writes into `data/kestrel_marts.db`;
later steps depend on earlier ones having run.

### 1. Build the core cleaned data + metric views
```powershell
python build_marts.py
```
Reads `data/kestrel_ops.db`, applies all cleaning rules (see
`DECISIONS.md`), and creates `data/kestrel_marts.db` with the cleaned
tables and the fill-rate/OTIF/route-lateness/cold-chain/returns views.
Takes a few seconds. Should finish with **zero warnings** — if you see
any, something about the source data has changed since this was built.

### 2. Fetch real freight cost (carrier API)
Open a **new terminal window** and leave it running:
```powershell
python partner_api/server.py
```
Then, from your main terminal:
```powershell
python build_freight_mart.py
```
Pulls ~41,500 invoices from the mock carrier API (takes 3–4 minutes due to
built-in rate limiting on the mock service — this is expected, see
`DECISIONS.md`). Caches the raw pull to `data/.freight_invoices_cache.pkl`,
so re-running this script after the first successful run is near-instant
unless you pass `--no-cache`.

### 3. Scrape competitor prices (BazaarPulse)
Open a **second new terminal window** and leave it running:
```powershell
cd bazaarpulse_site
python -m http.server 8080
```
Then, from your main terminal (back in the pack root):
```powershell
python build_bazaarpulse_mart.py
```
Scrapes all 4 cities (~1,168 listings), respecting the site's robots.txt
crawl-delay — takes roughly a minute.

### 4. Match competitor listings to Kestrel's own SKUs
```powershell
python build_price_gap_mart.py
```
Matches scraped listings to `products` (brand + pack size, fuzzy tiebreak),
builds the price-gap and top-20-by-value views. A few seconds.

### 5. Launch the dashboard
```powershell
streamlit run app.py
```
Opens automatically in your browser (usually `localhost:8501`). The
`partner_api` and `bazaarpulse_site` servers from steps 2–3 are **not**
needed once their data has been pulled — you can close those terminal
windows after steps 2 and 3 finish, before running the app.

---

## Fast path — data already built

If `data/kestrel_marts.db` already exists with all the tables/views
populated, skip straight to:
```powershell
streamlit run app.py
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `sqlite3 : term not recognized` | `sqlite3.exe` isn't on PATH — see `DECISIONS.md`/chat history for the Windows PATH fix, or just use the Python scripts, which don't need the CLI. |
| `could not reach http://localhost:8088` | `partner_api/server.py` isn't running — start it in its own terminal and leave it open. |
| `could not reach http://localhost:8080` | Same, for `bazaarpulse_site` — `python -m http.server 8080` from inside that folder. |
| App shows a fallback/proxy warning on the Money page | The real freight/price-gap views weren't built yet — run steps 2–4 above. |
| `build_marts.py` reports any WARNING | Stop and investigate before proceeding — every downstream step assumes a clean run here. |

---

## Project structure

```
01_Assignment_Brief.md / .docx   — original brief (unmodified)
02_Data_Dictionary.md            — original data dictionary (unmodified)
03_External_Sources.md           — original external sources doc (unmodified)
DECISIONS.md                     — judgment calls made, one page
README.md                        — this file

build_marts.py                   — step 1: core cleaning + views
build_freight_mart.py             — step 2: real freight cost from carrier API
build_bazaarpulse_mart.py         — step 3: competitor price scraper
build_price_gap_mart.py           — step 4: SKU matching + price gap
run_pipeline.py                   — runs all of the above in order, one command
app.py                            — the Streamlit dashboard

data/kestrel_ops.db              — raw source database (input, untouched)
data/kestrel_marts.db            — cleaned data + all views (generated)
bazaarpulse_site/                — local static scrape target
partner_api/                     — local mock carrier API
```
