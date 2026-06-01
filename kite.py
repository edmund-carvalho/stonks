import asyncio
import argparse
import os
import json
from datetime import datetime, timedelta, time, date
from mcp import ClientSession
from mcp.client.sse import sse_client
import urllib.request
import csv

# ----------------------------------------------------------------------
#  Market schedule constants
# ----------------------------------------------------------------------
MARKET_OPEN = time(9, 0)        # pre‑open starts
MARKET_CLOSE = time(16, 0)      # closing session ends

# --- Holiday handling ---
# Hardcoded NSE trading holidays for 2026.  Override with a "holidays.json" file.
_DEFAULTS = {
    date(2026, 1, 15),   # Municipal Corporation Election - Maharashtra
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali-Balipratipada
    date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),  # Christmas
}

_INSTRUMENT_REFRESH_PERIOD = (30*86400) # Refresh instrument list is older than 30 days

def load_holidays():
    """Return a set of holiday dates. Uses holidays.json if present."""
    if os.path.exists("holidays.json"):
        try:
            with open("holidays.json", "r") as f:
                data = json.load(f)
            return {datetime.strptime(d, "%Y-%m-%d").date() for d in data}
        except Exception:
            pass
    return _DEFAULTS

HOLIDAYS = load_holidays()


# ----------------------------------------------------------------------
#  Tiny helper functions
# ----------------------------------------------------------------------
def is_trading_day(d):
    """Return True if the given date is a trading day (not weekend/holiday)."""
    return d.weekday() < 5 and d not in HOLIDAYS


def next_trading_day(d):
    """Return the next trading day strictly after the given date."""
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def is_market_open(dt=None):
    """True if dt (default now) is within trading hours and not a holiday."""
    if dt is None:
        dt = datetime.now()
    if not is_trading_day(dt.date()):
        return False
    t = dt.time()
    return MARKET_OPEN <= t < MARKET_CLOSE


def safe_to_date(dt=None):
    """Return a 'YYYY-MM-DD 23:59:59' string suitable for the API.
       If the market is open, use yesterday (to avoid partial candle);
       otherwise use today."""
    if dt is None:
        dt = datetime.now()
    if is_market_open(dt):
        dt = dt - timedelta(days=1) # do not fetch incomplete daily cadle !
    return dt.strftime("%Y-%m-%d 23:59:59")


def parse_candle_date(s):
    """Parse a candle date string that may be in one of several formats."""
    s = str(s)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt)
        except ValueError:
            continue
    return datetime.strptime(s[:10], "%Y-%m-%d")


# ----------------------------------------------------------------------
#  Kite MCP wrapper
# ----------------------------------------------------------------------
class KiteMCPWrapper:
    def __init__(self, api_delay=1):
        self.SERVER_URL = "https://mcp.kite.trade/sse"
        self.session = None
        self.api_delay = api_delay

    async def connect(self):
        return sse_client(url=self.SERVER_URL)

    async def login(self, session):
        self.session = session
        login_result = await session.call_tool("login", arguments={})
        login_msg = login_result.content[0].text
        print(f"\nACTION REQUIRED: Login here:\n{login_msg}\n")
        input("Press Enter AFTER you see 'Success' in your browser...")
        await asyncio.sleep(2)

    async def get_historical_data(self, token, from_date=None, to_date=None):
        try:
            result = await self.session.call_tool("get_historical_data", {
                "instrument_token": token,
                "interval": "day",
                "from_date": from_date,
                "to_date": to_date
            })
            await asyncio.sleep(self.api_delay)
            if result.isError:
                error_msg = result.content[0].text if result.content else "Unknown error"
                print(f"API error for token {token}: {error_msg}")
                return None
            return result.content[0].text
        except Exception as e:
            print(f"API error: {e}")
            return None

    async def get_instrument_token(self, symbol, exchange="NSE"):
        
        result = await self.session.call_tool("search_instruments", {
            "exchange": exchange,
            "query": symbol
        })
        await asyncio.sleep(self.api_delay)
        
        instruments = json.loads(result.content[0].text)
        for inst in instruments:
            if (inst["tradingsymbol"].upper() == symbol.upper()
                    and inst["instrument_type"] == "EQ"):
                return int(inst["instrument_token"])
        return None


