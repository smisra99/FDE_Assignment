# DECISIONS.md

## What I built

A Streamlit control tower reading only from `data/kestrel\\\_marts.db` (never the raw
ops DB at runtime). Covers all five of Divya's asks: service (fill rate, OTIF),
cold chain, money (real freight cost, returns), price position, and a worst-first
"Overview" front page. Built via four idempotent, reproducible pipeline scripts
(`build\\\_marts.py`, `build\\\_freight\\\_mart.py`, `build\\\_bazaarpulse\\\_mart.py`,
`build\\\_price\\\_gap\\\_mart.py`) run once at setup, not at app runtime.

## What I deliberately did not build

* **Ask-anything (NL query layer)**: stubbed, not implemented. Time went to
getting five real, validated metrics right over a sixth speculative one.
* **Regional manager scoped view**: brief said "make it work for them too" as a
secondary ask.
* **Full reconciliation on `routes`/`inventory\\\_snapshots`/`salespeople`/`promotions`**:
only a lightweight sanity pass (row counts, null checks) — not the deep,
query-by-query treatment given to outlets/orders/deliveries/returns/products,
due to time.
* **Ask-anything aside, no LLM is used anywhere else** — every number in the app
is a plain SQL view.

## Key assumptions, each backed by a query

* **Fill rate reported in eaches**, not cases — Rakesh's explicit override of Divya's
instruction, justified by modern trade penalizing on units short.
* **"Last complete month" = June 2026**, confirmed (not assumed) by checking daily
order volume through the 30th for any drop-off — none found, only a weekly
(Sunday) dip pattern. Same evidence makes **Q1 FY27 (Apr–Jun 2026) a complete
quarter**, used on the front page per Rakesh's request.
* **Test outlets** = `outlet\\\_code LIKE 'TST%'` (3 of 724 rows) — found via a GST-number
collision, not guessed from the dictionary, which didn't mention this pattern at all.
* **City name collisions** (Bangalore/Bengaluru, Delhi/New Delhi) normalized to
Bengaluru/Delhi — a judgment call, not the only valid choice.
* **Kestrel is a multi-brand distributor** (Kestrel, Bluepeak, Hillfare, Amrit,
Coastline, Marwar per `products.brand`), not a single-brand manufacturer — this
reframed "competitor" price matching from "titles containing Kestrel" to
matching against the full brand list.
* **Return quantity sign** (\~6.5% negative, uniform, no pattern) treated as noise
via `.abs()` rather than a meaningful signal.

## Findings surfaced, not hidden

* **OTIF is \~62% (60-min grace) / \~30% (strict), nearly uniform across all 5
regions** — a systemic, network-wide delay problem (avg delay 132 min), not a
regional one.
* **All 140/140 routes** exceed the brief's literal ">2hr late on >10% of
deliveries" threshold — the question as posed doesn't discriminate in this
dataset; the dashboard ranks by severity instead of pass/fail.
* **Cold chain excursion flag is suspect**: chilled deliveries show 3.13/100
excursions vs. 2.92/100 for non-chilled — nearly identical, which shouldn't
happen if the flag is genuinely cold-chain-specific. Reported as asked, flagged
in the UI, not investigated further given time.
* **Real freight cost varies \~76% across warehouses** (₹18.66–₹32.89 per
delivered each) — this replaced the `fuel\\\_cost\\\_inr` proxy, which the data
dictionary itself flagged as unreliable (driver-entered, unreconciled).
* **Price-match quality**: 100% of scraped listings matched *a* candidate SKU
(brand+pack join is permissive by construction), but 3.8% (44/1,168) fail an
MRP-consistency check and are excluded from headline numbers rather than
silently trusted.

## What I'd do next with two more weeks

1. Wire the ask-anything layer, constrained to query only the existing metric
views (never raw SQL), with numbers shown alongside any generated answer.
2. Root-cause the cold-chain excursion flag anomaly instead of just flagging it.
3. Full reconciliation pass on `routes`, `inventory\\\_snapshots`, `salespeople`,
`promotions` — same rigor as the other seven tables.
4. Tighten SKU matching (raise the fuzzy threshold, add a manual review queue for
the 44 flagged mismatches) and extend price-gap coverage beyond Mumbai.
5. Role-scoped regional manager views; scheduled/cached refresh instead of
manual pipeline re-runs.

## What breaks first in production

* **The BazaarPulse scraper** — hand-written per-city parsers for four distinct,
undocumented markup formats. Any site redesign breaks it silently (wrong
prices, not errors) unless a price-sanity check is added.
* **The mock carrier API's rate limiting** (\~1-in-9 429s) makes a full pull
slow (3–4 min); a real carrier API at higher volume would need a smarter
backoff strategy or a scheduled background job, not a synchronous pull at app
setup.
* **No incremental refresh** — every pipeline script does a full rebuild from
scratch. Fine for 18 months of data now; won't scale indefinitely.

