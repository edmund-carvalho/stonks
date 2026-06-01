#!/usr/bin/env python3
"""
Full enrichment pipeline with incremental saving:
- NSE metadata from CSV files (capital, industry, sector, dependencies)
- Yahoo Finance raw stock.info (all fields)
- Always fetches fresh fundamentals (no skip)
- Saves after each successful symbol fetch to allow resume on crash
- Stops early if network appears down

Usage:
    python full_enrich.py input.json output.json
"""

import json
import sys
import csv
import os
import time
from typing import Dict
import requests

import yfinance as yf

# ------------------ Configuration ------------------
MAX_CONSECUTIVE_FAILURES = 3       # Stop after this many network errors in a row
DEFAULT_DELAY_SEC = 0.2            # Small delay to avoid rate limiting

_INSTRUMENT_REFRESH_PERIOD = (30*86400) # Refresh instrument list is older than 30 days

# ------------------ NSE CSV enrichment ------------------
INDUSTRY_TO_SECTOR = {
    "Financial Services": "Financial",
    "Banking": "Financial",
    "NBFC": "Financial",
    "Insurance": "Financial",
    "Asset Management": "Financial",
    "Capital Markets": "Financial",
    "Stock Exchanges": "Financial",
    "Information Technology": "Technology",
    "IT Services": "Technology",
    "Software": "Technology",
    "Technology": "Technology",
    "Healthcare": "Healthcare",
    "Pharmaceuticals": "Healthcare",
    "Hospitals": "Healthcare",
    "Biotechnology": "Healthcare",
    "Medical Equipment": "Healthcare",
    "Oil Gas & Consumable Fuels": "Energy",
    "Oil & Gas": "Energy",
    "Power": "Utilities",
    "Renewable Energy": "Utilities",
    "Fast Moving Consumer Goods": "Consumer Staples",
    "Consumer Durables": "Consumer Discretionary",
    "Consumer Services": "Consumer Discretionary",
    "Automobile and Auto Components": "Consumer Discretionary",
    "Textiles": "Consumer Discretionary",
    "Retail": "Consumer Discretionary",
    "Capital Goods": "Industrials",
    "Construction": "Industrials",
    "Construction Materials": "Materials",
    "Metals & Mining": "Materials",
    "Chemicals": "Materials",
    "Cement": "Materials",
    "Realty": "Real Estate",
    "Real Estate": "Real Estate",
    "Telecommunication": "Communication",
    "Media Entertainment & Publication": "Communication",
    "Media": "Communication",
    "Services": "Services",
    "Diversified": "Diversified",
    "Logistics": "Industrials",
    "Shipping": "Industrials",
    "Aviation": "Industrials",
    "Hotels": "Consumer Discretionary",
    "E-commerce": "Consumer Discretionary",
}

INDUSTRY_DEPENDENCIES = {
    "Financial Services": ["interest rates", "economy", "regulations", "credit policy", "NIM", "asset quality", "liquidity"],
    "Capital Goods": ["industrial capex", "infrastructure spending", "economy", "government spending", "order book"],
    "Oil Gas & Consumable Fuels": ["crude oil", "natural gas", "geopolitics", "refining margins", "currency", "global demand"],
    "Healthcare": ["pharmaceuticals", "regulations", "research", "demographics", "USFDA", "patents", "healthcare spending"],
    "Automobile and Auto Components": ["metals", "crude oil", "consumer demand", "technology", "supply chain", "rural demand", "interest rates"],
    "Information Technology": ["global economy", "currency", "technology", "talent", "digital transformation", "deal pipeline"],
    "Fast Moving Consumer Goods": ["commodities", "agriculture", "consumer spending", "rural demand", "branding", "distribution"],
    "Consumer Durables": ["metals", "plastics", "consumer spending", "technology", "real estate", "housing demand"],
    "Consumer Services": ["consumer spending", "economy", "disposable income", "digital adoption", "customer acquisition"],
    "Services": ["economy", "consumer spending", "business activity", "labor", "global trade", "infrastructure"],
    "Chemicals": ["crude oil", "petrochemicals", "global demand", "regulations", "environmental norms", "China demand"],
    "Realty": ["interest rates", "economy", "regulations", "demographics", "housing demand", "RERA", "steel", "cement"],
    "Telecommunication": ["spectrum", "technology", "regulations", "competition", "ARPU", "5G rollout", "subscriber growth"],
    "Power": ["coal", "gas", "renewables", "regulations", "power demand", "tariffs", "PPAs"],
    "Metals & Mining": ["commodities", "global demand", "currency", "China demand", "LME prices", "power costs"],
    "Construction": ["steel", "cement", "infrastructure spending", "labor", "order book", "government capex"],
    "Construction Materials": ["limestone", "coal", "infrastructure spending", "logistics", "real estate", "pricing power"],
    "Textiles": ["cotton", "yarn", "exports", "labor", "currency", "fashion trends", "PLI scheme"],
    "Diversified": ["economy", "multiple sectors", "management", "conglomerate discount", "capital allocation"],
    "Media Entertainment & Publication": ["advertising", "content", "consumer spending", "digital transformation", "OTT", "box office"],
}
DEFAULT_DEPENDENCIES = ["economy", "global markets", "regulations"]



