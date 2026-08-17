"""
build_freight_mart.py
=======================
Fetches freight invoices from the Kestrel Logistics Partner API (mock service)
and materializes them into data/kestrel_marts.db as fact_freight_invoices.

Prerequisites:
    1. Start the mock API in a separate terminal, from the assignment pack root:
         pip install fastapi uvicorn
         python partner_api/server.py
       Leave it running at http://localhost:8088.
    2. Run this script:
         python build_freight_mart.py
         
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import requests

API_BASE = "http://localhost:8088"
API_KEY = "kp_live_7f3a9c21"
HEADERS = {"X-API-Key": API_KEY}

MARTS_DB_PATH = Path("data/kestrel_marts.db")

# Bound the walk to the operational data's date range (18 months to 30 Jun 2026)
# rather than pulling the entire invoice history unbounded.
DATE_FROM = "2025-01-01"
DATE_TO = "2026-06-30"

MAX_RETRIES = 6


def log(msg: str) -> None:
    print(f"[build_freight_mart] {msg}")


def request_with_retry(url: str, params: dict = None) -> dict:
    """GET with retry/backoff for the documented 429 (Retry-After) and 503
    behavior. Raises on anything else or after exhausting retries."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt
            log(f"  network error ({e}), retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
            log(f"  429 rate limited, waiting {retry_after}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(retry_after)
            continue

        if resp.status_code == 503:
            wait = 2 ** attempt
            log(f"  503 service unavailable, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        # Anything else (401, 404, etc.) -- don't retry, surface it clearly.
        resp.raise_for_status()

    raise RuntimeError(f"Exhausted {MAX_RETRIES} retries fetching {url}")


def check_health() -> None:
    log(f"Checking API health at {API_BASE}/v1/health ...")
    try:
        resp = requests.get(f"{API_BASE}/v1/health", timeout=10)
        resp.raise_for_status()
        log(f"  OK: {resp.json()}")
    except requests.RequestException as e:
        log(f"ERROR: could not reach the mock API at {API_BASE}. Is it running?")
        log(f"  Start it with: python partner_api/server.py")
        log(f"  ({e})")
        sys.exit(1)


def fetch_sample_invoice() -> dict:
    """Fetch just the first page, unfiltered, to inspect the real field
    shape before deciding the join key to deliveries/orders."""
    log("Fetching one sample page of freight_invoices to inspect field shape...")
    data = request_with_retry(f"{API_BASE}/v1/freight_invoices")
    invoices = data.get("invoices") or data.get("results") or data.get("data") or []
    if not invoices:
        log(f"  WARNING: response shape unexpected, got keys: {list(data.keys())}")
        log(f"  Full response: {data}")
        return {}
    sample = invoices[0]
    log(f"  Sample invoice fields: {list(sample.keys())}")
    log(f"  Sample invoice: {sample}")
    return sample


def fetch_all_invoices() -> pd.DataFrame:
    log(f"Fetching freight_invoices from {DATE_FROM} to {DATE_TO} (cursor pagination)...")
    all_invoices = []
    cursor = None
    page = 0
    while True:
        page += 1
        params = {"from": DATE_FROM, "to": DATE_TO}
        if cursor:
            params["cursor"] = cursor
        data = request_with_retry(f"{API_BASE}/v1/freight_invoices", params=params)

        invoices = data.get("invoices") or data.get("results") or data.get("data") or []
        all_invoices.extend(invoices)
        cursor = data.get("next_cursor")

        if page % 10 == 0 or cursor is None:
            log(f"  page {page}: {len(all_invoices):,} invoices so far"
                f"{' (done)' if cursor is None else ''}")

        if cursor is None:
            break

    df = pd.DataFrame(all_invoices)
    log(f"Fetched {len(df):,} total invoices.")
    return df


def fetch_carriers() -> pd.DataFrame:
    log("Fetching carriers...")
    data = request_with_retry(f"{API_BASE}/v1/carriers")

    # Response shape wasn't confirmed by a sample call before this -- same
    # class of mistake as guessing product_price_history's columns earlier.
    # Try the common shapes; if none work, print the raw response so the
    # real shape is visible instead of silently returning 0 rows.
    if isinstance(data, list):
        carriers_list = data
    elif isinstance(data, dict):
        carriers_list = (data.get("carriers") or data.get("results")
                          or data.get("data") or [])
        if not carriers_list:
            log(f"  WARNING: couldn't find carriers in response. Raw keys: {list(data.keys())}")
            log(f"  Raw response: {data}")
    else:
        carriers_list = []
        log(f"  WARNING: unexpected response type {type(data)}: {data}")

    df = pd.DataFrame(carriers_list)
    log(f"  {len(df)} carriers")
    return df


def normalize_invoices(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    # amount and detention_charge are both in paise per 03_External_Sources.md
    # ("the amount field is in paise") -- detention_charge is presumed same
    # unit since it's a same-shape currency figure on the same record, but
    # this is an assumption, not confirmed by the doc. Flag in DECISIONS.md.
    df["amount_inr"] = df["amount"] / 100.0
    if "detention_charge" in df.columns:
        df["detention_charge_inr"] = df["detention_charge"] / 100.0

    # Only created_at_utc carries an actual time component -- convert that
    # one to IST. invoice_date/service_date are plain dates (no time), left
    # as-is; running them through a tz conversion would be meaningless.
    df["created_at_ist"] = (
        pd.to_datetime(df["created_at_utc"], utc=True, errors="coerce")
        .dt.tz_convert("Asia/Kolkata")
        .dt.strftime("%Y-%m-%d %H:%M:%S")
    )

    return df


def _stringify_list_columns(df: pd.DataFrame) -> pd.DataFrame:
    """SQLite's plain driver can't bind Python list objects either (same
    class of issue as the Timestamp binding problem in build_marts.py).
    carriers.regions comes back as a list (e.g. ['West', 'Central']) --
    join it into a comma-separated string so it's still readable/filterable
    in SQL, just not as a native array."""
    df = df.copy()
    for col in df.columns:
        sample = df[col].dropna()
        if len(sample) and isinstance(sample.iloc[0], list):
            df[col] = df[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
    return df


def write_to_marts(df: pd.DataFrame, carriers: pd.DataFrame) -> None:
    if not MARTS_DB_PATH.exists():
        log(f"ERROR: {MARTS_DB_PATH} not found. Run build_marts.py first.")
        sys.exit(1)
    conn = sqlite3.connect(MARTS_DB_PATH)
    df = _stringify_list_columns(df)
    df.to_sql("fact_freight_invoices", conn, if_exists="replace", index=False)
    log(f"  wrote fact_freight_invoices: {len(df):,} rows")
    if len(carriers):
        carriers = _stringify_list_columns(carriers)
        carriers.to_sql("dim_carriers", conn, if_exists="replace", index=False)
        log(f"  wrote dim_carriers: {len(carriers):,} rows")
    conn.close()


def create_freight_views(conn: sqlite3.Connection) -> None:
    
    log("Creating real freight cost views...")
    cur = conn.cursor()
    cur.execute("DROP VIEW IF EXISTS freight_cost_real_by_warehouse")
    cur.execute("""
        CREATE VIEW freight_cost_real_by_warehouse AS
        SELECT
            wh.warehouse_id, wh.warehouse_code, wh.warehouse_name,
            inv.total_freight_cost_inr,
            inv.total_detention_inr,
            inv.invoice_count,
            dlv.total_delivered_eaches,
            inv.total_freight_cost_inr * 1.0 / NULLIF(dlv.total_delivered_eaches, 0)
                AS freight_cost_per_delivered_each_inr
        FROM warehouses wh
        LEFT JOIN (
            SELECT w.warehouse_id,
                   SUM(fi.amount_inr) AS total_freight_cost_inr,
                   SUM(fi.detention_charge_inr) AS total_detention_inr,
                   COUNT(*) AS invoice_count
            FROM fact_freight_invoices fi
            JOIN warehouses w ON w.warehouse_code = fi.warehouse_code
            GROUP BY w.warehouse_id
        ) inv ON inv.warehouse_id = wh.warehouse_id
        LEFT JOIN (
            SELECT d.warehouse_id, SUM(fol.delivered_eaches) AS total_delivered_eaches
            FROM fact_deliveries_clean d
            JOIN fact_order_lines_clean fol ON fol.order_id = d.order_id
            GROUP BY d.warehouse_id
        ) dlv ON dlv.warehouse_id = wh.warehouse_id
    """)
    conn.commit()
    log("  created view: freight_cost_real_by_warehouse")

    # Quick sanity check, printed immediately -- don't wait for the app to
    # surface a broken join.
    check = pd.read_sql("""
        SELECT warehouse_code, total_freight_cost_inr, total_delivered_eaches,
               freight_cost_per_delivered_each_inr
        FROM freight_cost_real_by_warehouse
        ORDER BY warehouse_code
    """, conn)
    n_null = check["freight_cost_per_delivered_each_inr"].isna().sum()
    if n_null:
        log(f"  WARNING: {n_null}/{len(check)} warehouses have a null freight-per-each "
            f"value (no invoices matched or no deliveries matched) -- check the "
            f"warehouse_code join and the {check['warehouse_code'].tolist()} coverage.")
    log(f"  Sample:\n{check.to_string(index=False)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-only", action="store_true",
                         help="Fetch one sample invoice and print its shape, then exit. "
                              "Run this FIRST to confirm field names before a full pull.")
    parser.add_argument("--carriers-only", action="store_true",
                         help="Fetch just /v1/carriers and print the raw response, then exit. "
                              "Use this to debug the carriers endpoint without re-pulling "
                              "all 41,500 invoices.")
    args = parser.parse_args()

    check_health()

    if args.carriers_only:
        carriers = fetch_carriers()
        log(f"Carriers result:\n{carriers}")
        return

    if args.sample_only:
        fetch_sample_invoice()
        log("Sample-only run complete. Review the field names above, then adjust "
            "normalize_invoices() if needed, then re-run without --sample-only.")
        return

    carriers = fetch_carriers()
    invoices = fetch_all_invoices()
    invoices = normalize_invoices(invoices)
    write_to_marts(invoices, carriers)

    conn = sqlite3.connect(MARTS_DB_PATH)
    create_freight_views(conn)
    conn.close()

    log("Done. freight_cost_real_by_warehouse is ready -- update app.py's Money "
        "page to use it instead of the fuel_cost_inr proxy.")


if __name__ == "__main__":
    main()
