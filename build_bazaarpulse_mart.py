"""
build_bazaarpulse_mart.py
============================
Scrapes BazaarPulse (competitor price tracker) and writes raw listings into
data/kestrel_marts.db as raw_competitor_listings.

Run with the site already serving locally:
    cd bazaarpulse_site && python3 -m http.server 8080
Then:
    python build_bazaarpulse_mart.py
"""

import re
import sqlite3
import sys
import time
import urllib.robotparser
from html import unescape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "http://localhost:8080"
MARTS_DB_PATH = Path("data/kestrel_marts.db")
USER_AGENT = "KestrelControlTower/1.0"
CRAWL_DELAY_SECONDS = 1  # from robots.txt

CITIES_PAGINATED = ["mumbai", "delhi"]
CITIES_SINGLE_PAGE = ["bengaluru", "chennai"]


def log(msg: str) -> None:
    print(f"[build_bazaarpulse_mart] {msg}")


def get_robot_parser() -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{BASE_URL}/robots.txt")
    rp.read()
    return rp


def fetch(path: str, rp: urllib.robotparser.RobotFileParser) -> str | None:
    url = f"{BASE_URL}{path}"
    if not rp.can_fetch(USER_AGENT, url):
        log(f"  SKIPPED (disallowed by robots.txt): {path}")
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        time.sleep(CRAWL_DELAY_SECONDS)
        if resp.status_code != 200:
            log(f"  {path} -> HTTP {resp.status_code}, skipping")
            return None
        return resp.text
    except requests.RequestException as e:
        log(f"  {path} -> error: {e}, skipping")
        return None


def _clean_muted_text(div) -> str:
    return div.get_text(separator=" ", strip=True) if div else ""


def parse_price(card) -> float | None:
    
    """Mumbai"""
    price_span = card.select_one("span.price")
    if price_span:
        text = price_span.get_text(strip=True)
        match = re.search(r"[\d,]+\.?\d*", text)
        return float(match.group().replace(",", "")) if match else None

    """Bengaluru"""
    pricing_block = card.select_one("span.pricing-block")
    if pricing_block and pricing_block.has_attr("data-price-paise"):
        try:
            return float(pricing_block["data-price-paise"]) / 100.0
        except (ValueError, TypeError):
            return None

    """Delhi"""
    amt_div = card.select_one("div.amt")
    if amt_div:
        text = amt_div.get_text(separator=" ", strip=True)  # "Rs. 88.68 incl. taxes"
        match = re.search(r"[\d,]+\.\d+", text)
        return float(match.group().replace(",", "")) if match else None

    """Chennai"""
    selling_price = card.select_one("b.sellingPrice")
    if selling_price:
        text = selling_price.get_text(strip=True)  # "INR 229.86"
        match = re.search(r"[\d,]+\.?\d*", text)
        return float(match.group().replace(",", "")) if match else None

    return None


def parse_listing_card(card, city: str) -> dict:
    listing_id = card.get("data-listing-id")
    title_tag = card.select_one("a strong")
    title = title_tag.get_text(strip=True) if title_tag else None

    link_tag = card.select_one("a")
    product_url = link_tag["href"] if link_tag and link_tag.has_attr("href") else None

    muted_divs = card.select("div.muted")
    # First muted div: "Retailer . pack_size . Category"
    retailer, pack_size, category = None, None, None
    if len(muted_divs) >= 1:
        parts = [p.strip() for p in _clean_muted_text(muted_divs[0]).split(" . ")]
        # BeautifulSoup collapses &middot; to a literal middle dot char in get_text;
        # split on that character defensively in case " . " above doesn't match it.
        if len(parts) == 1:
            parts = [p.strip() for p in re.split(r"[·\u00B7]", _clean_muted_text(muted_divs[0]))]
        if len(parts) >= 3:
            retailer, pack_size, category = parts[0], parts[1], parts[2]

    # Second muted div: "MRP ₹Y . availability . rated R (N)"
    mrp_inr, availability, rating, rating_count = None, None, None, None
    if len(muted_divs) >= 2:
        text2 = _clean_muted_text(muted_divs[1])
        mrp_match = re.search(r"MRP\s*[₹\u20B9]\s*([\d,]+\.?\d*)", text2)
        if mrp_match:
            mrp_inr = float(mrp_match.group(1).replace(",", ""))
        if "unavailable" in text2.lower():
            availability = "OUT_OF_STOCK"
        elif "in stock" in text2.lower():
            availability = "IN_STOCK"
        rating_match = re.search(r"rated\s*([\d.]+)\s*\((\d+)\)", text2)
        if rating_match:
            rating = float(rating_match.group(1))
            rating_count = int(rating_match.group(2))

    # Third muted div: "Last seen: YYYY-MM-DD"
    last_seen = None
    if len(muted_divs) >= 3:
        text3 = _clean_muted_text(muted_divs[2])
        seen_match = re.search(r"(\d{4}-\d{2}-\d{2})", text3)
        if seen_match:
            last_seen = seen_match.group(1)

    return {
        "listing_id": listing_id,
        "city": city,
        "title_raw": title,
        "retailer": retailer,
        "pack_size_raw": pack_size,
        "category": category,
        "price_inr": parse_price(card),
        "mrp_inr": mrp_inr,
        "availability": availability,
        "rating": rating,
        "rating_count": rating_count,
        "last_seen": last_seen,
        "product_url": product_url,
    }