def download_nifty_csv(url: str, local_path: str, max_age_days: int = _INSTRUMENT_REFRESH_PERIOD) -> bool:
    """
    Download a CSV from niftyindices.com if missing or older than max_age_days.
    Returns True if the file exists/is up‑to‑date after the attempt.
    """
    # If file exists and is fresh enough, skip download
    if os.path.exists(local_path):
        age = time.time() - os.path.getmtime(local_path)
        if age < max_age_days * 86400:
            return True

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/csv,application/csv,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.niftyindices.com/",
    } # NSE may block request without header !!
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        # Save only if content looks like CSV (starts with "Symbol" or similar)
        if b"Symbol" in resp.content[:200] or b"Industry" in resp.content[:200]:
            with open(local_path, "wb") as f:
                f.write(resp.content)
            print(f"  Downloaded {os.path.basename(local_path)}")
            return True
        else:
            print(f"  Warning: {url} returned unexpected content (not CSV)")
            return False
    except Exception as e:
        print(f"  Failed to download {url}: {e}")
        return False
    
# NOTE : We dont really need these csv data from NSE, 
#        yahoo finance API already provides market cap in rupee terms.''
#        The mapping of rupee value to SMALL/MID/LARGE cap is currently unknown and ambigious
#        Thats why we do this circus of parsing CSV :)

