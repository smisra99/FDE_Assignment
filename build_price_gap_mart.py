"""
build_price_gap_mart.py
==========================
Matches raw_competitor_listings (scraped from BazaarPulse) to Kestrel's own
products table, and computes price gap: our MRP vs. the street price
retailers are actually charging.

Matching strategy:
    1. Parse brand + pack size out of each scraped listing (pack size is
       already a clean structured field from the scraper; brand is the
       first word(s) of the cleaned title).
    2. Join candidates on (brand, pack_value, pack_uom) -- usually narrows
       to 0 or 1 product, but a brand can have multiple SKUs at the same
       pack size in different categories (e.g. "Bluepeak Nuts 750g" vs
       "Bluepeak Milk 750g").
    3. Where multiple candidates remain, fuzzy-match the cleaned title
       against each candidate's product_name (difflib, stdlib only) and
       take the best match above a minimum similarity threshold.
    4. VALIDATION: compare the scraped listing's own displayed MRP against
       the matched product's real mrp_inr. These should be close if the
       match is correct -- a large gap is a signal the match may be wrong,
       flagged rather than silently trusted.

Unmatched listings (no brand/pack combination found in products, or no
candidate clears the fuzzy-match threshold) are kept in the output with
null product_id, not dropped -- so the match rate is visible and honest
rather than hidden by silently excluding failures.

Run after build_marts.py and build_bazaarpulse_mart.py:
    python build_price_gap_mart.py
"""

import re
import sqlite3
import sys
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

MARTS_DB_PATH = Path("data/kestrel_marts.db")
FUZZY_MATCH_THRESHOLD = 0.45  # conservative -- prefer "unmatched" over a wrong match

NOISE_PATTERNS = [
    r"\bPack of \d+\b",
    r"\bCombo\b",
    r"\(New\)",
    r"\|\s*Best Before \d+M",
    r"-\s*Family Pack",
]


def log(msg: str) -> None:
    print(f"[build_price_gap_mart] {msg}")


def clean_title(title: str) -> str:
    if not title:
        return ""
    text = title
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def parse_pack_size(pack_size_raw: str) -> tuple[float, str] | tuple[None, None]:
    """'400 ml' -> (400.0, 'ML'). Matches products.pack_size_value/uom units
    seen so far (G, ML, KG)."""
    if not pack_size_raw:
        return None, None
    match = re.match(r"([\d.]+)\s*(g|ml|kg|l)\b", pack_size_raw.strip(), re.IGNORECASE)
    if not match:
        return None, None
    return float(match.group(1)), match.group(2).upper()


def extract_brand(cleaned_title: str, known_brands: list[str]) -> str | None:
    """First word(s) of the cleaned title matched case-insensitively against
    the real distinct brand list from products -- not guessed patterns."""
    upper_title = cleaned_title.upper()
    for brand in sorted(known_brands, key=len, reverse=True):
        if upper_title.startswith(brand.upper()):
            return brand
    return None


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.upper(), b.upper()).ratio()


def match_listing(row: pd.Series, products: pd.DataFrame) -> pd.Series:
    cleaned = clean_title(row["title_raw"])
    pack_value, pack_uom = parse_pack_size(row["pack_size_raw"])

    brand = extract_brand(cleaned, products["brand"].dropna().unique().tolist())
    if brand is None or pack_value is None:
        return pd.Series({
            "matched_product_id": None, "matched_sku_code": None,
            "matched_product_name": None, "match_score": None,
            "match_reason": "no_brand_or_pack_parsed",
        })

    candidates = products[
        (products["brand"].str.upper() == brand.upper())
        & (products["pack_size_value"] == pack_value)
        & (products["pack_size_uom"].str.upper() == pack_uom)
    ]

    if candidates.empty:
        return pd.Series({
            "matched_product_id": None, "matched_sku_code": None,
            "matched_product_name": None, "match_score": None,
            "match_reason": "no_candidate_brand_pack_combo",
        })

    if len(candidates) == 1:
        best = candidates.iloc[0]
        score = similarity(cleaned, best["product_name"])
        reason = "unique_brand_pack_match"
    else:
        scores = candidates["product_name"].apply(lambda name: similarity(cleaned, name))
        best_idx = scores.idxmax()
        best = candidates.loc[best_idx]
        score = scores[best_idx]
        reason = f"fuzzy_tiebreak_among_{len(candidates)}_candidates"

    if score < FUZZY_MATCH_THRESHOLD:
        return pd.Series({
            "matched_product_id": None, "matched_sku_code": None,
            "matched_product_name": None, "match_score": round(score, 3),
            "match_reason": f"below_threshold_{FUZZY_MATCH_THRESHOLD} ({reason})",
        })

    return pd.Series({
        "matched_product_id": best["product_id"],
        "matched_sku_code": best["sku_code"],
        "matched_product_name": best["product_name"],
        "match_score": round(score, 3),
        "match_reason": reason,
    })


