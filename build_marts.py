"""
build_marts.py
================
Kestrel Provisions control tower — data cleaning + mart build script.

Reads raw data/kestrel_ops.db, applies cleaning rules established during
reconciliation (see DECISIONS.md for evidence behind each rule), and writes
a cleaned dimensional layer + fact tables to data/kestrel_marts.db.

Run this once during setup, before starting the Streamlit app:
    python build_marts.py

Idempotent — safe to re-run; it drops and recreates every table it writes.

--------------------------------------------------------------------------
RULES APPLIED (each backed by a query run against the raw data — see
DECISIONS.md for the exact evidence and row counts):

1. Outlets
   - Filter is_deleted = 0 (dictionary: app filters this, raw SQL does not)
   - is_test = outlet_code LIKE 'TST%'   (3 rows, confirmed exhaustive:
     the only two outlet_code prefixes in the table are 'OUT' and 'TST')
   - No outlet_code duplicates exist (verified) — outlet_id/outlet_code
     are safe unique keys, no dedupe needed
   - name+city collisions (~150 groups) are NOT duplicates — confirmed by
     inspecting GST/phone/lat-long, which differ across every collision.
     Templated names from the data generator, not real-world duplication.
   - City normalization: Bangalore -> Bengaluru, New Delhi -> Delhi
     (judgement call, documented in DECISIONS.md)

2. order_lines — UOM to eaches
   - qty_uom in {CASE, EACH} only, no third value, no nulls
   - case_pack_at_order has ZERO missing/zero values for CASE rows
     -> conversion is 100% reliable, no fallback needed
   - eaches = ordered_qty * case_pack_at_order  (CASE)
   - eaches = ordered_qty                        (EACH)

3. orders
   - Header value vs sum(line_value_inr): zero mismatches >= INR 1 found
     in current snapshot -> order_value_gross_inr trusted as-is (KP-2301
     appears already resolved in this data)
   - created_at parsed per source_system (three distinct formats):
       ERP_WEB      -> %d/%m/%Y %H:%M   (DD/MM assumed; Indian business)
       PARTNER_API  -> ISO8601 (Z suffix)
       SFA_MOBILE   -> %Y-%m-%d %H:%M:%S

4. deliveries
   - actual_arrival parsed per telematics_vendor:
       TELEMATICS_A -> %Y-%m-%d %H:%M:%S
       TELEMATICS_B -> %d-%b-%Y %I:%M %p

5. returns_credit_notes
   - return_qty has ~6.5% negative values, uniform across every reason
     code and disposition, no correlated pattern found (KP-2402) ->
     treated as sign noise, normalized via abs()

6. product_price_history — as-of-date price join
   - NOT YET VERIFIED for coverage gaps. Script includes the join;
     the coverage check (row count before/after) prints a warning if
     any order_lines fail to match a price window. Verify this output
     before trusting price-gap / MRP-based metrics.
--------------------------------------------------------------------------
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd

RAW_DB_PATH = Path("data/kestrel_ops.db")
MARTS_DB_PATH = Path("data/kestrel_marts.db")

CITY_MAP = {
    "Bangalore": "Bengaluru",
    "New Delhi": "Delhi",
}


def log(msg: str) -> None:
    print(f"[build_marts] {msg}")


def require_raw_db() -> sqlite3.Connection:
    if not RAW_DB_PATH.exists():
        log(f"ERROR: raw database not found at {RAW_DB_PATH.resolve()}")
        log("Run this script from the folder that contains the 'data' directory.")
        sys.exit(1)
    return sqlite3.connect(RAW_DB_PATH)


# --------------------------------------------------------------------------
# 1. Outlets
# --------------------------------------------------------------------------
def build_dim_outlets(raw: sqlite3.Connection) -> pd.DataFrame:
    log("Building dim_outlets...")
    outlets = pd.read_sql("SELECT * FROM outlets WHERE is_deleted = 0", raw)
    before = len(outlets)

    outlets["city_clean"] = outlets["city"].str.strip().replace(CITY_MAP)
    outlets["is_test"] = outlets["outlet_code"].str.startswith("TST")

    n_test = int(outlets["is_test"].sum())
    log(f"  {before} active outlets loaded; {n_test} flagged as test outlets (TST% prefix)")

    dupe_codes = outlets["outlet_code"].duplicated().sum()
    if dupe_codes:
        log(f"  WARNING: {dupe_codes} duplicate outlet_code values found — "
            f"this contradicts the reconciliation finding, investigate before trusting outlet-level metrics.")
    else:
        log("  Confirmed: no duplicate outlet_code values.")

    return outlets


# --------------------------------------------------------------------------
# 2. order_lines -> canonical eaches
# --------------------------------------------------------------------------
def build_fact_order_lines(raw: sqlite3.Connection) -> pd.DataFrame:
    log("Building fact_order_lines_clean (UOM -> eaches)...")
    ol = pd.read_sql("SELECT * FROM order_lines", raw)

    is_case = ol["qty_uom"] == "CASE"
    is_each = ol["qty_uom"] == "EACH"
    other = ~(is_case | is_each)
    if other.any():
        log(f"  WARNING: {other.sum()} rows have qty_uom outside {{CASE, EACH}} — "
            f"these will have null eaches, investigate.")

    ol["ordered_eaches"] = pd.NA
    ol["delivered_eaches"] = pd.NA
    ol["allocated_eaches"] = pd.NA

    ol.loc[is_each, "ordered_eaches"] = ol.loc[is_each, "ordered_qty"]
    ol.loc[is_each, "delivered_eaches"] = ol.loc[is_each, "delivered_qty"]
    ol.loc[is_each, "allocated_eaches"] = ol.loc[is_each, "allocated_qty"]

    ol.loc[is_case, "ordered_eaches"] = ol.loc[is_case, "ordered_qty"] * ol.loc[is_case, "case_pack_at_order"]
    ol.loc[is_case, "delivered_eaches"] = ol.loc[is_case, "delivered_qty"] * ol.loc[is_case, "case_pack_at_order"]
    ol.loc[is_case, "allocated_eaches"] = ol.loc[is_case, "allocated_qty"] * ol.loc[is_case, "case_pack_at_order"]

    coverage = ol["ordered_eaches"].notna().mean()
    log(f"  UOM conversion coverage: {coverage:.1%}")
    if coverage < 0.999:
        log("  WARNING: coverage below 99.9% — this contradicts the reconciliation finding "
            "of 100% case_pack_at_order coverage. Investigate before trusting fill-rate numbers.")

    return ol


# --------------------------------------------------------------------------
# 3. orders -> parsed timestamps
# --------------------------------------------------------------------------
def _parse_created_at(row: pd.Series):
    src = row["source_system"]
    raw_val = row["created_at"]
    try:
        if src == "ERP_WEB":
            return pd.to_datetime(raw_val, format="%d/%m/%Y %H:%M")
        elif src == "PARTNER_API":
            # This value ends in 'Z' (e.g. 2025-12-25T03:45:00Z) -> tz-aware.
            # Strip the timezone so this column stays uniformly tz-naive;
            # otherwise mixing tz-aware and tz-naive Timestamps in one
            # column breaks later bulk pd.to_datetime()/to_sql() calls.
            ts = pd.to_datetime(raw_val)
            if ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            return ts
        elif src == "SFA_MOBILE":
            return pd.to_datetime(raw_val, format="%Y-%m-%d %H:%M:%S")
        else:
            return pd.to_datetime(raw_val, errors="coerce")
    except (ValueError, TypeError):
        return pd.NaT


def build_fact_orders(raw: sqlite3.Connection) -> pd.DataFrame:
    log("Building fact_orders_clean (timestamp parsing)...")
    orders = pd.read_sql("SELECT * FROM orders", raw)
    # NOTE: do NOT re-wrap this in an outer pd.to_datetime(..., errors="coerce").
    # The per-row parse already returns proper Timestamp/NaT values; wrapping
    # a mixed tz-aware/tz-naive object column in a second bulk pd.to_datetime()
    # call fails at the array level and silently coerces almost everything to
    # NaT. _stringify_datetimes() (in write_marts) already knows how to handle
    # this object-dtype-of-Timestamps column correctly at write time.
    orders["created_at_parsed"] = orders.apply(_parse_created_at, axis=1)

    bad = orders["created_at_parsed"].isna().sum()
    if bad:
        log(f"  WARNING: {bad} orders failed created_at parsing — check source_system values "
            f"for any not in {{ERP_WEB, PARTNER_API, SFA_MOBILE}}.")
    else:
        log(f"  All {len(orders):,} orders parsed successfully.")

    orders["order_date_parsed"] = pd.to_datetime(orders["order_date"], errors="coerce")
    return orders


# --------------------------------------------------------------------------
# 4. deliveries -> parsed timestamps
# --------------------------------------------------------------------------
def _parse_arrival(row: pd.Series):
    vendor = row["telematics_vendor"]
    raw_val = row["actual_arrival"]
    if pd.isna(raw_val):
        return pd.NaT
    try:
        if vendor == "TELEMATICS_A":
            return pd.to_datetime(raw_val, format="%Y-%m-%d %H:%M:%S")
        elif vendor == "TELEMATICS_B":
            return pd.to_datetime(raw_val, format="%d-%b-%Y %I:%M %p")
        else:
            return pd.to_datetime(raw_val, errors="coerce")
    except (ValueError, TypeError):
        return pd.NaT


def build_fact_deliveries(raw: sqlite3.Connection) -> pd.DataFrame:
    log("Building fact_deliveries_clean (timestamp parsing)...")
    deliveries = pd.read_sql("SELECT * FROM deliveries", raw)
    # See note in build_fact_orders: don't double-wrap in an outer
    # pd.to_datetime(..., errors="coerce") — apply() already returns
    # proper Timestamp/NaT values.
    deliveries["actual_arrival_parsed"] = deliveries.apply(_parse_arrival, axis=1)
    deliveries["planned_arrival_parsed"] = pd.to_datetime(deliveries["planned_arrival"], errors="coerce")

    bad = deliveries.loc[deliveries["actual_arrival"].notna(), "actual_arrival_parsed"].isna().sum()
    if bad:
        log(f"  WARNING: {bad} deliveries with a non-null actual_arrival failed parsing — "
            f"check telematics_vendor values for anything outside {{TELEMATICS_A, TELEMATICS_B}}.")
    else:
        log(f"  All non-null actual_arrival values parsed successfully.")

    return deliveries


# --------------------------------------------------------------------------
# 5. returns_credit_notes -> sign normalization
# --------------------------------------------------------------------------
def build_fact_returns(raw: sqlite3.Connection) -> pd.DataFrame:
    log("Building fact_returns_clean (sign normalization)...")
    returns = pd.read_sql("SELECT * FROM returns_credit_notes", raw)

    n_negative = (returns["return_qty"] < 0).sum()
    pct = n_negative / len(returns) * 100 if len(returns) else 0
    log(f"  {n_negative} rows ({pct:.1f}%) had negative return_qty — normalizing via abs()")

    returns["return_qty_abs"] = returns["return_qty"].abs()
    return returns


# --------------------------------------------------------------------------
# 6. product_price_history -> as-of-date lookup (coverage check only, for now)
# --------------------------------------------------------------------------
def check_price_history_coverage(raw: sqlite3.Connection) -> None:
    log("Checking product_price_history as-of-date coverage against order_lines...")
    query = """
        SELECT COUNT(*) AS total_lines,
               SUM(CASE WHEN pph.price_history_id IS NULL THEN 1 ELSE 0 END) AS unmatched_lines
        FROM order_lines ol
        JOIN orders o ON o.order_id = ol.order_id
        LEFT JOIN product_price_history pph
          ON pph.product_id = ol.product_id
         AND o.order_date >= pph.effective_from
         AND (pph.effective_to IS NULL OR o.order_date <= pph.effective_to)
    """
    try:
        result = pd.read_sql(query, raw)
        total = int(result["total_lines"].iloc[0])
        unmatched = int(result["unmatched_lines"].iloc[0])
        pct = (unmatched / total * 100) if total else 0
        log(f"  {unmatched}/{total} order_lines ({pct:.1f}%) have no matching price window.")
        if pct > 1:
            log("  WARNING: meaningful coverage gap in the as-of-date price join — "
                "investigate before trusting price-based metrics (e.g. duplicate/overlapping "
                "validity windows, or order_date falling outside all windows for some SKUs).")
    except Exception as e:
        log(f"  Could not run price-history coverage check: {e}")


def build_fact_order_lines_priced(raw: sqlite3.Connection) -> pd.DataFrame:
    """order_lines joined to the price that was in effect as-of the order date.
    Use this (not products.mrp_inr / list_price_inr, which are CURRENT state only)
    for any historical revenue, MRP, or price-gap analysis."""
    log("Building fact_order_lines_priced (as-of-date price join)...")
    query = """
        SELECT ol.order_line_id, ol.order_id, ol.product_id,
               o.order_date,
               pph.price_history_id, pph.mrp_inr AS mrp_as_of_order,
               pph.list_price_inr AS list_price_as_of_order,
               pph.effective_from, pph.effective_to
        FROM order_lines ol
        JOIN orders o ON o.order_id = ol.order_id
        LEFT JOIN product_price_history pph
          ON pph.product_id = ol.product_id
         AND o.order_date >= pph.effective_from
         AND (pph.effective_to IS NULL OR o.order_date <= pph.effective_to)
    """
    priced = pd.read_sql(query, raw)

    dupe_matches = priced["order_line_id"].duplicated().sum()
    if dupe_matches:
        log(f"  WARNING: {dupe_matches} order_lines matched MORE THAN ONE price window "
            f"(overlapping validity periods in product_price_history) — this inflates row "
            f"count and will double-count if not deduplicated. Investigate before use.")

    unmatched = priced["price_history_id"].isna().sum()
    if unmatched:
        pct = unmatched / len(priced) * 100
        log(f"  {unmatched} order_lines ({pct:.1f}%) have no matching price window — "
            f"these will have null mrp_as_of_order/list_price_as_of_order.")

    return priced


# --------------------------------------------------------------------------
# 7. Plain reference/dimension tables — copied as-is (no cleaning needed,
#    confirmed via reconciliation: routes/warehouses/products/regions all
#    passed their sanity checks with no issues found)
# --------------------------------------------------------------------------
def copy_reference_tables(raw: sqlite3.Connection) -> dict:
    log("Copying reference tables (regions, routes, warehouses, products, "
        "salespeople, promotions, order_lines raw)...")
    tables = {}
    for name in ["regions", "routes", "warehouses", "products",
                 "salespeople", "promotions", "order_lines"]:
        tables[name] = pd.read_sql(f"SELECT * FROM {name}", raw)
        log(f"  copied {name}: {len(tables[name]):,} rows")
    return tables


# --------------------------------------------------------------------------
# Metric views — built directly in the marts DB via raw SQL, after the
# python-cleaned tables above are written. Each is documented with the
# reconciliation finding that justifies its logic.
# --------------------------------------------------------------------------
VIEW_DEFINITIONS = {
    # Fill rate (eaches, per Rakesh's override of Divya's "cases" instruction —
    # see DECISIONS.md). Excludes CANCELLED orders and test outlets.
    "fill_rate_by_outlet": """
        SELECT
            ou.outlet_id, ou.outlet_code, ou.outlet_name, ou.city_clean AS city,
            ou.region_id,
            SUM(fol.ordered_eaches) AS total_ordered_eaches,
            SUM(fol.delivered_eaches) AS total_delivered_eaches,
            SUM(fol.delivered_eaches) * 1.0 / NULLIF(SUM(fol.ordered_eaches), 0) AS fill_rate_eaches
        FROM fact_order_lines_clean fol
        JOIN fact_orders_clean o ON o.order_id = fol.order_id
        JOIN dim_outlets ou ON ou.outlet_id = o.outlet_id
        WHERE o.order_status != 'CANCELLED' AND ou.is_test = 0
        GROUP BY ou.outlet_id, ou.outlet_code, ou.outlet_name, ou.city_clean, ou.region_id
    """,

    # OTIF by region. Two thresholds reported side by side (see DECISIONS.md):
    # strict (delay_minutes <= 0) and a 60-min grace version, since strict OTIF
    # came back at ~30% uniformly across regions -- confirmed as a genuine
    # network-wide delay problem (avg delay 132 min), not a data bug.
    "otif_by_region": """
        SELECT
            ou.region_id, r.region_name,
            COUNT(DISTINCT d.delivery_id) AS total_deliveries,
            SUM(CASE WHEN d.delay_minutes <= 60 AND d.delivery_status = 'DELIVERED'
                     THEN 1 ELSE 0 END) AS otif_count_60min_grace,
            SUM(CASE WHEN d.delay_minutes <= 60 AND d.delivery_status = 'DELIVERED'
                     THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(DISTINCT d.delivery_id), 0) AS otif_rate_60min_grace,
            SUM(CASE WHEN d.delay_minutes <= 0 AND d.delivery_status = 'DELIVERED'
                     THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(DISTINCT d.delivery_id), 0) AS otif_rate_strict
        FROM fact_deliveries_clean d
        JOIN fact_orders_clean o ON o.order_id = d.order_id
        JOIN dim_outlets ou ON ou.outlet_id = o.outlet_id
        JOIN regions r ON r.region_id = ou.region_id
        WHERE ou.is_test = 0
        GROUP BY ou.region_id, r.region_name
    """,

    # Routes >2hr late on >10% of deliveries (brief Q5). Confirmed via
    # reconciliation: ALL 140 routes clear this bar -- systemic, not
    # route-specific. Dashboard should rank by severity, not treat this as
    # a pass/fail filter (see DECISIONS.md).
    "route_lateness": """
        SELECT
            d.route_id,
            COUNT(*) AS total_deliveries,
            SUM(CASE WHEN d.delay_minutes > 120 THEN 1 ELSE 0 END) AS late_over_2hr_count,
            SUM(CASE WHEN d.delay_minutes > 120 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS pct_late_over_2hr
        FROM fact_deliveries_clean d
        GROUP BY d.route_id
        HAVING COUNT(*) >= 10
    """,

    # Cold chain excursions per 100 chilled deliveries. NOTE (see DECISIONS.md):
    # temperature_excursion_flag shows near-identical rates on chilled (3.13%)
    # vs non-chilled (2.92%) deliveries -- expected to be near-zero for
    # non-chilled cargo if the flag were cold-chain-specific. Reported as
    # requested by the brief, with this caveat flagged, not investigated
    # further given time constraints.
    "cold_chain_excursions": """
        SELECT
            dc.has_chilled_item,
            COUNT(*) AS total_deliveries,
            SUM(d.temperature_excursion_flag) AS excursion_count,
            SUM(d.temperature_excursion_flag) * 100.0 / COUNT(*) AS excursions_per_100
        FROM fact_deliveries_clean d
        JOIN (
            SELECT ol.order_id,
                   MAX(CASE WHEN p.is_chilled = 1 THEN 1 ELSE 0 END) AS has_chilled_item
            FROM order_lines ol
            JOIN products p ON p.product_id = ol.product_id
            GROUP BY ol.order_id
        ) dc ON dc.order_id = d.order_id
        GROUP BY dc.has_chilled_item
    """,

    # Freight cost per delivered case -- PROXY METRIC, see DECISIONS.md.
    # Uses deliveries.fuel_cost_inr, which the data dictionary explicitly
    # flags as "driver-entered, not reconciled to carrier invoices." This is
    # NOT the real freight cost (that requires the partner carrier API, not
    # yet integrated). Shown with an explicit caveat on the dashboard.
    "freight_cost_proxy_by_warehouse": """
        SELECT
            d.warehouse_id,
            COUNT(DISTINCT d.delivery_id) AS total_deliveries,
            SUM(d.fuel_cost_inr) AS total_fuel_cost_inr,
            SUM(fol.delivered_eaches) AS total_delivered_eaches
        FROM fact_deliveries_clean d
        JOIN fact_order_lines_clean fol ON fol.order_id = d.order_id
        GROUP BY d.warehouse_id
    """,

    # Returns as % of dispatch value, by category, with leading reason code.
    # return_qty normalized via abs() per KP-2402 (see DECISIONS.md).
    "returns_by_category": """
        SELECT
            p.abc_class,
            r.return_reason_code,
            COUNT(*) AS return_count,
            SUM(r.return_qty_abs) AS total_return_qty,
            SUM(r.credit_note_value_inr) AS total_credit_note_value_inr
        FROM fact_returns_clean r
        JOIN products p ON p.product_id = r.product_id
        GROUP BY p.abc_class, r.return_reason_code
    """,
}


def create_views(marts_conn: sqlite3.Connection) -> None:
    log("Creating metric views...")
    cur = marts_conn.cursor()
    for name, sql in VIEW_DEFINITIONS.items():
        cur.execute(f"DROP VIEW IF EXISTS {name}")
        cur.execute(f"CREATE VIEW {name} AS {sql}")
        log(f"  created view: {name}")
    marts_conn.commit()


# --------------------------------------------------------------------------
# Write everything to the marts DB
# --------------------------------------------------------------------------
def _stringify_datetimes(df: pd.DataFrame) -> pd.DataFrame:
    """SQLite (via the plain sqlite3 driver) can't bind pandas Timestamp objects
    directly. Convert any datetime64 columns to ISO-format strings before writing,
    so downstream SQL can still filter/sort on them as text.

    Also catches columns that ended up as dtype=object but actually contain
    Timestamp/NaT values (e.g. built via row-wise .apply()) — those don't
    trip pd.api.types.is_datetime64_any_dtype, so we check for them explicitly."""
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
            df[col] = df[col].where(df[col].notna(), None)
        elif df[col].dtype == object:
            sample = df[col].dropna()
            if len(sample) and isinstance(sample.iloc[0], pd.Timestamp):
                df[col] = pd.to_datetime(df[col], errors="coerce")
                df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
                df[col] = df[col].where(df[col].notna(), None)
    return df


def write_marts(tables: dict) -> sqlite3.Connection:
    log(f"Writing marts to {MARTS_DB_PATH}...")
    marts = sqlite3.connect(MARTS_DB_PATH)
    for name, df in tables.items():
        df = _stringify_datetimes(df)
        df.to_sql(name, marts, if_exists="replace", index=False)
        log(f"  wrote {name}: {len(df):,} rows")
    return marts


def main() -> None:
    raw = require_raw_db()

    dim_outlets = build_dim_outlets(raw)
    fact_order_lines = build_fact_order_lines(raw)
    fact_orders = build_fact_orders(raw)
    fact_deliveries = build_fact_deliveries(raw)
    fact_returns = build_fact_returns(raw)
    check_price_history_coverage(raw)
    fact_order_lines_priced = build_fact_order_lines_priced(raw)
    reference_tables = copy_reference_tables(raw)

    all_tables = {
        "dim_outlets": dim_outlets,
        "fact_order_lines_clean": fact_order_lines,
        "fact_orders_clean": fact_orders,
        "fact_deliveries_clean": fact_deliveries,
        "fact_returns_clean": fact_returns,
        "fact_order_lines_priced": fact_order_lines_priced,
        **reference_tables,
    }

    marts_conn = write_marts(all_tables)
    create_views(marts_conn)
    marts_conn.close()

    raw.close()
    log("Done. Cleaned layer + metric views are ready in data/kestrel_marts.db.")
    log("Run the Streamlit app next: streamlit run app.py")


if __name__ == "__main__":
    main()