def load_market_cap_from_csvs() -> Dict[str, str]:
    market_cap = {}
    symbol_industry = {}
    csv_config = {
        "ind_nifty100list.csv": "LARGE",
        "ind_niftymidcap150list.csv": "MID",
        "ind_NiftySmallcap500_list.csv": "SMALL",
    }
    base_url = "https://www.niftyindices.com/IndexConstituent/"

    for filename, category in csv_config.items():
        local_path = filename
        url = base_url + filename

        # Try to download if missing or stale
        if not os.path.exists(local_path) or (time.time() - os.path.getmtime(local_path) > _INSTRUMENT_REFRESH_PERIOD):
            print(f"  Fetching {filename} from niftyindices.com...")
            success = download_nifty_csv(url, local_path)
            if not success:
                print(f"  Warning: Could not download {filename}, using existing file (if any) or skipping.")
                if not os.path.exists(local_path):
                    continue  # skip this category entirely

        # Now read the CSV (same as your original code)
        try:
            with open(local_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                if 'Symbol' not in (reader.fieldnames or []):
                    print(f"  Warning: {local_path} missing 'Symbol' column, skipping.")
                    continue
                for row in reader:
                    symbol = row.get('Symbol', '').strip().upper()
                    if symbol:
                        if symbol not in market_cap:
                            market_cap[symbol] = category
                        industry = row.get('Industry', '').strip()
                        if industry:
                            symbol_industry[symbol] = industry
        except Exception as e:
            print(f"  Error reading {local_path}: {e}")
            continue

    return market_cap, symbol_industry

# ------------------ Yahoo symbol conversion ------------------
INDEX_MAPPING = {
    "NIFTY 50": "^NSEI",
    "NIFTY50": "^NSEI",
    "INDIA VIX": "^INDIAVIX",
    "INDIAVIX": "^INDIAVIX",
    "BANK NIFTY": "^NSEBANK",
    "NIFTY BANK": "^NSEBANK",
}

def clean_symbol(symbol: str) -> str:
    """Convert user symbol to Yahoo Finance format."""
    orig = " ".join(symbol.split())  # normalize spaces
    if orig in INDEX_MAPPING:
        return INDEX_MAPPING[orig]
    # For equities: remove spaces and add .NS
    cleaned = ''.join(symbol.split())
    if '.' not in cleaned:
        cleaned = cleaned + ".NS"
    return cleaned

# ------------------ Yahoo fundamentals ------------------
def get_yahoo_fundamentals(ticker_str: str) -> Dict:
    """Return full info dict from Yahoo Finance. Raises exception on network errors."""
    stock = yf.Ticker(ticker_str)
    info = stock.info
    return info if isinstance(info, dict) else {}

# ------------------ Main pipeline with incremental saving ------------------
def full_enrich(input_file, output_file, delay_sec=DEFAULT_DELAY_SEC):
    print("📊 Loading NSE CSV metadata...")
    market_cap_map, symbol_industry = load_market_cap_from_csvs()
    print(f"   Found {len(market_cap_map)} symbols in CSVs")

    # Load input file
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    watchlist = data.get("watchlist", [])

    # Helper to write current state to output file (preserves all original keys)
    def save_progress():
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # First pass: add NSE metadata (capital, industry, sector, dependencies)
    for entry in watchlist:
        symbol_raw = entry.get("symbol", "")
        if not symbol_raw:
            print("⚠️  Skipping entry without 'symbol' key:", entry, file=sys.stderr)
            continue
        symbol_upper = symbol_raw.upper()
        capital = market_cap_map.get(symbol_upper, "UNKNOWN")
        industry = symbol_industry.get(symbol_upper)
        sector = INDUSTRY_TO_SECTOR.get(industry, "Other") if industry else "Other"
        dependencies = INDUSTRY_DEPENDENCIES.get(industry, DEFAULT_DEPENDENCIES) if industry else DEFAULT_DEPENDENCIES

        meta = entry.setdefault("metadata", {})
        meta["capital"] = capital
        meta["industry"] = industry
        meta["sector"] = sector
        meta["dependencies"] = dependencies

    # Save after metadata pass so it's not lost if the network phase fails early
    save_progress()
    print("   Metadata saved.")

    # Second pass: fetch Yahoo fundamentals incrementally
    print(f"\n📈 Fetching Yahoo fundamentals for {len(watchlist)} entries...")
    consecutive_failures = 0
    for idx, entry in enumerate(watchlist, 1):
        # Validate symbol again (in case entry was added without symbol)
        symbol_raw = entry.get("symbol", "")
        if not symbol_raw:
            print(f"[{idx}/{len(watchlist)}] SKIP (no symbol)")
            continue

        yf_symbol = clean_symbol(symbol_raw)
        
        # Indices have no meaningful fundamentals – skip with a note
        if yf_symbol.startswith('^'):
            print(f"[{idx}/{len(watchlist)}] {yf_symbol} ↪ index, skipping")
            entry.setdefault("metadata", {})["fundamentals"] = {
                "index": True,
                "note": "Index, no fundamentals available"
            }
            save_progress()
            continue

        print(f"[{idx}/{len(watchlist)}] {yf_symbol}", end=" ", flush=True)

        try:
            info = get_yahoo_fundamentals(yf_symbol)
            entry.setdefault("metadata", {})["fundamentals"] = info
            print("✓")
            consecutive_failures = 0
            save_progress()
        except Exception as e:
            print(f"✗ {e}")
            entry.setdefault("metadata", {})["fundamentals"] = {"error": str(e)}
            save_progress()
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print("⚠️  Too many consecutive failures – likely network issue. Stopping.")
                break

        time.sleep(delay_sec)

    # Final save (already saved incrementally, but ensure the last state is written)
    save_progress()
    print(f"\n✅ Full enrichment complete. Output: {output_file}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python full_enrich.py input.json output.json")
        sys.exit(1)
    full_enrich(sys.argv[1], sys.argv[2])