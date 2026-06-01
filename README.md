# stonks – Stock Analysis Tool

A complete pipeline to fetch NSE stock data, enrich with fundamentals, compute technical indicators, and rank stocks using a cross‑sectional scoring system.

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Installation](#installation)
- [Data Flow (Correct Order)](#data-flow-correct-order)
- [Quick Start](#quick-start)
- [Detailed Usage](#detailed-usage)
- [Configuration](#configuration)
- [Scoring System](#scoring-system)
- [Technical Indicators & Fundamentals](#technical-indicators--fundamentals)
- [Command Line Reference](#command-line-reference)
- [Example Walkthrough](#example-walkthrough)
- [License](#license)
- [Author](#author)

---

## Overview

`stonks` is a three‑stage tool for Indian (NSE) equity analysis:

1. **Enrichment** – Adds market cap, industry, sector, dependencies, and Yahoo Finance fundamentals to a watchlist (job file).
2. **Data Fetching** – Downloads daily OHLCV candles via Kite Connect (MCP) and copies the enriched metadata into each candle file.
3. **Analysis** – Computes technical indicators, fundamental scores, and cross‑sectional rankings.

The pipeline is designed to run **once** for fundamentals (they are stored in the job file) and **daily** for candle updates.  
When you refresh fundamentals (e.g., weekly), you can **update only the metadata** in existing candle files without re‑downloading candles.

---

## Features

- **Technical Indicators**: SMA, EMA, RSI, MACD, Bollinger Bands, ATR, ADX, Ichimoku, Fibonacci, Monthly Pivots, Rolling Returns, Volatility, Candlestick Patterns, Candle Score, Keltner Channel, TTM Squeeze.
- **Fundamental Metrics**: Trailing/Forward P/E, P/B, PEG, ROE, Profit Margin, Revenue/Earnings Growth, Beta, Dividend Yield, Payout Ratio, Market Cap, Analyst Recommendation, Target Upside, Earnings/Ex‑Dividend Dates, 52‑Week Position.
- **Cross‑Sectional Ranking**: Normalised factor‑based scoring with configurable technical/fundamental blend.
- **Incremental Updates**: Fetch only missing candles; skip already downloaded data.
- **Metadata‑Only Updates**: Refresh fundamentals in existing candle files without re‑fetching candles.
- **Crash‑Resistant Enrichment**: Saves after each symbol.
- **Parallel Loading**: Multi‑threaded stock loading for large datasets.
- **Coloured Terminal Output**: Readable with ANSI colour codes.

---

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/stonks.git
cd stonks

# (Optional) Create a virtual environment
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate      # Windows

# Install dependencies
pip install -r requirements.txt
```

**Note**: `kite.py` requires a Kite Connect API key and uses an interactive browser login (MCP). For unattended operation, modify the script to use an API key directly.

---

## Data Flow (Correct Order)

```
basic job file (dailyJobs.json)
         │
         ▼
    rich.py          ← adds NSE metadata + Yahoo Finance fundamentals (once or weekly)
         │
         ▼
 enriched job file (e.g., enriched.json)
         │
         ▼
    kite.py          ← initial fetch: downloads candles, copies metadata into each candle file
         │
         ▼
  candles/*.json     ← each contains both price data and full metadata
         │
         │  (later, after re‑running rich.py to refresh fundamentals)
         │
         ▼
    kite.py --update-metadata-only   ← updates metadata in existing candle files (no re‑fetch)
         │
         ▼
   stonks.py         ← analysis & ranking
```

**Key point**: `rich.py` only updates the **job file** – it never touches candle files.  
`kite.py` (with or without `--update-metadata-only`) propagates metadata from the job file into candle files.  
Thus you can refresh fundamentals without re‑downloading historical candles.

---

## Quick Start

```bash
# 1. Create a basic job file (just symbols)
cat > mywatchlist.json << EOF
{
  "default_days": 500,
  "watchlist": [
    { "symbol": "RELIANCE" },
    { "symbol": "TCS" },
    { "symbol": "HDFCBANK" }
  ]
}
EOF

# 2. Enrich with fundamentals and NSE metadata
python rich.py mywatchlist.json enriched.json

# 3. Fetch historical candles (interactive login)
python kite.py --job enriched.json --output-dir candles --days 500

# 4. Analyse and rank
python stonks.py candles/ -r --tech-weight 0.85 --fund-weight 0.15
```

---

## Detailed Usage

### 1. Prepare a basic job file

The job file is a JSON with a `watchlist` array. Each entry must have a `symbol` key. Optional `metadata` can be pre‑filled (e.g., `capital`, `sector`), but `rich.py` will overwrite or supplement it.

```json
{
  "default_days": 2000,
  "watchlist": [
    { "symbol": "RELIANCE" },
    { "symbol": "TATACONSUM" }
  ]
}
```

### 2. Enrich with fundamentals and NSE metadata (`rich.py`)

```bash
python rich.py input_job.json output_job.json
```

- Reads the watchlist from `input_job.json`.
- Downloads NSE constituent CSVs (if missing or older than 7 days) from `niftyindices.com`.
- Adds `capital` (LARGE/MID/SMALL), `industry`, `sector`, `dependencies` using the CSV data.
- Fetches **fresh** fundamentals from Yahoo Finance for each symbol (no caching by default).
- Saves incrementally to `output_job.json` after each symbol (crash‑resistant).

> **Important**: This script does **not** require existing candle data. It only modifies the job file. Run it once when you create a watchlist, and re‑run periodically (e.g., weekly) to refresh fundamentals.

### 3. Fetch historical candles (`kite.py`)

```bash
python kite.py --job enriched.json --output-dir candles [--days N] [--update] [--force-download]
```

- Reads the enriched job file.
- For each symbol, looks up the instrument token (using fast CSV lookup or API).
- Downloads daily candles from Kite MCP for the requested number of days.
- **Copies** the metadata from the job file into the candle JSON file (under `"metadata"`).
- Saves one file per symbol inside `--output-dir`.

**Options**:

- `--days` : Number of trading days to fetch (default from job’s `default_days` or 2000).
- `--update` : Fetch only missing days since the last saved candle (much faster).
- `--force-download` : Re‑download everything, ignoring existing files.
- `--delay` : Seconds between API calls (default 1.0 – respect rate limits).

The output file structure (e.g., `candles/RELIANCE.json`):

```json
{
  "metadata": { ... },   // full metadata from enriched job
  "data": [
    { "date": "2025-01-02 00:00:00", "open": ..., "high": ..., ... },
    ...
  ]
}
```

### 4. Update only fundamentals in existing candle files (`kite.py --update-metadata-only`)

After you re‑run `rich.py` to refresh fundamentals (e.g., new P/E ratios, analyst ratings), you have an **updated job file** with fresh `metadata.fundamentals`.  
Instead of re‑downloading all candles, you can **update only the metadata** in your existing candle files using:

```bash
python kite.py --job updated_enriched.json --output-dir candles --update-metadata-only
```

- Reads the **new enriched job file**.
- For each symbol, opens the existing candle JSON file in `--output-dir`.
- Replaces its `metadata` with the latest metadata from the job file.
- **Does not** call the Kite API or modify candle `data`.
- Saves the file back.

This is extremely fast (no network delays) and preserves all historical price data.  
After this step, `stonks.py` will see the updated fundamentals when you analyse.

### 5. Analyse and rank (`stonks.py`)

```bash
python stonks.py candles/ [-r] [--tech-weight T] [--fund-weight F]
```

- `candles/` can be a directory of JSON files (as produced by `kite.py`) or a single JSON file.
- Without `-r` (ranking), shows two tables: technical indicators summary and fundamentals summary.
- With `-r` : cross‑sectional ranking with normalised scores.

**Ranking weights**:

- `--tech-weight` : weight for technical composite (default 0.85)
- `--fund-weight` : weight for fundamental composite (default 0.15)

The two weights **must sum to 1.0** (the script normalises them internally).

### Daily & Weekly Routine

- **Daily** (to get new candles):
  ```bash
  python kite.py --job enriched.json --output-dir candles --update
  ```

- **Weekly** (to refresh fundamentals, then update metadata in candle files):
  ```bash
  python rich.py dailyJobs.json enriched.json   # fresh Yahoo data
  python kite.py --job enriched.json --output-dir candles --update-metadata-only
  ```

No need to re‑fetch candles – the `--update-metadata-only` flag propagates the new fundamentals instantly.

---

## Configuration

### NSE constituent CSV files

`rich.py` automatically downloads the following files from `niftyindices.com` if missing or older than 7 days:

- `ind_nifty100list.csv`
- `ind_niftymidcap150list.csv`
- `ind_NiftySmallcap500_list.csv`

The download uses proper HTTP headers to avoid being blocked. You can also place these files manually in the working directory.

### Trading holidays

`kite.py` reads holidays from a `holidays.json` file (if present) or uses a built‑in set for 2026.  
Format of `holidays.json`:

```json
["2026-01-26", "2026-03-03", ...]
```

Dates must be in `YYYY-MM-DD` format.

### Market hours

Hardcoded in `kite.py`:

- Market open: 09:00 (pre‑open starts)
- Market close: 16:00

Modify `MARKET_OPEN` and `MARKET_CLOSE` if needed.

---

## Scoring System

The ranking engine uses a **two‑pass cross‑sectional normalisation**:

1. **Raw factor scores** are calculated for each stock (momentum, trend, RSI quality, Sharpe, etc.).
2. **Min‑max normalisation** across the universe turns each factor into a 0‑100 score.
3. **Weighted composite** (technical) = sum of factor weights × normalised scores.
4. **Bonus computer** adds candlestick, Ichimoku, peak‑proximity, and TTM squeeze adjustments.
5. **Fundamental composite** is built from sub‑categories (valuation, quality, growth, analyst, etc.).
6. **Final overall score** = `(1 - fund_weight) * ta_composite + fund_weight * fa_composite`.

The default factor weights and fundamental sub‑weights are documented inside `stonks.py`. You can override them via the API or by editing the script.

For a detailed explanation of each factor, see the **Scoring System** section in the source code comments or the original README (available on GitHub).

---

## Technical Indicators & Fundamentals

**Technical indicators** (auto‑registered):

- SMA, EMA, RSI, MACD, Bollinger Bands, ATR, MFI, ADX, Ichimoku, Fibonacci, Monthly Pivot, Rolling Return, Annualised Volatility, Candle Patterns, Candle Score, Keltner Channel, TTM Squeeze.

**Fundamental metrics** (auto‑registered):

- TrailingPE, ForwardPE, P/B, PEG, ROE, ProfitMargin, RevenueGrowth, EarningsGrowth, Beta, DividendYield, PayoutRatio, MarketCap, AnalystRec, TargetUpside, EarningsDate, ExDividendDate, 52Week.

Each indicator/fundamental provides a `classify()` method returning a verdict, a 0‑100 score, and a colour.

---

## Command Line Reference

### `rich.py`
```
usage: python rich.py input.json output.json
```
Adds NSE metadata and Yahoo fundamentals to a job file.

### `kite.py`
```
usage: python kite.py --job JOB_FILE [--output-dir DIR] [--days N] [--update] [--force-download] [--update-metadata-only] [--delay SEC]
```
- `--job` : enriched job file (required).
- `--output-dir` : directory for candle JSON files (default `.`).
- `--days` : number of trading days to fetch.
- `--update` : incremental candle fetch (only missing days).
- `--force-download` : re‑fetch all candles (ignore existing).
- `--update-metadata-only` : **do not fetch candles**; only update metadata in existing candle files from the job file.
- `--delay` : seconds between API calls (default 1.0).

### `stonks.py`
```
usage: python stonks.py PATH [-r] [--tech-weight T] [--fund-weight F]
```
- `PATH` : directory of candle JSONs or a single JSON file.
- `-r` : show cross‑sectional ranking.
- `--tech-weight` : weight for technical composite (default 0.85).
- `--fund-weight` : weight for fundamental composite (default 0.15).

---

## Example Walkthrough

Below is a complete run using a sample watchlist (`sampleBasicJob.json`) containing `RELIANCE`, `HDFCBANK`, `TATACONSUM`, `ITC`, `INDIA VIX`, and `NIFTY 50`.

### 1. Enrich the job file

```bash
python rich.py sampleBasicJob.json sampleEnrichedJob.json
```

Output:
```
📊 Loading NSE CSV metadata...
  Fetching ind_nifty100list.csv from niftyindices.com...
  Downloaded ind_nifty100list.csv
  Fetching ind_niftymidcap150list.csv from niftyindices.com...
  Downloaded ind_niftymidcap150list.csv
  Fetching ind_NiftySmallcap500_list.csv from niftyindices.com...
  Downloaded ind_NiftySmallcap500_list.csv
   Found 754 symbols in CSVs
   Metadata saved.

📈 Fetching Yahoo fundamentals for 6 entries...
[1/6] RELIANCE.NS ✓
[2/6] HDFCBANK.NS ✓
[3/6] TATACONSUM.NS ✓
[4/6] ITC.NS ✓
[5/6] ^INDIAVIX ↪ index, skipping
[6/6] ^NSEI ↪ index, skipping

✅ Full enrichment complete. Output: sampleEnrichedJob.json
```

### 2. Fetch historical candles (interactive Kite login)

```bash
python kite.py --job sampleEnrichedJob.json --output-dir candles --days 2000
```

After logging in the browser and pressing Enter, the candles are downloaded:
```
[1/6] RELIANCE...
[2/6] HDFCBANK...
[3/6] TATACONSUM...
[4/6] ITC...
[5/6] INDIA VIX...
[6/6] NIFTY 50...
```

### 3. Show summary tables (no ranking)

```bash
python stonks.py candles/
```

This produces two tables. The technical indicators table (truncated):

```
Symbol     | Close     | SMA(20)   | RSI(14) | MACD Line | MACD Signal | MACD Hist | BB Upper  | BB Lower  | ...
-----------+-----------+-----------+---------+-----------+-------------+-----------+-----------+-----------+---
HDFCBANK   | 744.550   | 769.155   | 39.086  | -8.162    | -8.429      | 0.266     | 796.959   | 741.351   | ...
ITC        | 286.900   | 298.308   | 35.825  | -1.503    | -0.169      | -1.334    | 307.532   | 289.083   | ...
RELIANCE   | 1321.200  | 1379.670  | 39.312  | -11.090   | -6.973      | -4.117    | 1470.564  | 1288.776  | ...
...
```

And the fundamentals table:

```
Symbol     | Trailing P/E | Forward P/E | P/B       | PEG  | ROE % | Profit Margin % | Rev Growth % | Earn Growth % | ...
-----------+--------------+-------------+-----------+------+-------+-----------------+--------------+---------------+---
HDFCBANK   | 16.628736    | 11.860803   | 1.9576683 | 0.89 | 13.8% | +0.27%          | -1.8%        | 0.075         | ...
ITC        | 17.175756    | 15.948882   | 4.8975215 | 1.78 | 29.3% | +0.26%          | -5.0%        | -0.727        | ...
RELIANCE   | 22.243805    | 18.482237   | 1.9886377 | 0.82 | 9.1%  | +0.08%          | +12.5%       | -0.126        | ...
...
```

### 4. Cross‑sectional ranking

```bash
python stonks.py candles/ -r --tech-weight 0.85 --fund-weight 0.15
```

```txt
Gate Flags:
  M = Momentum (20d return > 0)
  T = Trend (price > SMA20 or oversold recovery)
  A = ADX (ADX > 20 or RSI < 30)
  F = MFI (MFI <= 80)
  I = Ichimoku (price above cloud)
  B = BB/TTM Squeeze (volatility compression)
  ✓ = passed  ✗ = failed

--+------------+-------+------+------+---+---+---+---+---+--
# | Symbol     | Score | TA   | FA   | M | T | A | F | I | B |
--+------------+-------+------+------+---+---+---+---+---+--
1 | HDFCBANK   | 73.2  | 70.8 | 86.4 | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ |
2 | TATACONSUM | 57.5  | 58.4 | 52.7 | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
3 | ITC        | 53.0  | 49.4 | 73.2 | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ |
4 | RELIANCE   | 50.4  | 47.2 | 68.5 | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ |
5 | NIFTY 50   | 49.5  | 58.3 | -    | ✗ | ✓ | ✗ | ✗ | ✗ | ✗ |
6 | INDIA VIX  | 33.9  | 39.9 | -    | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
--+------------+-------+------+------+---+---+---+---+---+--
```

### 5. Full report for a single stock

```bash
python stonks.py candles/HDFCBANK.json
```

This prints a detailed report with business summary, technical indicators (with raw values and verdicts), and fundamental analysis (with scores). Example excerpt:

```txt
========================================================================
  Full Report: HDFCBANK
========================================================================
  Date   : 2026-05-29   Close  : 744.550   Volume : 101,299,431

  Business Summary:
                        HDFC Bank Limited provides banking and financial products...

  Technical Indicators:
  SMA(20)        : pct=-3.20                                 (S:53) ▼ -3.2% below
  RSI(14)        : rsi=39.09                                 (S:86) oversold recovery (39)
  MACD           : macd=-8.16  signal=-8.43  histogram=0.27  (S:47) bullish hist=0.2665...
  ...
  Fundamental Analysis:
  TrailingPE              16.63                                     reasonable (PE=16.6)            score  67
  ForwardPE               11.86                                     cheap (PE=11.9)                 score  74
  AnalystRec              (1.15385, 'strong_buy')                   STRONG BUY                      score  96
  ...
========================================================================
```

---

## License

This project is licensed under the GNU General Public License v3.0 – see the [LICENSE](LICENSE) file for details.

- You may freely use and modify this software for personal purposes.
- Redistribution or derivative works must also be licensed under GPL‑3.0.
- Commercial resale of this software as a standalone product is prohibited.

---

## Author

**Edmund Carvalho**  
[GitHub](https://github.com/edmund-carvalho)

*For questions or contributions, please open an issue on the repository.*