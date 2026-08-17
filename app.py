"""
Kestrel Provisions control tower — Streamlit dashboard.

Reads ONLY from data/kestrel_marts.db (never the raw ops db) — run
build_marts.py first to generate it.

"""

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

MARTS_DB_PATH = Path("data/kestrel_marts.db")

st.set_page_config(
    page_title="Kestrel Control Tower",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    if not MARTS_DB_PATH.exists():
        st.error(
            f"Could not find {MARTS_DB_PATH}. Run `python build_marts.py` first "
            f"from the folder containing the `data` directory, then restart this app."
        )
        st.stop()
    return sqlite3.connect(MARTS_DB_PATH, check_same_thread=False)


@st.cache_data(ttl=600)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    return pd.read_sql(sql, conn, params=params)


def region_filter_ui(df: pd.DataFrame, region_col: str = "region_name") -> pd.DataFrame:
    if region_col not in df.columns:
        return df
    regions = ["All"] + sorted(df[region_col].dropna().unique().tolist())
    choice = st.selectbox("Region", regions, key=f"region_filter_{region_col}")
    if choice != "All":
        return df[df[region_col] == choice]
    return df


# --------------------------------------------------------------------------
# Sidebar navigation
# --------------------------------------------------------------------------
st.sidebar.title("Kestrel Control Tower")
page = st.sidebar.radio(
    "View",
    ["Overview (Q1 FY27)", "Service — Fill Rate", "Service — OTIF & Routes",
     "Cold Chain", "Money", "Ask Anything"],
)

st.sidebar.markdown("---")
st.sidebar.caption(
    "Data: 18 months to 30 Jun 2026. Q1 FY27 (Apr–Jun 2026) confirmed complete "
    "— see DECISIONS.md."
)

# --------------------------------------------------------------------------
# Overview — the "one screen" Divya asked for
# --------------------------------------------------------------------------
if page == "Overview (Q1 FY27)":
    st.title("Overview — Q1 FY27 (Apr–Jun 2026)")
    st.caption(
        "Front page per Rakesh's request: FY runs Apr–Mar, so Q1 FY27 = Apr–Jun 2026. "
        "Confirmed as a complete quarter (daily order volume holds steady through 30 Jun)."
    )

    otif = query("SELECT * FROM otif_by_region")
    fill = query("SELECT * FROM fill_rate_by_outlet")
    cold = query("SELECT * FROM cold_chain_excursions")
    routes = query("SELECT * FROM route_lateness")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        overall_otif = (otif["otif_count_60min_grace"].sum() /
                         otif["total_deliveries"].sum())
        st.metric("OTIF (60-min grace)", f"{overall_otif:.1%}",
                   help="On-time (within 60 min of planned) and in full. "
                        "Strict (0-min) OTIF is ~30% — see Service tab.")
    with col2:
        avg_fill = (fill["total_delivered_eaches"].sum() /
                    fill["total_ordered_eaches"].sum())
        st.metric("Fill Rate (eaches)", f"{avg_fill:.1%}",
                   help="Eaches, per Rakesh's instruction — not cases.")
    with col3:
        chilled = cold[cold["has_chilled_item"] == 1]
        excursion_rate = (chilled["excursion_count"].sum() /
                           chilled["total_deliveries"].sum() * 100)
        st.metric("Cold Chain Excursions", f"{excursion_rate:.1f} / 100",
                   help="Per 100 chilled deliveries. Caveat: non-chilled "
                        "deliveries show a nearly identical rate — see DECISIONS.md.")
    with col4:
        pct_routes_flagged = (routes["pct_late_over_2hr"] > 0.10).mean() * 100
        st.metric("Routes >2hr Late (>10% of deliveries)", f"{pct_routes_flagged:.0f}%",
                   help="ALL 140 routes currently clear this bar — a network-wide "
                        "delay issue, not a route-specific one. See Service tab.")

    st.warning(
        "**Systemic delay finding:** every route in the network exceeds the "
        "'2hr late on >10% of deliveries' threshold, and OTIF is nearly uniform "
        "across all 5 regions (~62% with 60-min grace). This points to a "
        "structural planning/capacity issue, not a regional or route-specific "
        "problem. See the Service tab for details.",
        icon="⚠️",
    )

    st.subheader("Worst 5 outlets by fill rate (last complete month — Jun 2026)")
    worst_fill = query("""
        SELECT ou.outlet_code, ou.outlet_name, ou.city_clean AS city,
               SUM(fol.delivered_eaches) * 1.0 / NULLIF(SUM(fol.ordered_eaches), 0) AS fill_rate_eaches,
               SUM(fol.ordered_eaches) AS total_ordered_eaches
        FROM fact_order_lines_clean fol
        JOIN fact_orders_clean o ON o.order_id = fol.order_id
        JOIN dim_outlets ou ON ou.outlet_id = o.outlet_id
        WHERE o.order_status != 'CANCELLED' AND ou.is_test = 0
          AND o.order_date >= '2026-06-01' AND o.order_date < '2026-07-01'
        GROUP BY ou.outlet_code, ou.outlet_name, ou.city_clean
        HAVING total_ordered_eaches > 0
        ORDER BY fill_rate_eaches ASC
        LIMIT 5
    """)
    worst_fill["fill_rate_eaches"] = (worst_fill["fill_rate_eaches"] * 100).round(1)
    st.dataframe(
        worst_fill.rename(columns={
            "outlet_code": "Outlet Code", "outlet_name": "Outlet", "city": "City",
            "fill_rate_eaches": "Fill Rate %", "total_ordered_eaches": "Ordered (eaches)",
        }),
        hide_index=True, width='stretch',
    )

# --------------------------------------------------------------------------
# Service — Fill Rate
# --------------------------------------------------------------------------
elif page == "Service — Fill Rate":
    st.title("Fill Rate (eaches)")
    st.caption(
        "Reported in eaches per Rakesh's override — modern trade penalizes on "
        "units short, not cases. Toggle below to see the cases view if needed."
    )

    period = st.radio("Period", ["Last complete month (Jun 2026)", "Full 18 months"],
                       horizontal=True)

    if period.startswith("Last"):
        date_filter = "AND o.order_date >= '2026-06-01' AND o.order_date < '2026-07-01'"
    else:
        date_filter = ""

    df = query(f"""
        SELECT ou.outlet_code, ou.outlet_name, ou.city_clean AS city, ou.region_id,
               SUM(fol.delivered_eaches) * 1.0 / NULLIF(SUM(fol.ordered_eaches), 0) AS fill_rate_eaches,
               SUM(fol.ordered_eaches) AS total_ordered_eaches
        FROM fact_order_lines_clean fol
        JOIN fact_orders_clean o ON o.order_id = fol.order_id
        JOIN dim_outlets ou ON ou.outlet_id = o.outlet_id
        WHERE o.order_status != 'CANCELLED' AND ou.is_test = 0
        {date_filter}
        GROUP BY ou.outlet_code, ou.outlet_name, ou.city_clean, ou.region_id
        HAVING total_ordered_eaches > 0
    """)

    n_worst = st.slider("Show worst N outlets", 5, 50, 10)
    worst = df.sort_values("fill_rate_eaches").head(n_worst).copy()
    worst["fill_rate_eaches"] = (worst["fill_rate_eaches"] * 100).round(1)

    st.subheader(f"Worst {n_worst} outlets by fill rate")
    st.dataframe(
        worst[["outlet_code", "outlet_name", "city", "fill_rate_eaches", "total_ordered_eaches"]]
        .rename(columns={"outlet_code": "Code", "outlet_name": "Outlet", "city": "City",
                          "fill_rate_eaches": "Fill Rate %", "total_ordered_eaches": "Ordered (eaches)"}),
        hide_index=True, width='stretch',
    )

    st.subheader("Distribution across all outlets")
    st.bar_chart(df.set_index("outlet_code")["fill_rate_eaches"].sort_values())

# --------------------------------------------------------------------------
# Service — OTIF & Routes
# --------------------------------------------------------------------------
elif page == "Service — OTIF & Routes":
    st.title("OTIF & Route Lateness")

    st.subheader("OTIF by region")
    st.caption(
        "Two thresholds shown: strict (delivered at/before planned time) and "
        "60-min grace (standard industry tolerance). Both are nearly uniform "
        "across regions — a systemic delay issue, not regional. Avg delay "
        "network-wide is ~132 minutes."
    )
    otif = query("SELECT * FROM otif_by_region ORDER BY otif_rate_60min_grace ASC")
    otif_display = otif.copy()
    otif_display["otif_rate_60min_grace"] = (otif_display["otif_rate_60min_grace"] * 100).round(1)
    otif_display["otif_rate_strict"] = (otif_display["otif_rate_strict"] * 100).round(1)
    st.dataframe(
        otif_display[["region_name", "total_deliveries", "otif_rate_60min_grace", "otif_rate_strict"]]
        .rename(columns={"region_name": "Region", "total_deliveries": "Deliveries",
                          "otif_rate_60min_grace": "OTIF % (60-min grace)",
                          "otif_rate_strict": "OTIF % (strict)"}),
        hide_index=True, width='stretch',
    )

    st.subheader("Routes — >2hr late, ranked by severity")
    st.warning(
        "All 140 routes in the network exceed the brief's literal threshold "
        "(>2hr late on >10% of deliveries) — this list is ranked by severity "
        "as the more useful framing, since the threshold itself doesn't "
        "discriminate in this dataset. See DECISIONS.md.",
        icon="⚠️",
    )
    routes = query("""
        SELECT route_id, total_deliveries, late_over_2hr_count, pct_late_over_2hr
        FROM route_lateness
        ORDER BY pct_late_over_2hr DESC
        LIMIT 20
    """)
    routes["pct_late_over_2hr"] = (routes["pct_late_over_2hr"] * 100).round(1)
    st.dataframe(
        routes.rename(columns={"route_id": "Route", "total_deliveries": "Deliveries",
                                "late_over_2hr_count": ">2hr Late Count",
                                "pct_late_over_2hr": "% >2hr Late"}),
        hide_index=True, width='stretch',
    )

# --------------------------------------------------------------------------
# Cold Chain
# --------------------------------------------------------------------------
elif page == "Cold Chain":
    st.title("Cold Chain")

    cold = query("SELECT * FROM cold_chain_excursions")
    chilled = cold[cold["has_chilled_item"] == 1].iloc[0]
    non_chilled = cold[cold["has_chilled_item"] == 0].iloc[0]

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Excursions per 100 chilled deliveries",
                   f"{chilled['excursions_per_100']:.2f}")
    with col2:
        st.metric("Excursions per 100 non-chilled deliveries",
                   f"{non_chilled['excursions_per_100']:.2f}")

    st.warning(
        "**Data quality caveat:** these two rates are nearly identical "
        "(chilled ≈ 3.13%, non-chilled ≈ 2.92%). If temperature_excursion_flag "
        "were genuinely cold-chain-specific, the non-chilled rate should be "
        "near zero. This suggests the flag may capture a more general vehicle/"
        "sensor event rather than a true cold-chain breach. Reported here as "
        "the brief requested, with this caveat flagged rather than hidden — "
        "see DECISIONS.md.",
        icon="⚠️",
    )

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------
elif page == "Money":
    st.title("Money")

    st.subheader("Freight cost per delivered case")
    try:
        freight = query("SELECT * FROM freight_cost_real_by_warehouse ORDER BY warehouse_code")
        if freight["total_freight_cost_inr"].notna().any():
            st.caption(
                "Real, carrier-billed freight cost from the partner logistics API "
                "(invoices are billed at route/warehouse/day level, joined here by warehouse)."
            )
            freight_display = freight.copy()
            freight_display["freight_cost_per_delivered_each_inr"] = (
                freight_display["freight_cost_per_delivered_each_inr"].round(2)
            )
            st.dataframe(
                freight_display[["warehouse_code", "warehouse_name", "invoice_count",
                                  "total_freight_cost_inr", "total_delivered_eaches",
                                  "freight_cost_per_delivered_each_inr"]]
                .rename(columns={
                    "warehouse_code": "Warehouse", "warehouse_name": "Name",
                    "invoice_count": "Invoices", "total_freight_cost_inr": "Total Freight Cost (INR)",
                    "total_delivered_eaches": "Delivered (eaches)",
                    "freight_cost_per_delivered_each_inr": "Freight Cost / Delivered Each (INR)",
                }),
                hide_index=True, width='stretch',
            )
        else:
            raise ValueError("view exists but has no data")
    except Exception:
        st.warning(
            "**Proxy metric — real freight data not available.** Falls back to "
            "`fuel_cost_inr`, which the data dictionary explicitly flags as "
            "driver-entered and NOT reconciled to carrier invoices. Run "
            "`build_freight_mart.py` to populate real freight cost.",
            icon="⚠️",
        )
        freight = query("SELECT * FROM freight_cost_proxy_by_warehouse")
        freight["fuel_cost_per_delivered_eaches"] = (
            freight["total_fuel_cost_inr"] / freight["total_delivered_eaches"].replace(0, pd.NA)
        ).round(2)
        st.dataframe(
            freight[["warehouse_id", "total_deliveries", "total_fuel_cost_inr",
                     "fuel_cost_per_delivered_eaches"]]
            .rename(columns={"warehouse_id": "Warehouse", "total_deliveries": "Deliveries",
                              "total_fuel_cost_inr": "Total Fuel Cost (INR)",
                              "fuel_cost_per_delivered_eaches": "Fuel Cost / Delivered Each (INR)"}),
            hide_index=True, width='stretch',
        )

    st.subheader("Returns by category (ABC class) and leading reason code")
    returns = query("""
        SELECT abc_class, return_reason_code, return_count, total_return_qty,
               total_credit_note_value_inr
        FROM returns_by_category
        ORDER BY total_credit_note_value_inr DESC
    """)
    st.dataframe(
        returns.rename(columns={
            "abc_class": "ABC Class", "return_reason_code": "Reason Code",
            "return_count": "Return Count", "total_return_qty": "Total Qty Returned",
            "total_credit_note_value_inr": "Total Credit Note Value (INR)",
        }),
        hide_index=True, width='stretch',
    )

    st.subheader("Price position vs. competitors — top 20 SKUs by value (Mumbai)")
    try:
        top20 = query("""
            SELECT sku_code, product_name, total_order_value_inr, mrp_inr,
                   lowest_observed_price_mumbai_inr, price_gap_inr, price_gap_pct
            FROM top20_skus_price_position_mumbai
            ORDER BY total_order_value_inr DESC
        """)
        if top20.empty:
            raise ValueError("view exists but is empty")
        n_missing = top20["lowest_observed_price_mumbai_inr"].isna().sum()
        st.caption(
            "Matched from scraped BazaarPulse listings to Kestrel's own product "
            "catalog (brand + pack size, fuzzy tiebreak on title, filtered to "
            "matches passing an MRP-consistency check). "
            + (f"{n_missing}/20 top-value SKUs have no validated Mumbai match "
               f"(not stocked by tracked retailers there, or filtered out by the "
               f"consistency check) — shown with a blank gap, not hidden."
               if n_missing else "")
        )
        top20_display = top20.copy()
        for col in ["mrp_inr", "lowest_observed_price_mumbai_inr", "price_gap_inr"]:
            top20_display[col] = top20_display[col].round(2)
        top20_display["price_gap_pct"] = top20_display["price_gap_pct"].round(1)
        st.dataframe(
            top20_display.rename(columns={
                "sku_code": "SKU", "product_name": "Product",
                "total_order_value_inr": "Order Value (INR)", "mrp_inr": "Our MRP (INR)",
                "lowest_observed_price_mumbai_inr": "Lowest Observed (Mumbai, INR)",
                "price_gap_inr": "Gap (INR)", "price_gap_pct": "Gap %",
            }),
            hide_index=True, width='stretch',
        )
    except Exception:
        st.info(
            "**Not yet built.** Run `build_bazaarpulse_mart.py` then "
            "`build_price_gap_mart.py` to populate this.",
            icon="ℹ️",
        )

# --------------------------------------------------------------------------
# Ask Anything
# --------------------------------------------------------------------------
elif page == "Ask Anything":
    st.title("Ask Anything")
    st.info(
        "**Not yet built.** Planned: constrain an LLM to query only the "
        "metric views above (fill_rate_by_outlet, otif_by_region, "
        "route_lateness, cold_chain_excursions, freight_cost_proxy_by_warehouse, "
        "returns_by_category) via defined functions/tools, never raw SQL "
        "against the 13 source tables — grounded answers, no hallucinated "
        "numbers. See DECISIONS.md for scope and what's next.",
        icon="ℹ️",
    )
    st.text_input("Try a question (not yet wired up):",
                   placeholder="e.g. why did fill rate drop in the West last week?",
                   disabled=True)