# ----------------------------------------------------------------------
#  Utility functions
# ----------------------------------------------------------------------
def build_work_list(args):
    """Build task list from job file or CLI arguments, removing duplicates."""
    tasks_dict = {}
    if args.job and os.path.exists(args.job):
        with open(args.job, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for item in data.get("watchlist", []):
                sym = item["symbol"].upper()
                # Store the full object directly in the dictionary
                tasks_dict[sym] = {
                    "symbol": sym,
                    "days": args.days,
                    "metadata": item.get("metadata", {})
                }
    elif args.symbols:
        for s in args.symbols:
            sym = s.upper()
            # Must match the same structure as above
            tasks_dict[sym] = {
                "symbol": sym,
                "days": args.days,
                "metadata": None
            }

    # Simply return the values; they are already the dictionaries you want
    return list(tasks_dict.values())

def save_candles(symbol, candles, metadata, output_dir):
    """Save candles to JSON file."""
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, f"{symbol}.json")
    with open(filename, 'w') as f:
        json.dump({"metadata": metadata, "data": candles}, f, indent=2)

def parse_nse_tokens_file(file="instruments.csv"):
    """For faster lookup without API usage"""
    if not os.path.exists(file) or (time.time() - os.path.getmtime(file) > _INSTRUMENT_REFRESH_PERIOD):
        print("--- Syncing Master Instrument List (NSE) ---")
        urllib.request.urlretrieve("https://api.kite.trade/instruments", file)
    with open(file, 'r', encoding='utf-8') as f:
        return list(csv.DictReader(f))

def lookup_nse_token_fast(symbol, instruments):
    """For faster lookup without API usage"""
    for instrument in instruments:
        if (instrument['tradingsymbol'] == symbol.upper()
                and instrument['exchange'] == 'NSE'
                and instrument['instrument_type'] == 'EQ'):
            return int(instrument['instrument_token'])
    return None


def parse_user_args():
    parser = argparse.ArgumentParser(
        description="Fetch historical data for NSE stocks via Kite MCP"
    )
    parser.add_argument("--symbols", nargs='+', help="NSE symbols (space separated)")
    parser.add_argument("--days", type=int, default=2000,
                        help="Number of days to fetch (default: 2000)")
    parser.add_argument("--job", "-j", type=str,
                        help="Path to job JSON file (e.g., dailyJobs.json)")
    parser.add_argument("--output-dir", "-o", type=str, default=".",
                        help="Directory to save JSON files (default: current directory)")
    parser.add_argument("--delay", type=float, default=1,
                        help="Delay between API calls in seconds (default: 1.0)")
    parser.add_argument("--force-download", "-f", default=False, action="store_true",
                        help="Force re-download data")
    parser.add_argument("--update", "-u", default=False, action="store_true",
                        help="Update mode: fetch only missing days since last saved candle")
    parser.add_argument("--update-metadata-only", default=False, action="store_true",
                        help="Only update metadata in saved JSON files from the job file (no data fetch)")
    return parser.parse_args()

# <-- UPDATE MODE: helper to get last date from saved file
def get_last_candle_date(symbol, output_dir):
    """Return the latest date (as datetime) from saved candles, or None if no data."""
    filename = os.path.join(output_dir, f"{symbol}.json")
    if not os.path.exists(filename):
        return None
    try:
        with open(filename, 'r') as f:
            data = json.load(f)
        candles = data.get("data", [])
        if not candles:
            return None
        dates = [parse_candle_date(c["date"]) for c in candles]
        return max(dates)
    except Exception:
        return None

def get_fetch_params(symbol, args, update_mode, output_dir):
    now = datetime.now()
    from_dt = (now - timedelta(days=args.days)).strftime("%Y-%m-%d 00:00:00")
    to_dt = safe_to_date(now)
    is_full_fetch = True

    if args.force_download:
        return from_dt, to_dt, is_full_fetch

    last = get_last_candle_date(symbol, output_dir)
    if last is None:
        return from_dt, to_dt, is_full_fetch

    if not update_mode:
        return from_dt, to_dt, is_full_fetch

    # Update mode: fetch only from the next TRADING day onward
    next_trade = next_trading_day(last)
    # If the next trading day is after our safe to_date, nothing to fetch
    if next_trade.strftime("%Y-%m-%d") > to_dt[:10]:
        return None
    # Otherwise fetch from that next trading day
    from_dt = next_trade.strftime("%Y-%m-%d 00:00:00")
    return from_dt, to_dt, False

def merge_candles(existing, new, is_full_fetch):
    if is_full_fetch:
        return new
    merged = {c["date"]: c for c in existing}
    merged.update({c["date"]: c for c in new})
    return sorted(merged.values(), key=lambda x: x["date"])