def main() -> None:
    if not MARTS_DB_PATH.exists():
        log(f"ERROR: {MARTS_DB_PATH} not found. Run build_marts.py and "
            f"build_bazaarpulse_mart.py first.")
        sys.exit(1)

    conn = sqlite3.connect(MARTS_DB_PATH)
    listings = pd.read_sql("SELECT * FROM raw_competitor_listings", conn)
    products = pd.read_sql("SELECT * FROM products", conn)
    log(f"Loaded {len(listings):,} listings and {len(products):,} products.")

    log(f"Distinct brands in products: {sorted(products['brand'].dropna().unique().tolist())}")

    log("Matching listings to products (brand + pack size, fuzzy tiebreak)...")
    match_results = listings.apply(lambda row: match_listing(row, products), axis=1)
    matched = pd.concat([listings, match_results], axis=1)

    n_matched = matched["matched_product_id"].notna().sum()
    match_rate = n_matched / len(matched) * 100
    log(f"Matched {n_matched:,}/{len(matched):,} listings ({match_rate:.1f}%).")
    log(f"Match failure reasons:\n{matched.loc[matched['matched_product_id'].isna(), 'match_reason'].value_counts().to_string()}")

    # Validation: compare scraped MRP to matched product's real MRP.
    matched_only = matched[matched["matched_product_id"].notna()].copy()
    products_mrp = products.set_index("product_id")["mrp_inr"]
    matched_only["our_mrp_inr"] = matched_only["matched_product_id"].map(products_mrp)
    matched_only["scraped_mrp_vs_our_mrp_diff_pct"] = (
        (matched_only["mrp_inr"] - matched_only["our_mrp_inr"]).abs()
        / matched_only["our_mrp_inr"] * 100
    )
    suspicious = matched_only[matched_only["scraped_mrp_vs_our_mrp_diff_pct"] > 10]
    if len(suspicious):
        log(f"  WARNING: {len(suspicious)} matches have >10% gap between scraped "
            f"MRP and our real MRP -- these are likely mismatches despite clearing "
            f"the fuzzy threshold. Flagged in output (see mrp_mismatch_flag column), "
            f"not dropped.")
    matched_only["mrp_mismatch_flag"] = matched_only["scraped_mrp_vs_our_mrp_diff_pct"] > 10

    matched_only["price_gap_inr"] = matched_only["price_inr"] - matched_only["our_mrp_inr"]
    matched_only["price_gap_pct"] = matched_only["price_gap_inr"] / matched_only["our_mrp_inr"] * 100

    matched_only.to_sql("competitor_price_gap", conn, if_exists="replace", index=False)
    log(f"  wrote competitor_price_gap: {len(matched_only):,} rows")

    # Keep the full set (matched + unmatched) too, for transparency on match rate.
    matched.to_sql("competitor_listings_matched_all", conn, if_exists="replace", index=False)
    log(f"  wrote competitor_listings_matched_all: {len(matched):,} rows "
        f"(includes unmatched, for transparency)")

    # Brief Q6: "For our top twenty SKUs by value, how does our MRP compare

    log("Building top-20-by-value price position view...")
    order_value = pd.read_sql("""
        SELECT product_id, SUM(line_value_inr) AS total_order_value_inr
        FROM order_lines
        GROUP BY product_id
        ORDER BY total_order_value_inr DESC
        LIMIT 20
    """, conn)

    clean_matches = matched_only[~matched_only["mrp_mismatch_flag"]]
    mumbai_prices = (
        clean_matches[clean_matches["city"] == "mumbai"]
        .groupby("matched_product_id")["price_inr"]
        .min()
        .reset_index()
        .rename(columns={"price_inr": "lowest_observed_price_mumbai_inr"})
    )

    top20 = order_value.merge(
        products[["product_id", "sku_code", "product_name", "mrp_inr"]],
        on="product_id", how="left"
    ).merge(
        mumbai_prices, left_on="product_id", right_on="matched_product_id", how="left"
    )
    top20["price_gap_inr"] = top20["lowest_observed_price_mumbai_inr"] - top20["mrp_inr"]
    top20["price_gap_pct"] = top20["price_gap_inr"] / top20["mrp_inr"] * 100
    n_no_mumbai_data = top20["lowest_observed_price_mumbai_inr"].isna().sum()
    if n_no_mumbai_data:
        log(f"  NOTE: {n_no_mumbai_data}/20 top-value SKUs have no validated Mumbai "
            f"listing match -- shown with null price gap, not silently dropped.")

    top20.to_sql("top20_skus_price_position_mumbai", conn, if_exists="replace", index=False)
    log(f"  wrote top20_skus_price_position_mumbai: {len(top20)} rows")

    conn.close()
    log("Done.")


if __name__ == "__main__":
    main()