def get_total_pages(html: str) -> int:
    """Parses 'page 1 of 17' from the listing page header."""
    match = re.search(r"page\s+\d+\s+of\s+(\d+)", html, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def scrape_city(city: str, paginated: bool, rp: urllib.robotparser.RobotFileParser) -> tuple[list[dict], list]:
    log(f"Scraping {city} ({'paginated' if paginated else 'single-page/query-param'})...")
    listings = []
    raw_cards = []

    first_path = f"/city/{city}/page/1.html" if paginated else f"/city/{city}/index.html"
    html = fetch(first_path, rp)
    if html is None:
        log(f"  Could not fetch first page for {city}, skipping city entirely.")
        return listings, raw_cards

    total_pages = get_total_pages(html)
    log(f"  {city}: {total_pages} pages")

    for page_num in range(1, total_pages + 1):
        if page_num == 1:
            page_html = html
        else:
            path = (f"/city/{city}/page/{page_num}.html" if paginated
                    else f"/city/{city}/index.html?p={page_num}")
            page_html = fetch(path, rp)
            if page_html is None:
                log(f"  {city} page {page_num}: fetch failed, skipping this page "
                    f"(noted -- 'some pages are unreachable' per the doc)")
                continue

        soup = BeautifulSoup(page_html, "html.parser")
        cards = soup.select("div.product-item")
        for card in cards:
            listings.append(parse_listing_card(card, city))
            raw_cards.append(card)

    log(f"  {city}: {len(listings)} listings parsed")
    return listings, raw_cards


def write_to_marts(listings: list[dict]) -> None:
    if not MARTS_DB_PATH.exists():
        log(f"ERROR: {MARTS_DB_PATH} not found. Run build_marts.py first.")
        sys.exit(1)
    import pandas as pd
    df = pd.DataFrame(listings)
    conn = sqlite3.connect(MARTS_DB_PATH)
    df.to_sql("raw_competitor_listings", conn, if_exists="replace", index=False)
    log(f"  wrote raw_competitor_listings: {len(df):,} rows")
    conn.close()


def diagnose_missing_prices(all_listings: list[dict], all_cards: list) -> None:
    """When a meaningful fraction of listings have no parsed price, don't
    just warn -- show which cities/conditions correlate, and dump a few
    raw failing cards so the real markup can be inspected instead of
    guessed at again."""
    import pandas as pd
    df = pd.DataFrame(all_listings)
    missing = df[df["price_inr"].isna()]
    if missing.empty:
        return

    log(f"  Missing-price breakdown by city:\n{missing['city'].value_counts().to_string()}")
    if "availability" in missing.columns:
        log(f"  Missing-price breakdown by availability:\n"
            f"{missing['availability'].value_counts(dropna=False).to_string()}")

    debug_dir = Path("bazaarpulse_debug")
    debug_dir.mkdir(exist_ok=True)
    missing_ids = set(missing["listing_id"].astype(str))
    n_saved = 0
    for card in all_cards:
        if str(card.get("data-listing-id")) in missing_ids and n_saved < 5:
            (debug_dir / f"failing_card_{card.get('data-listing-id')}.html").write_text(
                str(card), encoding="utf-8"
            )
            n_saved += 1
    if n_saved:
        log(f"  Saved {n_saved} failing card(s) to {debug_dir.resolve()}/ for inspection.")


def main() -> None:
    try:
        resp = requests.get(BASE_URL, timeout=5)
        resp.raise_for_status()
    except requests.RequestException:
        log(f"ERROR: could not reach {BASE_URL}. Start the site first:")
        log(f"  cd bazaarpulse_site && python3 -m http.server 8080")
        sys.exit(1)

    rp = get_robot_parser()

    all_listings = []
    all_cards = []
    for city in CITIES_PAGINATED:
        listings, cards = scrape_city(city, paginated=True, rp=rp)
        all_listings.extend(listings)
        all_cards.extend(cards)
    for city in CITIES_SINGLE_PAGE:
        listings, cards = scrape_city(city, paginated=False, rp=rp)
        all_listings.extend(listings)
        all_cards.extend(cards)

    log(f"Total listings scraped: {len(all_listings):,} "
        f"(expected ~1,137 per 03_External_Sources.md)")

    # Sanity check: how many parsed successfully vs have nulls in key fields
    n_no_price = sum(1 for l in all_listings if l["price_inr"] is None)
    n_no_title = sum(1 for l in all_listings if l["title_raw"] is None)
    if n_no_price:
        log(f"  WARNING: {n_no_price} listings have no parsed price -- "
            f"check both price markup variants are being matched correctly.")
        diagnose_missing_prices(all_listings, all_cards)
    if n_no_title:
        log(f"  WARNING: {n_no_title} listings have no parsed title.")

    write_to_marts(all_listings)
    log("Done. Next: match raw_competitor_listings to Kestrel's own products "
        "(filter to Kestrel-branded titles, join on pack size) -- see DECISIONS.md "
        "for why this is a separate step pending products' real schema.")


if __name__ == "__main__":
    main()