def filter_incomplete_today_candles(candles):
    """Remove today's candle if the market is still open."""
    now = datetime.now()
    if not is_market_open(now):
        return candles
    today_str = now.strftime("%Y-%m-%d")
    return [c for c in candles if c.get("date", "")[:10] != today_str]

async def fetch_candles(symbol, kite, instruments, args, update_mode, output_dir):
    params = get_fetch_params(symbol, args, update_mode, output_dir)
    if params is None:
        return None, None  # Skip

    from_date, to_date, is_full_fetch = params

    token = lookup_nse_token_fast(symbol, instruments)
    if not token:
        token = await kite.get_instrument_token(symbol)
        if not token:
            print(f"  ERROR: Token not found")
            return None, None

    raw = await kite.get_historical_data(token, from_date=from_date, to_date=to_date)

    if raw is None:
        return None, None
    new_candles = json.loads(raw)
    if not new_candles:
        print(f"  No data received for range")
        return None, None

    new_candles = filter_incomplete_today_candles(new_candles)
    existing = []
    metadata = None
    if not is_full_fetch:
        filename = os.path.join(output_dir, f"{symbol}.json")
        if os.path.exists(filename):
            try:
                with open(filename, 'r') as f:
                    data = json.load(f)
                existing = data.get("data", [])
                metadata = data.get("metadata")
            except Exception:
                pass

    final_candles = merge_candles(existing, new_candles, is_full_fetch)
    return final_candles, metadata


# ----------------------------------------------------------------------
#  Metadata‑only update (synchronous, no Kite needed)
# ----------------------------------------------------------------------
def update_metadata_only(tasks, output_dir):
    """
    For each task, if the saved candle file exists, replace its 'metadata'
    with the job‑file metadata and save. Tasks without metadata are skipped.
    """
    updated = 0
    for item in tasks:
        symbol = item['symbol']
        metadata = item['metadata']
        if metadata is None:
            print(f"[SKIP] {symbol} – no metadata in job")
            continue
        filename = os.path.join(output_dir, f"{symbol}.json")
        if not os.path.exists(filename):
            print(f"[SKIP] {symbol} – file not found")
            continue
        try:
            with open(filename, 'r') as f:
                data = json.load(f)
            data['metadata'] = metadata
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"[OK] {symbol} metadata updated")
            updated += 1
        except Exception as e:
            print(f"[FAIL] {symbol} – {e}")
    print(f"Updated metadata for {updated} symbol(s).")


# ----------------------------------------------------------------------
#  Main orchestrator
# ----------------------------------------------------------------------
async def run_app():
    args = parse_user_args()

    # Handle metadata‑only mode early – no Kite connection needed
    if args.update_metadata_only:
        if not args.job:
            print("Error: --update-metadata-only requires a --job file to get metadata.")
            return
        tasks = build_work_list(args)
        if not tasks:
            print("No symbols found in job file.")
            return
        print("Updating metadata only (no data fetch)...")
        update_metadata_only(tasks, args.output_dir)
        return

    tasks = build_work_list(args)
    instruments = parse_nse_tokens_file("instruments.csv")

    if not tasks:
        print("No tasks found. Provide --symbols or --job.")
        return

    print(f"\n{'='*60}")
    print(f"Kite Historical Data Fetcher")
    if args.update:
        print("UPDATE MODE: fetch only missing days")
    if args.force_download:
        print("FORCE DOWNLOAD: ignoring existing data")
    print(f"Output directory: {args.output_dir}")
    print(f"{'-'*60}\n")

    os.makedirs(args.output_dir, exist_ok=True)
    kite = KiteMCPWrapper(api_delay=args.delay)

    async with await kite.connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            kite.session = session
            await kite.login(session)

            for idx, item in enumerate(tasks, 1):
                symbol = item['symbol']
                print(f"[{idx}/{len(tasks)}] {symbol}...")

                candles, metadata = await fetch_candles(
                    symbol, kite, instruments, args,
                    update_mode=args.update,
                    output_dir=args.output_dir
                )

                if not candles or candles is None:
                    continue

                final_metadata = metadata if metadata is not None else item['metadata']
                save_candles(symbol, candles, final_metadata, args.output_dir)

if __name__ == "__main__":
    try:
        asyncio.run(run_app())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    except Exception as e:
        print(f"\nFatal error: {e}")