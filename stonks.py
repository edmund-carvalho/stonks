#!/usr/bin/env python3
"""
stonks - Stock Analysis Tool
Copyright (C) 2025 Edmund Carvalho

A comprehensive stock analysis tool with lazy indicator computation,
parallel processing, weighted composite scoring, and clean table rendering.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY
without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

Architecture:
    Candle -> Stock -> Indicators/Fundamentals -> Scoring Factors -> Ranking

Key Design Decisions:
    - All indicators and fundamentals are auto-registered via __init_subclass__
    - Indicator results are lazily computed and cached per Stock instance
    - Cross-sectional ranking uses percentile-rank normalisation across the universe
    - Scoring is continuous (no quantized tiers) for better differentiation
    - Market cap adjustments are applied to volatility-sensitive indicators


Code Layout :
    1. Candle          (no dependencies)
    2. BaseTechnicalIndicator   (depends on Candle conceptually)
    3. All Indicators  (depend on BaseTechnicalIndicator)
    4. BaseFundamentalIndicator (no dependencies)
    5. All Fundamentals (depend on BaseFundamentalIndicator)
    6. Stock           (depends on Indicators + Fundamentals)
    7. StockFactory    (depends on Stock)
    8. Tables/Scoring  (depend on Stock)
    9. Stonks/CLI      (depends on everything)
"""

from __future__ import annotations
import argparse
import json
import logging
import math
import os
import re
import textwrap
import tomllib
from abc import ABC, abstractmethod
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type, TypedDict

logger = logging.getLogger("stonks")

_STONKS_BANNER = """
  /₹₹₹₹₹₹  /₹₹₹₹₹₹₹₹ /₹₹₹₹₹₹  /₹₹   /₹₹ /₹₹   /₹₹  /₹₹₹₹₹₹ 
 /₹₹__  ₹₹|__  ₹₹__//₹₹__  ₹₹| ₹₹₹ | ₹₹| ₹₹  /₹₹/ /₹₹__  ₹₹
| ₹₹  \\__/   | ₹₹  | ₹₹  \\ ₹₹| ₹₹₹₹| ₹₹| ₹₹ /₹₹/ | ₹₹  \\__/
|  ₹₹₹₹₹₹    | ₹₹  | ₹₹  | ₹₹| ₹₹ ₹₹ ₹₹| ₹₹₹₹₹/  |  ₹₹₹₹₹₹ 
 \\____  ₹₹   | ₹₹  | ₹₹  | ₹₹| ₹₹  ₹₹₹₹| ₹₹  ₹₹   \\____  ₹₹
 /₹₹  \\ ₹₹   | ₹₹  | ₹₹  | ₹₹| ₹₹\\  ₹₹₹| ₹₹\\  ₹₹  /₹₹  \\ ₹₹
|  ₹₹₹₹₹₹/   | ₹₹  |  ₹₹₹₹₹₹/| ₹₹ \\  ₹₹| ₹₹ \\  ₹₹|  ₹₹₹₹₹₹/
 \\______/    |__/   \\______/ |__/  \\__/|__/  \\__/ \\______/ 
"""

# =============================================================================
# ANSI COLOR CODES
# =============================================================================
class CLR:
    """ANSI color codes for terminal output."""
    G  = '\033[92m'   # green  - bullish / positive
    R  = '\033[91m'   # red    - bearish / negative
    Y  = '\033[93m'   # yellow - neutral / caution
    B  = '\033[94m'   # blue
    M  = '\033[95m'   # magenta - alerts (squeeze fired)
    CY = '\033[96m'   # cyan   - informational
    W  = '\033[97m'   # white
    BD = '\033[1m'    # bold
    DM = '\033[2m'    # dim
    E  = '\033[0m'    # reset

    @classmethod
    def disable(cls):
        """Blank out every color code (for --no-color / NO_COLOR / test assertions)."""
        for attr in ("G", "R", "Y", "B", "M", "CY", "W", "BD", "DM", "E"):
            setattr(cls, attr, "")


# =============================================================================
# TYPE DEFINITIONS
# =============================================================================
class Verdict(TypedDict):
    """Standard return type for all classify() methods."""
    verdict: str           # human-readable classification
    score:   int           # 0-100 (higher = better / more bullish)
    color:   str           # CLR code for terminal display
    result:  Dict[str, Any]  # extra computed data for display


class PreComputeMode(Enum):
    """Controls which indicator types are pre-computed during loading."""
    PCM_ALL         = "all"          # precompute everything
    PCM_TECHNICAL   = "technical"    # only technical indicators
    PCM_FUNDAMENTAL = "fundamental"  # only fundamental data
    PCM_NONE        = "none"         # skip all precomputation


# =============================================================================
# CONSTANTS
# =============================================================================
HIST_WINDOW = 252  # trading days (~1 year) for history-normalised indicators


# =============================================================================
# DEBUG LOGGING FOR SWALLOWED EXCEPTIONS
# =============================================================================
# Several call sites (Stock.precompute, factor scoring in rank_stocks_xnorm,
# _fund_sub_scores, table renderers) deliberately catch broad exceptions so
# one bad indicator/symbol doesn't crash a whole run - but that also means a
# typo'd indicator name or a genuine bug silently degrades to "N/A"/neutral
# scores with no visible trace. --debug (see parse_args/main) raises this
# logger to DEBUG and adds a stderr handler; without it, nothing is printed
# (Python's logging module is silent below WARNING by default).
_LOGGED_SWALLOWED: set = set()


def _log_swallowed(site: str, symbol: str, exc: Exception) -> None:
    """Log a caught-and-ignored exception once per (site, symbol) - the
    same failure would otherwise repeat every time this site runs."""
    key = (site, symbol)
    if key in _LOGGED_SWALLOWED:
        return
    _LOGGED_SWALLOWED.add(key)
    logger.debug("swallowed %s in %s for %s: %s", type(exc).__name__, site, symbol, exc)


# =============================================================================
# 1.  CANDLE - Single OHLCV data point
# =============================================================================
class Candle:
    """Single OHLCV candlestick with optional open interest."""

    def __init__(self, symbol: str, timestamp: datetime,
                 open: float, high: float, low: float,
                 close: float, volume: float,
                 open_interest: Optional[float] = None):
        self.symbol = symbol
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.open_interest = open_interest

    def __repr__(self):
        oi = f", OI={self.open_interest:.0f}" if self.open_interest else ""
        return (f"Candle({self.symbol!r}, {self.timestamp.isoformat()}, "
                f"O={self.open:.3f} H={self.high:.3f} L={self.low:.3f} "
                f"C={self.close:.3f} V={self.volume:.0f}{oi})")


def _norm_index(index: int, n: int) -> int:
    """
    Normalize a possibly-negative index against a sequence of length n.

    Returns the equivalent non-negative index for valid inputs
    (index in [-n, n-1]). For invalid inputs (out of that range in either
    direction) the result is still out of [0, n) - callers must still
    bounds-check the result (0 <= result < n) before indexing with it,
    exactly as they already bounds-check plain positive indices. This
    just centralizes the "n + index" arithmetic so every classify()
    handles negative indices the same way instead of some relying on
    Python's native negative-indexing (which silently mis-wraps once
    |index| > n) and others reimplementing the same one-liner.
    """
    return index if index >= 0 else n + index


def rolling_min_max(values: List[Optional[float]], window: int, min_valid: int = 5
                     ) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """
    Rolling min/max of `values` over a trailing `window`-sized lookback
    ending at (and including) each index, skipping None entries. Returns
    (lo, hi) parallel lists; both stay None at an index until at least
    `min_valid` non-None values have appeared within that index's window -
    matching every caller's existing "not enough history yet" behaviour.

    O(n) via a monotonic deque (each index enters and leaves each internal
    deque at most once), replacing an O(n * window) per-bar list-slice-
    and-rescan that was independently copy-pasted in MACD, ATR, ADX, and
    AnnualizedVolatility's compute() methods. A None entry never enters
    the deques and never counts toward min_valid - it's invisible to the
    historical range, not a zero. Callers with their own "sentinel value"
    concept (e.g. ADX also excluding non-positive values) must translate
    that to None before calling; this helper only knows about None.
    """
    n = len(values)
    lo: List[Optional[float]] = [None] * n
    hi: List[Optional[float]] = [None] * n
    min_deque = deque()   # (index, value), increasing value order
    max_deque = deque()   # (index, value), decreasing value order
    valid_indices = deque()  # indices of non-None values currently in window

    for i, v in enumerate(values):
        start = i - window + 1

        while min_deque and min_deque[0][0] < start:
            min_deque.popleft()
        while max_deque and max_deque[0][0] < start:
            max_deque.popleft()
        while valid_indices and valid_indices[0] < start:
            valid_indices.popleft()

        if v is not None:
            while min_deque and min_deque[-1][1] >= v:
                min_deque.pop()
            min_deque.append((i, v))
            while max_deque and max_deque[-1][1] <= v:
                max_deque.pop()
            max_deque.append((i, v))
            valid_indices.append(i)

        if len(valid_indices) >= min_valid:
            lo[i] = min_deque[0][1]
            hi[i] = max_deque[0][1]

    return lo, hi


# =============================================================================
# 2.  INDICATOR BASE CLASS + AUTO-REGISTRATION
# =============================================================================
class BaseTechnicalIndicator(ABC):
    """
    Abstract base for all technical indicators.
    
    Subclasses are automatically registered in _registry via __init_subclass__.
    Each Stock instance creates one instance of every registered indicator.
    """
    _registry: List[Type["BaseTechnicalIndicator"]] = []   # class-level registry

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseTechnicalIndicator._registry.append(cls)       # auto-register

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name including parameters (e.g. 'SMA(20)')."""
        pass

    @abstractmethod
    def compute(self, stock: Stock) -> Any:
        """
        Calculate the indicator using the stock's data.
        May depend on other already-computed indicators via stock.get_indicator().
        Returns a list of values or a dict of lists.
        """
        pass

    @abstractmethod
    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        """
        Classify the indicator at a given candle index.
        Returns a Verdict with score (0-100), color, and descriptive text.
        """
        pass

    # Convenience accessors for common classification fields
    def get_verdict(self, stock: Stock, index: int = -1) -> str:
        """Return only the verdict string."""
        return self.classify(stock, index).get("verdict", "N/A")

    def get_score(self, stock: Stock, index: int = -1) -> int:
        """Return only the numeric score (0-100)."""
        return self.classify(stock, index).get("score", 0)

    def get_color(self, stock: Stock, index: int = -1) -> str:
        """Return only the ANSI colour code."""
        return self.classify(stock, index).get("color", CLR.DM)

    def get_result(self, stock: Stock, index: int = -1) -> Dict[str, Any]:
        """Return the extra result dict."""
        return self.classify(stock, index).get("result", {})


# =============================================================================
# 3.  CONCRETE TECHNICAL INDICATORS
# =============================================================================

class SMA(BaseTechnicalIndicator):
    """
    Simple Moving Average (SMA).

    Formula:
        SMA(t) = (1/n) * sum(price[t-n+1 : t+1])

    where n is the period.

    Reference: https://www.investopedia.com/terms/s/sma.asp
    """

    def __init__(self, period: int = 20):
        self.period = period

    @property
    def name(self):
        return f"SMA({self.period})"

    def compute(self, stock: Stock):
        closes = stock.closes
        n = len(closes)
        out = [None] * n
        for i in range(n):
            if i >= self.period - 1:
                out[i] = sum(closes[i - self.period + 1 : i + 1]) / self.period
        return out

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        series = stock.get_indicator(self.name)
        idx = _norm_index(index, len(series))
        if idx < 0 or idx >= len(series) or series[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        px = stock.closes[idx]
        ma = series[idx]
        pct = (px / ma - 1) * 100

        # Get ATR for volatility scaling
        try:
            atr_data = stock.get_indicator("ATR(14)")
            atr_vals = atr_data["atr_vals"] if isinstance(atr_data, dict) else atr_data
            atr = atr_vals[idx] if 0 <= idx < len(atr_vals) and atr_vals[idx] else None
        except (KeyError, IndexError, TypeError):
            atr = None
        
        if atr and ma > 0:
            vol = (atr / ma) * 100
            if vol > 0:
                z_score = pct / vol
                # tanh squashes extreme values: z=0→0, z=2→0.96, z=5→1.0
                score = 50.0 + math.tanh(z_score * 0.8) * 40.0
            else:
                score = 50.0
        else:
            score = 50.0

        score = max(10, min(90, int(round(score))))
        
        if pct > 0:
            verdict = f"▲ {pct:+.1f}% above"
            color = CLR.G
        else:
            verdict = f"▼ {pct:+.1f}% below"
            color = CLR.R
        return {"verdict": verdict, "score": score, "color": color, "result": {"pct": pct}}


class EMA(BaseTechnicalIndicator):
    """
    Exponential Moving Average (EMA).

    Uses the standard EMA formula with Wilder's smoothing constant:
        k = 2 / (period + 1)
        EMA(t) = price(t) * k + EMA(t-1) * (1 - k)

    The initial value is seeded as the SMA over the first 'period' bars.

    Reference: https://www.investopedia.com/terms/e/ema.asp
    """

    def __init__(self, period: int = 20):
        self.period = period

    @property
    def name(self):
        return f"EMA({self.period})"

    def compute(self, stock: Stock):
        prices = stock.closes
        n = len(prices)
        out = [None] * n
        if n < self.period:
            return out
        k = 2.0 / (self.period + 1)
        prev = sum(prices[:self.period]) / self.period
        out[self.period - 1] = prev
        for i in range(self.period, n):
            prev = prices[i] * k + prev * (1.0 - k)
            out[i] = prev
        return out

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        series = stock.get_indicator(self.name)
        idx = _norm_index(index, len(series))
        if idx < 0 or idx >= len(series) or series[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        px = stock.closes[idx]
        ma = series[idx]
        pct = (px / ma - 1) * 100
        if pct > 0:
            verdict = f"▲ {pct:+.1f}% above"
            score = int(min(90, 60 + abs(pct) * 2))
            color = CLR.G
        else:
            verdict = f"▼ {pct:+.1f}% below"
            score = int(max(10, 60 - abs(pct) * 2))
            color = CLR.R
        return {"verdict": verdict, "score": score, "color": color, "result": {"pct": pct}}


class RSI(BaseTechnicalIndicator):
    """
    Relative Strength Index (RSI) - Wilder's smoothed version.

    Calculation steps:
        1. Compute upward and downward price changes:
           gain = max(close(t) - close(t-1), 0)
           loss = max(close(t-1) - close(t), 0)
        2. First average gain/loss = simple average over 'period' bars.
        3. Subsequent averages use Wilder's smoothing:
           avg_gain(t) = (avg_gain(t-1) * (period-1) + gain(t)) / period
           avg_loss(t) = (avg_loss(t-1) * (period-1) + loss(t)) / period
        4. RS = avg_gain / avg_loss
        5. RSI = 100 - (100 / (1 + RS))

    The classification uses market-cap-adjusted sweet-spot thresholds
    because small-cap stocks naturally have wider RSI swings than large caps.

    Reference: J. Welles Wilder Jr., "New Concepts in Technical Trading Systems" (1978)
    """
    def __init__(self, period: int = 14):
        self.period = period

    @property
    def name(self):
        return f"RSI({self.period})"

    def compute(self, stock: Stock):
        prices = stock.closes
        n = len(prices)
        out = [None] * n
        if n <= self.period:
            return out
        gains = [max(prices[i] - prices[i-1], 0) for i in range(1, n)]
        losses = [max(prices[i-1] - prices[i], 0) for i in range(1, n)]
        ag = sum(gains[:self.period]) / self.period
        al = sum(losses[:self.period]) / self.period
        out[self.period] = 100.0 - 100.0 / (1.0 + ag / al) if al else 100.0
        for j in range(self.period, n - 1):
            ag = (ag * (self.period - 1) + gains[j]) / self.period
            al = (al * (self.period - 1) + losses[j]) / self.period
            out[j + 1] = 100.0 - 100.0 / (1.0 + ag / al) if al else 100.0
        return out

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        """Continuous RSI classification with market-cap-adjusted thresholds."""
        series = stock.get_indicator(self.name)
        idx = _norm_index(index, len(series))
        if idx < 0 or idx >= len(series) or series[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        rsi = series[idx]
        
        # Market cap adjusted sweet spot
        cap = stock.metadata.get("capital", "").upper()
        if "SMALL" in cap:
            sweet_mid, steepness = 50, 0.12
        elif "LARGE" in cap:
            sweet_mid, steepness = 45, 0.15
        else:
            sweet_mid, steepness = 47, 0.13

        score = int(round(100.0 / (1.0 + math.exp((rsi - sweet_mid) * steepness))))
        
        # Verdict label based on RSI zones
        if rsi < 30:
            verdict = f"OVERSOLD ({rsi:.0f})"
            color = CLR.G
        elif rsi < 45:
            verdict = f"value zone ({rsi:.0f})"
            color = CLR.G
        elif rsi <= 55:
            verdict = f"sweet-spot ({rsi:.0f})"
            color = CLR.CY
        elif rsi <= 70:
            verdict = f"elevated ({rsi:.0f})"
            color = CLR.Y
        elif rsi <= 80:
            verdict = f"OVERBOUGHT ({rsi:.0f})"
            color = CLR.R
        else:
            verdict = f"extreme ({rsi:.0f})"
            color = CLR.R
        
        return {"verdict": verdict, "score": score, "color": color, "result": {"rsi": rsi}}


class MACD(BaseTechnicalIndicator):
    """
    Moving Average Convergence Divergence (MACD).

    Algorithm:
        MACD Line  = EMA(close, 12) - EMA(close, 26)
        Signal Line = EMA(MACD Line, 9)
        Histogram   = MACD Line - Signal Line

    The classification is history-normalised: the current histogram value
    is expressed as a percentile within its own 1-year (252-day) range.
    A crossover bonus/penalty of ±10 is applied when the histogram
    changes sign (signal line crossover).

    Reference: Gerald Appel, "The Moving Average Convergence-Divergence Trading Method" (1979)

    Moving Average Convergence Divergence.
    Uses EMA(12) - EMA(26) with 9-period signal line.
    Classification is history-normalised via 1-year histogram range.
    """
    @property
    def name(self):
        return "MACD"

    def compute(self, stock: Stock):
        closes = stock.closes
        n = len(closes)
        macd_line = [None] * n
        signal = [None] * n
        histogram = [None] * n
        hist_lo = [None] * n
        hist_hi = [None] * n

        if n < 26:
            return {"macd": macd_line, "signal": signal, "histogram": histogram,
                    "hist_lo": hist_lo, "hist_hi": hist_hi}

        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)

        for i in range(n):
            if ema12[i] is not None and ema26[i] is not None:
                macd_line[i] = ema12[i] - ema26[i]

        # Signal = EMA(9) of MACD line
        sig_ema = self._ema_of_list(macd_line, 9)
        for i in range(n):
            if sig_ema[i] is not None:
                signal[i] = sig_ema[i]
                histogram[i] = macd_line[i] - signal[i]
        
        # Rolling hist stats per bar (no look-ahead) - see rolling_min_max().
        hist_lo, hist_hi = rolling_min_max(histogram, HIST_WINDOW)

        return {"macd": macd_line, "signal": signal, "histogram": histogram,
                "hist_lo": hist_lo, "hist_hi": hist_hi}

    # =============================================================================
    # MACD.classify  -  histogram position relative to its own 1-year range
    # =============================================================================
    # Directional rule:
    #   histogram > 0  = bullish  → high pct → high score
    #   histogram < 0  = bearish  → low pct  → low score
    #   cross (sign change vs prev bar) → bonus / penalty
    # =============================================================================
    def classify(self, stock, index: int = -1):
        """
        History-normalised MACD classification.
        Score = percentile of current histogram within 1-year range.
        Crossover bonus/penalty of +/-10 applied for signal line crosses.
        """
        data = stock.get_indicator(self.name)
        hist_series = data["histogram"]
        n   = len(hist_series)
        idx = _norm_index(index, n)

        if idx < 0 or idx >= n or hist_series[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}

        hist     = hist_series[idx]
        macd_val = data["macd"][idx]
        sig_val  = data["signal"][idx]

        # Use per-bar hist stats (no look-ahead)
        lo  = data["hist_lo"][idx]
        hi  = data["hist_hi"][idx]
        cur = hist_series[idx]
        hist_pct = (((cur - lo) / (hi - lo) * 100.0)
                    if (lo is not None and hi is not None
                        and hi - lo > 1e-9 and cur is not None)
                    else None)

        if hist_pct is None:
            # fall back to fixed-threshold score
            if hist > 0:
                score   = min(90, 60 + hist * 10)
                verdict = f"bullish (hist {hist:.4f})"
                color   = CLR.G
            elif hist < 0:
                score   = max(10, 60 + hist * 10)
                verdict = f"bearish (hist {hist:.4f})"
                color   = CLR.R
            else:
                score   = 50
                verdict = "neutral"
                color   = CLR.Y
            return {"verdict": verdict, "score": int(score), "color": color,
                    "result": {"macd": macd_val, "signal": sig_val, "histogram": hist}}

        # hist_pct: 0 = at historical low (most bearish), 100 = historical high (most bullish)
        score = hist_pct   # already 0-100

        # Bonus/penalty for histogram crossover (sign change)
        prev_hist = hist_series[idx - 1] if idx >= 1 else 0
        crossed_up   = hist > 0 and (prev_hist is None or prev_hist <= 0)
        crossed_down = hist < 0 and (prev_hist is None or prev_hist >= 0)
        if crossed_up:
            score = min(100, score + 10)
        elif crossed_down:
            score = max(0,   score - 10)

        # Colour by direction
        if   hist > 0:
            color = CLR.G
            direction = "bullish"
        elif hist < 0:
            color = CLR.R
            direction = "bearish"
        else:
            color = CLR.Y
            direction = "neutral"

        verdict = (f"{direction} hist={hist:.4f}  "
                f"({hist_pct:.0f}th pctile of 1yr range "
                f"{lo:.4f}→{hi:.4f})")

        return {
            "verdict": verdict,
            "score":   int(round(score)),
            "color":   color,
            "result":  {
                "macd": macd_val, "signal": sig_val, "histogram": hist,
                "hist_lo": lo, "hist_hi": hi, "hist_pct": hist_pct,
            },
        }

    @staticmethod
    def _ema(data: List[float], period: int) -> List[Optional[float]]:
        """Exponential Moving Average helper."""
        n = len(data)
        out = [None] * n
        if n < period:
            return out
        k = 2.0 / (period + 1)
        prev = sum(data[:period]) / period
        out[period - 1] = prev
        for i in range(period, n):
            prev = data[i] * k + prev * (1.0 - k)
            out[i] = prev
        return out

    @staticmethod
    def _ema_of_list(values: List[float], period: int) -> List[Optional[float]]:
        """EMA of a list that may contain None values."""
        n = len(values)
        out = [None] * n
        start = None
        count = 0
        for i, v in enumerate(values):
            if v is not None:
                if start is None: start = i
                count += 1
                if count == period: break
        if count < period:
            return out
        sub = [v for v in values[:start + period] if v is not None]
        ema_val = sum(sub) / len(sub)
        idx = start + period - 1
        out[idx] = ema_val
        k = 2.0 / (period + 1)
        for i in range(idx + 1, n):
            if values[i] is not None:
                ema_val = values[i] * k + ema_val * (1.0 - k)
            out[i] = ema_val
        return out


class BollingerBands(BaseTechnicalIndicator):
    """
    Bollinger Bands.

    Calculated as:
        Middle Band = SMA(close, period)
        Upper Band  = Middle Band + (stddev * standard deviation)
        Lower Band  = Middle Band - (stddev * standard deviation)

    The standard deviation is computed over the same 'period' window.
    Classification uses %B (position within the bands):
        %B = (price - lower) / (upper - lower)
    with continuous, inverted scoring (lower %B = better entry).

    Reference: John Bollinger, "Bollinger on Bollinger Bands" (2002)
    """
    def __init__(self, period: int = 20, stddev: float = 2.0):
        self.period = period
        self.stddev = stddev

    @property
    def name(self):
        return f"BB({self.period},{self.stddev})"

    def compute(self, stock: Stock):
        closes = stock.closes
        n = len(closes)
        upper = [None] * n
        middle = [None] * n
        lower = [None] * n
        for i in range(n):
            if i >= self.period - 1:
                window = closes[i - self.period + 1 : i + 1]
                mid = sum(window) / self.period
                std = math.sqrt(sum((x - mid) ** 2 for x in window) / self.period)
                upper[i] = mid + self.stddev * std
                middle[i] = mid
                lower[i] = mid - self.stddev * std
            else:
                upper[i] = middle[i] = lower[i] = closes[i]
        return {"upper": upper, "middle": middle, "lower": lower}

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        """Continuous %B classification: 0% = lower band (best entry), 100% = upper band."""
        bb = stock.get_indicator(self.name)
        idx = _norm_index(index, len(bb["upper"]))
        if idx < 0 or idx >= len(bb["upper"]) or bb["upper"][idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        px = stock.closes[idx]
        up = bb["upper"][idx]
        lo = bb["lower"][idx]
        mid = bb["middle"][idx]
        
        # Continuous %B scoring: 0% = at lower band, 100% = at upper band
        if up != lo:
            pct_b = (px - lo) / (up - lo) * 100
        else:
            pct_b = 50
        
        # Inverted: lower %B = better entry (near support)
        if pct_b <= 0:
            score = 90.0 + min(10, abs(pct_b))  # Below lower band: 90-100
            verdict = f"BELOW lower - oversold ({pct_b:.0f}%B)"
            color = CLR.G
        elif pct_b <= 20:
            score = 80.0 + ((20 - pct_b) / 20) * 10  # Near lower: 80-90
            verdict = f"near lower band ({pct_b:.0f}%B)"
            color = CLR.G
        elif pct_b <= 50:
            score = 60.0 + ((50 - pct_b) / 30) * 20  # Lower half: 60-80
            verdict = f"inside bands ({pct_b:.0f}%B)"
            color = CLR.Y
        elif pct_b <= 80:
            score = 40.0 + ((80 - pct_b) / 30) * 20  # Upper half: 40-60
            verdict = f"inside bands ({pct_b:.0f}%B)"
            color = CLR.Y
        elif pct_b <= 100:
            score = 20.0 + ((100 - pct_b) / 20) * 20  # Near upper: 20-40
            verdict = f"near upper band ({pct_b:.0f}%B)"
            color = CLR.R
        else:
            score = max(0, 20.0 - (pct_b - 100) * 2)  # Above upper: 0-20
            verdict = f"ABOVE upper - overbought ({pct_b:.0f}%B)"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color,
                "result": {"upper": up, "lower": lo, "middle": mid, "position_pct": pct_b}}


class ATR(BaseTechnicalIndicator):
    """
    Average True Range (ATR) - Wilder's smoothed version.

    True Range (TR) is the greatest of:
        - Current high minus current low
        - Absolute value of (current high minus previous close)
        - Absolute value of (current low minus previous close)

    First ATR value = simple average of TR over 'period' bars.
    Subsequent values use Wilder's smoothing:
        ATR(t) = (ATR(t-1) * (period-1) + TR(t)) / period

    Classification is history-normalised via ATR% (ATR / close * 100),
    expressed as a percentile within a 1-year range. Score is inverted:
    low ATR% relative to history = high score (entry-friendly).

    Reference: J. Welles Wilder Jr., "New Concepts in Technical Trading Systems" (1978)
    """
    def __init__(self, period: int = 14):
        self.period = period

    @property
    def name(self):
        return f"ATR({self.period})"

    def compute(self, stock: Stock):
        highs, lows, closes = stock.highs, stock.lows, stock.closes
        n = len(closes)
        out = [None] * n
        if n <= self.period:
            return {"atr_vals": out, "atr_pct": [],
                    "hist_lo": [None] * n, "hist_hi": [None] * n}
        
        # Calculate True Range
        tr = []
        for i in range(1, n):
            tr.append(max(highs[i] - lows[i],
                        abs(highs[i] - closes[i-1]),
                        abs(lows[i] - closes[i-1])))
        
        # Calculate ATR (Wilder's smoothing)
        val = sum(tr[:self.period]) / self.period
        out[self.period] = val
        for j in range(self.period, len(tr)):
            val = (val * (self.period - 1) + tr[j]) / self.period
            out[j + 1] = val
        
        # Pre-compute ATR% hist stats so classify() is O(1)
        # Compute per-bar ATR%
        atr_pct = [(out[i] / closes[i] * 100.0)
                if (out[i] is not None and closes[i]) else None
                for i in range(len(out))]
        
        # Rolling hist stats per bar - see rolling_min_max().
        hist_lo, hist_hi = rolling_min_max(atr_pct, HIST_WINDOW)

        return {"atr_vals": out, "atr_pct": atr_pct,
                "hist_lo": hist_lo,
                "hist_hi": hist_hi}

    # =============================================================================
    # ATR.classify  -  ATR% relative to stock's own 1-year ATR% range
    # =============================================================================
    # Low ATR% relative to history = stock is calm = higher score (entry-friendly).
    # High ATR% relative to history = elevated risk = lower score.
    # Score is INVERTED: score = 100 - hist_pct
    # =============================================================================

    def classify(self, stock, index: int = -1):
        """History-normalised ATR classify (inverted: low vol = high score)."""
        data    = stock.get_indicator(self.name)
        atr_raw = data["atr_vals"] if isinstance(data, dict) else data
        n   = len(atr_raw)
        idx = _norm_index(index, n)

        if idx < 0 or idx >= n or atr_raw[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}

        # data is now a dict from the updated compute()
        atr_vals = data.get("atr_vals", data) if isinstance(data, dict) else data
        atr_val  = atr_vals[idx] if isinstance(atr_vals, list) else data[idx]
        close    = stock.closes[idx]
        atr_pct  = (atr_val / close) * 100.0 if close else 0.0

        # Read pre-computed hist stats - O(1)
        lo  = data["hist_lo"][idx] if isinstance(data, dict) else None
        hi  = data["hist_hi"][idx] if isinstance(data, dict) else None
        cur = data.get("atr_pct", [None])[idx] if isinstance(data, dict) else None
        hist_pct = ((cur - lo) / (hi - lo) * 100.0
                    if (lo is not None and hi is not None and hi - lo > 1e-9 and cur is not None)
                    else None)

        if hist_pct is None:
            # fixed-threshold fallback
            if atr_pct < 2:
                return {"verdict": f"Low ({atr_pct:.1f}%)", "score": 70, "color": CLR.G,
                        "result": {"atr": atr_val, "atr_pct": atr_pct}}
            elif atr_pct < 4:
                return {"verdict": f"Moderate ({atr_pct:.1f}%)", "score": 50, "color": CLR.Y,
                        "result": {"atr": atr_val, "atr_pct": atr_pct}}
            elif atr_pct < 7:
                return {"verdict": f"High ({atr_pct:.1f}%)", "score": 30, "color": CLR.R,
                        "result": {"atr": atr_val, "atr_pct": atr_pct}}
            else:
                return {"verdict": f"Very High ({atr_pct:.1f}%)", "score": 10, "color": CLR.R,
                        "result": {"atr": atr_val, "atr_pct": atr_pct}}

        # Invert: high hist_pct (most volatile) → low score
        score = 100.0 - hist_pct

        # Label relative to own history
        if   hist_pct <= 25:
            label = "Low vol (1yr low)"
            color = CLR.G
        elif hist_pct <= 50:
            label = "Below avg vol"
            color = CLR.G
        elif hist_pct <= 75:
            label = "Above avg vol"
            color = CLR.Y
        else:               
            label = "High vol (1yr high)"
            color = CLR.R

        verdict = (f"{label}  ATR%={atr_pct:.1f}%  "
                f"({hist_pct:.0f}th pctile of 1yr range "
                f"{lo:.1f}%→{hi:.1f}%)")

        return {
            "verdict": verdict,
            "score":   int(round(score)),
            "color":   color,
            "result":  {
                "atr": atr_val, "atr_pct": atr_pct,
                "hist_lo": lo, "hist_hi": hi, "hist_pct": hist_pct,
            },
        }


class MFI(BaseTechnicalIndicator):
    """
    Money Flow Index (MFI) - volume-weighted RSI variant.

    Steps:
        1. Typical Price (TP) = (high + low + close) / 3
        2. Raw Money Flow = TP * volume
        3. Positive Money Flow = sum of Raw Money Flow on up-days (TP > previous TP)
        4. Negative Money Flow = sum of Raw Money Flow on down-days (TP < previous TP)
        5. Money Ratio (MR) = Positive MF / Negative MF
        6. MFI = 100 - (100 / (1 + MR))

    Classification uses continuous scoring with a trend adjustment:
    falling MFI from overbought levels receives a bonus (distribution
    may be ending), while rising MFI from oversold is penalised.

    Reference: Gene Quong & Avrum Soudack, "The Money Flow Index" (1989)
    """
    def __init__(self, period: int = 14):
        self.period = period

    @property
    def name(self):
        return f"MFI({self.period})"

    def compute(self, stock: Stock):
        highs, lows, closes, volumes = stock.highs, stock.lows, stock.closes, stock.volumes
        n = len(closes)
        out = [None] * n
        for i in range(n):
            if i < self.period:
                out[i] = 50.0
                continue
            tp = [(highs[j] + lows[j] + closes[j]) / 3
                  for j in range(i - self.period, i + 1)]
            mf = [tp[j] * volumes[i - self.period + j] for j in range(len(tp))]
            pos = sum(mf[j] for j in range(1, len(tp)) if tp[j] > tp[j-1])
            neg = sum(mf[j] for j in range(1, len(tp)) if tp[j] < tp[j-1])
            out[i] = 100.0 if neg == 0 else 100.0 - 100.0 / (1.0 + pos / neg)
        return out

    def classify(self, stock: "Stock", index: int = -1) -> Verdict:
        series = stock.get_indicator(self.name)
        idx = _norm_index(index, len(series))
        if idx < 0 or idx >= len(series) or series[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}

        mfi = series[idx]

        # MFI trend over 5 days
        mfi_5d_ago = series[idx - 5] if idx >= 5 and series[idx - 5] is not None else mfi
        mfi_trend = mfi - mfi_5d_ago
        
        # Base score from MFI level
        if mfi < 20:
            base = 100.0 - (mfi / 20) * 15.0  # 100 to 85
        elif mfi <= 40:
            base = 85.0 - ((mfi - 20) / 20) * 15.0  # 85 to 70
        elif mfi <= 50:
            base = 70.0 - ((mfi - 40) / 10) * 20.0  # 70 to 50
        elif mfi <= 65:
            base = 50.0 - ((mfi - 50) / 15) * 30.0  # 50 to 20
        elif mfi <= 80:
            base = 20.0 - ((mfi - 65) / 15) * 15.0  # 20 to 5
        else:
            base = max(0, 5.0 - ((mfi - 80) / 20) * 5.0)  # 5 to 0
        
        # Trend adjustment: reward falling MFI (distribution ending), penalize rising
        trend_adj = -mfi_trend * 1.5  # MFI dropping 10 points = +15 bonus
        
        score = max(0, min(100, base + trend_adj))
        
        # Verdict based on level
        if mfi < 20:
            verdict = f"OVERSOLD ({mfi:.0f})"
            color = CLR.G
        elif mfi <= 50:
            verdict = f"recovering value zone ({mfi:.0f})"
            color = CLR.G
        elif mfi <= 65:
            verdict = f"neutral ({mfi:.0f})"
            color = CLR.Y
        elif mfi <= 80:
            verdict = f"elevated ({mfi:.0f})"
            color = CLR.R
        else:
            verdict = f"OVERBOUGHT ({mfi:.0f})"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"mfi": mfi}}


class ADX(BaseTechnicalIndicator):
    """
    Average Directional Index (ADX) with +DI and -DI.

    Algorithm (Wilder's method):
        1. True Range (TR) - as in ATR.
        2. Directional Movement:
           +DM = current high - previous high (if positive and > -DM, else 0)
           -DM = previous low - current low (if positive and > +DM, else 0)
        3. Smoothed TR, +DM, -DM using Wilder's smoothing (period).
        4. +DI = (smoothed +DM / smoothed TR) * 100
        5. -DI = (smoothed -DM / smoothed TR) * 100
        6. DX = abs(+DI - -DI) / (+DI + -DI) * 100
        7. ADX = Wilder's smoothed average of DX.

    Classification is history-normalised: the current ADX is expressed
    as a percentile of its 1-year range. The directional bias from +DI/-DI
    determines whether the trend strength score is bullish or bearish.

    Reference: J. Welles Wilder Jr., "New Concepts in Technical Trading Systems" (1978)
    """
    def __init__(self, period: int = 14):
        self.period = period

    @property
    def name(self):
        return f"ADX({self.period})"

    def compute(self, stock: Stock):
        highs, lows, closes = stock.highs, stock.lows, stock.closes
        n = len(closes)
        p = self.period
        adx_vals = [0.0] * n
        plus_di = [0.0] * n
        minus_di = [0.0] * n
        if n < p * 2 + 2:
            return {"adx": adx_vals, "+di": plus_di, "-di": minus_di,
                    "hist_lo": [None] * n, "hist_hi": [None] * n}

        # compute True Range and directional movement once
        tr  = [0.0] * (n - 1)
        pdm = [0.0] * (n - 1)
        mdm = [0.0] * (n - 1)
        for i in range(1, n):
            tr[i-1] = max(highs[i] - lows[i],
                        abs(highs[i] - closes[i-1]),
                        abs(lows[i] - closes[i-1]))
            up = highs[i] - highs[i-1]
            dn = lows[i-1] - lows[i]
            pdm[i-1] = up if up > dn and up > 0 else 0.0
            mdm[i-1] = dn if dn > up and dn > 0 else 0.0

        # Wilder's smoothing for TR, +DM, -DM
        atr = sum(tr[:p]) / p
        atr_pdm = sum(pdm[:p]) / p
        atr_mdm = sum(mdm[:p]) / p
        dx_list = []
        pdi_list = []   # per-bar +DI, parallel to dx_list (was previously
        mdi_list = []   # discarded, leaving plus_di/minus_di backfilled
                        # with only the final loop iteration's values)
        for j in range(p, len(tr)):
            atr     = (atr * (p - 1) + tr[j]) / p
            atr_pdm = (atr_pdm * (p - 1) + pdm[j]) / p
            atr_mdm = (atr_mdm * (p - 1) + mdm[j]) / p
            pdi = (atr_pdm / atr) * 100 if atr else 0
            mdi = (atr_mdm / atr) * 100 if atr else 0
            di_sum = pdi + mdi
            dx_list.append(abs(pdi - mdi) / di_sum * 100 if di_sum else 0)
            pdi_list.append(pdi)
            mdi_list.append(mdi)

        if len(dx_list) < p:
            return {"adx": adx_vals, "+di": plus_di, "-di": minus_di,
                    "hist_lo": [None] * n, "hist_hi": [None] * n}

        adx_smooth = sum(dx_list[:p]) / p
        idx = p * 2
        adx_vals[idx] = adx_smooth
        plus_di[idx] = pdi_list[p - 1]   # DI for the same bar as dx_list[p-1]
        minus_di[idx] = mdi_list[p - 1]
        for k in range(p, len(dx_list)):
            adx_smooth = (adx_smooth * (p - 1) + dx_list[k]) / p
            idx = p * 2 + 1 + (k - p)
            if idx < n:
                adx_vals[idx] = adx_smooth
                plus_di[idx] = pdi_list[k]
                minus_di[idx] = mdi_list[k]
        
        # Pre-compute ADX hist stats so classify() is O(1). ADX's sentinel
        # for "no data yet" is 0.0 (not None) - translate that (and any
        # non-positive value) to None before the shared rolling helper,
        # which only understands None as "exclude this bar".
        adx_for_range = [v if (v is not None and v > 0) else None for v in adx_vals]
        hist_lo, hist_hi = rolling_min_max(adx_for_range, HIST_WINDOW)
        
        return {"adx": adx_vals, "+di": plus_di, "-di": minus_di,
                "hist_lo": hist_lo,
                "hist_hi": hist_hi}

    # =============================================================================
    # ADX.classify  -  trend strength relative to stock's own 1-year ADX range,
    #                  direction from +DI/-DI still applied on top
    # =============================================================================
    # hist_pct of ADX captures "how strong is this trend vs this stock's history".
    # Score = hist_pct (strong trend in historical context = high score),
    # then halved and shifted based on direction:
    #   bullish (+DI > -DI): score stays high
    #   bearish (-DI > +DI): score is inverted (strong bearish trend → low score)
    # =============================================================================

    def classify(self, stock, index: int = -1):
        """History-normalised ADX with directional bias from +DI/-DI."""
        data = stock.get_indicator(self.name)
        adx_series = data["adx"]
        n   = len(adx_series)
        idx = _norm_index(index, n)

        if idx < 0 or idx >= n or adx_series[idx] == 0.0:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}

        adx = adx_series[idx]
        pdi = data["+di"][idx]
        mdi = data["-di"][idx]

        # Use per-bar hist stats
        lo  = data["hist_lo"][idx]
        hi  = data["hist_hi"][idx]
        cur = adx_series[idx]
        hist_pct = (((cur - lo) / (hi - lo) * 100.0)
                    if (lo is not None and hi is not None
                        and hi - lo > 1e-9 and cur is not None)
                    else None)

        if hist_pct is None:
            # fixed-threshold fallback
            if adx > 25:   strength = "strong trend"
            elif adx > 20: strength = "developing"
            else:          strength = "no trend"
            bullish    = pdi > mdi
            adx_score  = min(80, adx * 2) if adx > 20 else max(20, adx * 2)
            if not bullish: adx_score = 100 - adx_score
            return {
                "verdict": f"{strength} / {'BULLISH' if bullish else 'BEARISH'}",
                "score":   int(adx_score),
                "color":   CLR.G if bullish else CLR.R,
                "result":  {"adx": adx, "+di": pdi, "-di": mdi},
            }

        bullish = pdi > mdi

        # Trend strength score: hist_pct of ADX (high ADX for this stock = strong trend)
        # For bullish:  high strength → high score
        # For bearish:  high strength → low score  (strong downtrend is bad for buyer)
        if bullish:
            score = hist_pct
        else:
            score = 100.0 - hist_pct

        # Label by percentile quartile
        if   hist_pct <= 25:  strength = "weak trend"
        elif hist_pct <= 50:  strength = "moderate trend"
        elif hist_pct <= 75:  strength = "strong trend"
        else:                 strength = "very strong trend"

        direction = "BULLISH" if bullish else "BEARISH"
        color     = CLR.G if bullish else CLR.R

        verdict = (f"{strength} / {direction}  ADX={adx:.1f}  "
                f"({hist_pct:.0f}th pctile of 1yr range "
                f"{lo:.1f}→{hi:.1f})")

        return {
            "verdict": verdict,
            "score":   int(round(score)),
            "color":   color,
            "result":  {
                "adx": adx, "+di": pdi, "-di": mdi,
                "hist_lo": lo, "hist_hi": hi, "hist_pct": hist_pct,
            },
        }


class Ichimoku(BaseTechnicalIndicator):
    """
    Ichimoku Kinko Hyo (Ichimoku Cloud).

    Components:
        Tenkan-sen (Conversion Line) = (highest high + lowest low) / 2 over 9 periods
        Kijun-sen  (Base Line)       = (highest high + lowest low) / 2 over 26 periods
        Senkou Span A (Leading A)    = (Tenkan + Kijun) / 2, plotted 26 periods ahead
        Senkou Span B (Leading B)    = (highest high + lowest low) / 2 over 52 periods, plotted 26 ahead
        Cloud = area between Senkou A and Senkou B.

    Classification uses the distance of price from the cloud (continuous)
    or the categorical cloud position when Senkou data is insufficient.

    Reference: Goichi Hosoda, "Ichimoku Kinko Hyo" (1969)
    """
    @property
    def name(self):
        return "Ichimoku"

    def compute(self, stock: Stock):
        highs, lows, closes = stock.highs, stock.lows, stock.closes
        n = len(closes)
        tenkan = [None] * n
        kijun = [None] * n
        senk_a = [None] * n
        senk_b = [None] * n
        cloud_pos = [None] * n
        for i in range(n):
            def mid(p):
                if i < p - 1:
                    return None
                return (max(highs[i - p + 1 : i + 1]) + min(lows[i - p + 1 : i + 1])) / 2
            t = mid(9)
            k = mid(26)
            sb = mid(52)
            tenkan[i] = t
            kijun[i] = k
            senk_b[i] = sb
            sa = (t + k) / 2 if t is not None and k is not None else None
            senk_a[i] = sa
            px = closes[i]
            if sa is not None and sb is not None:
                ct = max(sa, sb)
                cb = min(sa, sb)
                if px > ct: cloud_pos[i] = "Above (Bullish)"
                elif px < cb: cloud_pos[i] = "Below (Bearish)"
                else: cloud_pos[i] = "Inside (Neutral)"
            else:
                cloud_pos[i] = "Insufficient data"
        return {"tenkan": tenkan, "kijun": kijun, "senk_a": senk_a, "senk_b": senk_b, "cloud_pos": cloud_pos}

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        """Distance-based Ichimoku classification."""
        ichi = stock.get_indicator(self.name)
        idx = _norm_index(index, len(ichi["cloud_pos"]))
        if idx < 0 or idx >= len(ichi["cloud_pos"]) or ichi["cloud_pos"][idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        pos = ichi["cloud_pos"][idx]
        tenkan = ichi["tenkan"][idx]
        kijun = ichi["kijun"][idx]
        px = stock.closes[idx]

        # Distance above/below cloud as percentage
        senk_a = ichi["senk_a"][idx]
        senk_b = ichi["senk_b"][idx]
        
        if senk_a and senk_b:
            cloud_top = max(senk_a, senk_b)
            cloud_bot = min(senk_a, senk_b)
            
            if px > cloud_top:
                # Above cloud: distance from cloud top matters
                dist_pct = (px - cloud_top) / cloud_top * 100 if cloud_top > 0 else 0
                score = min(90, 65.0 + min(25, dist_pct * 5))  # 65-90 based on distance above
                verdict = f"Above cloud - bullish ({dist_pct:+.1f}%)"
                color = CLR.G
            elif px < cloud_bot:
                dist_pct = (cloud_bot - px) / cloud_bot * 100 if cloud_bot > 0 else 0
                score = max(10, 35.0 - min(25, dist_pct * 5))  # 35-10 based on distance below
                verdict = f"Below cloud - bearish ({dist_pct:+.1f}%)"
                color = CLR.R
            else:
                # Inside cloud: position within cloud
                cloud_thickness = cloud_top - cloud_bot
                pct_in = (px - cloud_bot) / cloud_thickness * 100 if cloud_thickness > 0 else 50
                score = 40.0 + (pct_in / 100) * 20
                verdict = f"Inside cloud - neutral ({pct_in:.0f}% through)"
                color = CLR.Y
        else:
            if "Above" in pos:
                score = 75
                verdict = "Above cloud - bullish"
                color = CLR.G
            elif "Below" in pos:
                score = 25
                verdict = "Below cloud - bearish"
                color = CLR.R
            else:
                score = 50
                verdict = "Insufficient data"
                color = CLR.DM
        
        return {"verdict": verdict, "score": int(round(score)), "color": color,
                "result": {"tenkan": tenkan, "kijun": kijun, "cloud_pos": pos}}


class FibonacciLevels(BaseTechnicalIndicator):
    """
    Fibonacci Retracement Levels.

    Based on the highest high and lowest low over a lookback period (default 126 days).
    Retracement levels are calculated from the high downwards:
        Level = High - (ratio * (High - Low))
    Standard ratios: 0.236, 0.382, 0.500, 0.618, 0.786.
    Extension: 1.272 (127.2%).

    Classification uses continuous scoring, with the highest scores
    in the 38.2%-61.8% zone (the "golden pocket").

    Reference: Leonardo Fibonacci (1202), applied to finance by various technical analysts.
    """
    def __init__(self, days: int = 126):
        self.days = days

    @property
    def name(self):
        return f"FibLevels({self.days})"

    def compute(self, stock: Stock):
        highs, lows = stock.highs, stock.lows
        n = len(stock.closes)
        out = [{} for _ in range(n)]
        for i in range(n):
            start = max(0, i - self.days + 1)
            hi = max(highs[start:i+1])
            lo = min(lows[start:i+1])
            diff = hi - lo if hi > lo else 1
            out[i] = {"hi": hi, "lo": lo,
                      "23.6%": hi - 0.236*diff, "38.2%": hi - 0.382*diff,
                      "50.0%": hi - 0.500*diff, "61.8%": hi - 0.618*diff,
                      "78.6%": hi - 0.786*diff, "127.2% ext": hi + 0.272*diff}
        return out

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        """Continuous Fibonacci retracement scoring - optimal zone: 38.2%-61.8%."""
        levels = stock.get_indicator(self.name)
        idx = _norm_index(index, len(levels))
        if idx < 0 or idx >= len(levels) or not levels[idx]:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        px = stock.closes[idx]
        hi = levels[idx]["hi"]
        lo = levels[idx]["lo"]
        diff = hi - lo if hi > lo else 1
        retrace_pct = (hi - px) / diff * 100
        
        # Continuous scoring based on Fibonacci zone quality
        # Optimal entry zone: 38.2% to 61.8% (highest scores)
        if retrace_pct <= 0:
            score = 5
            verdict = f"At/above high - extended ({retrace_pct:.1f}%)"
            color = CLR.R
        elif retrace_pct < 23.6:
            score = 5 + (retrace_pct / 23.6) * 30  # 5 to 35
            verdict = f"< 23.6% - barely pulled back ({retrace_pct:.1f}%)"
            color = CLR.Y
        elif retrace_pct < 38.2:
            score = 35 + ((retrace_pct - 23.6) / (38.2 - 23.6)) * 25  # 35 to 60
            verdict = f"23.6-38.2% - mild pullback ({retrace_pct:.1f}%)"
            color = CLR.Y
        elif retrace_pct <= 50:
            score = 60 + ((retrace_pct - 38.2) / (50 - 38.2)) * 30  # 60 to 90
            verdict = f"38.2-50% - ideal entry ({retrace_pct:.1f}%)"
            color = CLR.G
        elif retrace_pct <= 61.8:
            score = 90 - ((retrace_pct - 50) / (61.8 - 50)) * 10  # 90 to 80
            verdict = f"50-61.8% - valid retracement ({retrace_pct:.1f}%)"
            color = CLR.G
        elif retrace_pct <= 78.6:
            score = 80 - ((retrace_pct - 61.8) / (78.6 - 61.8)) * 25  # 80 to 55
            verdict = f"61.8-78.6% - deep pullback ({retrace_pct:.1f}%)"
            color = CLR.Y
        elif retrace_pct <= 100:
            score = 55 - ((retrace_pct - 78.6) / (100 - 78.6)) * 30  # 55 to 25
            verdict = f"78.6-100% - near lows ({retrace_pct:.1f}%)"
            color = CLR.R
        else:
            score = max(0, 25 - ((retrace_pct - 100) / 27.2) * 25)  # 25 to 0
            verdict = f"Below swing low - breakdown ({retrace_pct:.1f}%)"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color,
                "result": {"hi": hi, "lo": lo, "retrace_pct": retrace_pct}}


class MonthlyPivotPoints(BaseTechnicalIndicator):
    """
    Monthly Pivot Points (floor trader's method).

    Derived from the previous month's high, low, and close:
        Pivot Point (PP) = (H + L + C) / 3
        Resistance 1 (R1) = PP + 0.382 * (H - L)
        Resistance 2 (R2) = PP + 0.618 * (H - L)
        Resistance 3 (R3) = PP + 1.000 * (H - L)
        Support 1 (S1)     = PP - 0.382 * (H - L)
        Support 2 (S2)     = PP - 0.618 * (H - L)
        Support 3 (S3)     = PP - 1.000 * (H - L)

    Using Fibonacci ratios instead of the classic equidistant levels.

    Classification is continuous, with the highest scores near PP-R1
    (ideal entry zone) and S1-PP (pullback to support).

    Reference: Classic floor trading methodology, adapted with Fibonacci ratios.
    """
    @property
    def name(self):
        return "MonthlyPivot"

    def compute(self, stock: Stock):
        monthly = {}
        for c in stock.candles:
            ym = c.timestamp.strftime("%Y-%m")
            if ym not in monthly:
                monthly[ym] = {"hi": c.high, "lo": c.low, "cl": c.close}
            else:
                monthly[ym]["hi"] = max(monthly[ym]["hi"], c.high)
                monthly[ym]["lo"] = min(monthly[ym]["lo"], c.low)
                monthly[ym]["cl"] = c.close
        months = sorted(monthly.keys())
        if len(months) < 2:
            return {}
        prev = monthly[months[-2]]
        rng = prev["hi"] - prev["lo"]
        pp = (prev["hi"] + prev["lo"] + prev["cl"]) / 3
        return {"pp": pp, "r1": pp + 0.382*rng, "r2": pp + 0.618*rng, "r3": pp + rng,
                "s1": pp - 0.382*rng, "s2": pp - 0.618*rng, "s3": pp - rng, "month": months[-2]}

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        """Continuous pivot zone scoring - optimal: PP to R1."""
        piv = stock.get_indicator(self.name)
        idx = _norm_index(index, len(stock.closes))
        if not piv or idx < 0 or idx >= len(stock.closes):
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        px = stock.closes[idx]
        pp = piv["pp"]
        r1 = piv["r1"]
        r2 = piv["r2"]
        r3 = piv["r3"]
        s1 = piv["s1"]
        s2 = piv["s2"]
        s3 = piv["s3"]
        
        # Continuous scoring based on which zone price is in
        total_range = r3 - s3 if r3 > s3 else 1
        
        # Optimal zone: PP to R1 (and S1 to PP)
        if px > r3:
            score = max(0, 10 - (px - r3) / total_range * 10)
            verdict = "Above R3 - extremely extended"
            color = CLR.R
        elif px > r2:
            score = 10 + ((r3 - px) / (r3 - r2)) * 10  # 10 to 20
            verdict = "R2-R3 - strong resistance"
            color = CLR.R
        elif px > r1:
            score = 20 + ((r2 - px) / (r2 - r1)) * 20  # 20 to 40
            verdict = "R1-R2 - above first resistance"
            color = CLR.Y
        elif px > pp:
            score = 40 + ((r1 - px) / (r1 - pp)) * 45  # 40 to 85
            verdict = "PP-R1 - ideal entry zone"
            color = CLR.G
        elif px > s1:
            score = 60 + ((pp - px) / (pp - s1)) * 15  # 60 to 75
            verdict = "S1-PP - pullback to support"
            color = CLR.G
        elif px > s2:
            score = 45 + ((s1 - px) / (s1 - s2)) * 15  # 45 to 60
            verdict = "S2-S1 - testing support"
            color = CLR.Y
        elif px > s3:
            score = 25 + ((s2 - px) / (s2 - s3)) * 20  # 25 to 45
            verdict = "S3-S2 - weak zone"
            color = CLR.R
        else:
            score = max(0, 25 - (s3 - px) / total_range * 25)
            verdict = "Below S3 - strong breakdown"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": piv}


class RollingReturn(BaseTechnicalIndicator):
    """
    Rolling Return (Rate of Change).

    Formula:
        Ret(n)(t) = (close(t) / close(t-n) - 1) * 100

    where n is the period (default 5 days).

    Classification is history-normalised: the current return is expressed
    as a percentile of its own 1-year (252-day) range. This captures how
    unusual the current return is relative to the stock's own history.

    Reference: Standard momentum/ROC indicator.
    """
    def __init__(self, period: int = 5):
        self.period = period

    @property
    def name(self):
        return f"Ret({self.period})"

    def compute(self, stock: Stock):
        closes = stock.closes
        n = len(closes)
        out = [None] * n
        for i in range(n):
            if i >= self.period and closes[i - self.period] != 0:
                out[i] = (closes[i] / closes[i - self.period] - 1) * 100
        # Pre-compute hist stats so classify() is O(1)
        w       = min(HIST_WINDOW, len(out))
        valid_h = [v for v in out[-w:] if v is not None]
        return {"vals": out,
                "hist_lo": min(valid_h) if valid_h else None,
                "hist_hi": max(valid_h) if valid_h else None}

    # =============================================================================
    # RollingReturn.classify  -  return relative to stock's own 1-year return range
    # =============================================================================
    # hist_pct = 100 means current return is the highest it's been in a year.
    # Sign is naturally captured: if the historical range is mostly negative,
    # a flat return still scores higher than a deeply negative one.
    # =============================================================================

    def classify(self, stock, index: int = -1):
        """History-normalised return: higher percentile = stronger momentum."""
        data   = stock.get_indicator(self.name)
        series = data.get("vals") if isinstance(data, dict) else data
        n   = len(series)
        idx = _norm_index(index, n)

        if idx < 0 or idx >= n or series[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}

        ret = series[idx]
        # O(1): read pre-computed hist stats from compute() cache
        lo       = data.get("hist_lo") if isinstance(data, dict) else None
        hi       = data.get("hist_hi") if isinstance(data, dict) else None
        _vals    = data.get("vals") if isinstance(data, dict) else data
        _cur     = _vals[idx] if (_vals and idx < len(_vals)) else None
        hist_pct = (((_cur - lo) / (hi - lo) * 100.0)
                    if (lo is not None and hi is not None
                        and hi - lo > 1e-9 and _cur is not None)
                    else None)

        if hist_pct is None:
            # fixed-threshold fallback
            if ret > 5:    return {"verdict": f"Strong +{ret:.1f}%", "score": 95, "color": CLR.G, "result": {"return": ret}}
            elif ret > 2:  return {"verdict": f"+{ret:.1f}%",         "score": 75, "color": CLR.G, "result": {"return": ret}}
            elif ret > 0:  return {"verdict": f"+{ret:.1f}%",         "score": 60, "color": CLR.G, "result": {"return": ret}}
            elif ret > -2:
                return {"verdict": f"{ret:.1f}%",          "score": 40, "color": CLR.R, "result": {"return": ret}}
            elif ret > -5:
                return {"verdict": f"{ret:.1f}%",          "score": 25, "color": CLR.R, "result": {"return": ret}}
            else:          return {"verdict": f"Strong {ret:.1f}%",   "score": 5,  "color": CLR.R, "result": {"return": ret}}

        score = hist_pct   # high return vs own history = high score
        color = CLR.G if ret >= 0 else CLR.R

        if   hist_pct >= 75: label = "near 1yr high return"
        elif hist_pct >= 50: label = "above avg return"
        elif hist_pct >= 25: label = "below avg return"
        else:                label = "near 1yr low return"

        verdict = (f"{ret:+.1f}%  {label}  "
                   f"({hist_pct:.0f}th pctile of 1yr range {lo:+.1f}%->{hi:+.1f}%)")

        return {
            "verdict": verdict,
            "score":   int(round(score)),
            "color":   color,
            "result":  {
                "return": ret,
                "hist_lo": lo, "hist_hi": hi, "hist_pct": hist_pct,
            },
        }


class AnnualizedVolatility(BaseTechnicalIndicator):
    """
    Annualized Volatility (historical).

    Steps:
        1. Compute daily log-returns (or simple returns) over 'period' days.
        2. Calculate the sample standard deviation of those returns.
        3. Annualize by multiplying by sqrt(252) (trading days per year).
        4. Express as a percentage.

    Classification is history-normalised (inverted): low volatility
    relative to the stock's own 1-year range scores higher (entry-friendly).

    Reference: Standard quantitative finance methodology.
    """
    def __init__(self, period: int = 20):
        self.period = period

    @property
    def name(self):
        return f"AnnVol({self.period})"

    def compute(self, stock: Stock):
        closes = stock.closes
        n = len(closes)
        out = [None] * n
        for i in range(n):
            if i < self.period: continue
            rets = []
            for j in range(i - self.period + 1, i + 1):
                if closes[j-1] != 0:
                    rets.append(closes[j] / closes[j-1] - 1)
            if len(rets) < 3: continue
            mu = sum(rets) / len(rets)
            var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
            out[i] = math.sqrt(var) * math.sqrt(252) * 100
        
        # Rolling hist stats per bar - see rolling_min_max().
        hist_lo, hist_hi = rolling_min_max(out, HIST_WINDOW)

        return {"vals": out,
                "hist_lo": hist_lo,
                "hist_hi": hist_hi}

    # =============================================================================
    # AnnualizedVolatility.classify  -  relative to stock's own 1-year vol range
    # =============================================================================
    # Same inversion as ATR: low vol relative to history = high score.
    # =============================================================================
    def classify(self, stock, index: int = -1):
        """Inverted: low vol relative to history = high score."""
        data   = stock.get_indicator(self.name)
        series = data.get("vals") if isinstance(data, dict) else data
        n   = len(series)
        idx = _norm_index(index, n)

        if idx < 0 or idx >= n or series[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}

        vol = series[idx]
        # O(1): read pre-computed hist stats from compute() cache
        # Use per-bar hist stats (no look-ahead)
        lo       = data["hist_lo"][idx] if isinstance(data, dict) else None
        hi       = data["hist_hi"][idx] if isinstance(data, dict) else None
        _cur     = series[idx]
        hist_pct = (((_cur - lo) / (hi - lo) * 100.0)
                    if (lo is not None and hi is not None
                        and hi - lo > 1e-9 and _cur is not None)
                    else None)

        if hist_pct is None:
            if vol < 20:   return {"verdict": f"Low ({vol:.1f}%)",       "score": 75, "color": CLR.G,  "result": {"volatility": vol}}
            elif vol < 35:
                return {"verdict": f"Moderate ({vol:.1f}%)",   "score": 50, "color": CLR.Y,  "result": {"volatility": vol}}
            elif vol < 50:
                return {"verdict": f"High ({vol:.1f}%)",        "score": 25, "color": CLR.R,  "result": {"volatility": vol}}
            else:          return {"verdict": f"Very High ({vol:.1f}%)",   "score": 5,  "color": CLR.R,  "result": {"volatility": vol}}

        score = 100.0 - hist_pct  # Inverted: low vol = high score

        if   hist_pct <= 25:
            label = "Low vol regime"
            color = CLR.G
        elif hist_pct <= 50:
            label = "Below avg vol"
            color = CLR.G
        elif hist_pct <= 75:
            label = "Above avg vol"
            color = CLR.Y
        else:
            label = "High vol regime"
            color = CLR.R

        verdict = (f"{label}  {vol:.1f}%  "
                   f"({hist_pct:.0f}th pctile of 1yr range {lo:.1f}%->{hi:.1f}%)")

        return {
            "verdict": verdict,
            "score":   int(round(score)),
            "color":   color,
            "result":  {
                "volatility": vol,
                "hist_lo": lo, "hist_hi": hi, "hist_pct": hist_pct,
            },
        }


class CandlePatterns(BaseTechnicalIndicator):
    """
    Japanese Candlestick Pattern Recognition.

    Detects the following patterns (daily only):
        - Hammer / Shooting Star
        - Bullish / Bearish Engulfing
        - Morning Star / Evening Star
        - Bullish / Bearish Marubozu
        - Bullish / Bearish Harami
        - Doji

    Classification uses a net bullish/bearish count, mapped continuously to 0-100.

    Reference: Steve Nison, "Japanese Candlestick Charting Techniques" (1991)

    WIP : 
        - TODO figure out how to get more candles !
        - TODO compute weekly/monthly candles from daily candles
    """
    @property
    def name(self):
        return "Patterns"

    def compute(self, stock: Stock):
        n = len(stock.candles)
        out = [[] for _ in range(n)]
        for i in range(n):
            out[i] = self._patterns_at(i, stock)
        return out

    def _patterns_at(self, i, stock):
        """Detect common candlestick patterns at index i."""
        if i < 2:
            return []
        o, h, l, c = stock.opens[i], stock.highs[i], stock.lows[i], stock.closes[i]
        po, pc = stock.opens[i-1], stock.closes[i-1]
        body = abs(c - o)
        rng = h - l
        lo_w = min(o,c)-l
        hi_w = h-max(o,c)
        if rng < 1e-9:
            return []
        pats = []
        if body < rng*0.3 and lo_w > body*2 and hi_w < body*0.3: pats.append("Hammer")
        if pc < po and c > o and o < pc and c > po: pats.append("Bullish Engulfing")
        if i >= 2:
            ppo, ppc = stock.opens[i-2], stock.closes[i-2]
            pb = abs(stock.closes[i-1] - stock.opens[i-1])
            pr = stock.highs[i-1] - stock.lows[i-1]
            if ppc < ppo and pb < pr*0.3 and c > o and c > (ppo+ppc)/2: pats.append("Morning Star")
        if c > o and hi_w < body*0.1 and lo_w < body*0.1: pats.append("Bullish Marubozu")
        if pc < po and c > o and o > pc and c < po: pats.append("Bullish Harami")
        if body < rng*0.3 and hi_w > body*2 and lo_w < body*0.3: pats.append("Shooting Star")
        if pc > po and c < o and o > pc and c < po: pats.append("Bearish Engulfing")
        if i >= 2:
            ppo, ppc = stock.opens[i-2], stock.closes[i-2]
            pb = abs(stock.closes[i-1] - stock.opens[i-1])
            pr = stock.highs[i-1] - stock.lows[i-1]
            if ppc > ppo and pb < pr*0.3 and c < o and c < (ppo+ppc)/2: pats.append("Evening Star")
        if c < o and hi_w < body*0.1 and lo_w < body*0.1: pats.append("Bearish Marubozu")
        if body < rng*0.1: pats.append("Doji")
        return pats

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        series = stock.get_indicator(self.name)
        idx = _norm_index(index, len(series))
        if idx < 0 or idx >= len(series):
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        patterns = series[idx]
        if not patterns:
            return {"verdict": "-", "score": 50, "color": CLR.DM, "result": {"patterns": []}}
        
        bullish = sum(1 for p in patterns if "bullish" in p.lower() or "hammer" in p.lower() or "morning" in p.lower())
        bearish = sum(1 for p in patterns if "bearish" in p.lower() or "shooting" in p.lower() or "evening" in p.lower())
        net = bullish - bearish
        
        # Continuous: net -5 to +5 mapped to 0-100
        score = 50.0 + (net / 5.0) * 50.0  # net=0 -> 50, net=5 -> 100, net=-5 -> 0
        score = max(0, min(100, score))
        
        return {"verdict": ", ".join(patterns[:3]), "score": int(round(score)), "color": CLR.CY,
                "result": {"patterns": patterns, "bullish": bullish, "bearish": bearish}}


class CandleScore(BaseTechnicalIndicator):
    """
    Numeric Candlestick Scoring.

    Assigns points based on pattern strength and direction:
        +5 for strong bullish patterns (Engulfing, Morning Star, Hammer)
        +3 for moderate bullish patterns (Marubozu)
        -3 for moderate bearish patterns (Shooting Star, Bearish Harami)
        -5 for strong bearish patterns (Evening Star, Bearish Engulfing)

    Classification is history-normalised: the raw score is expressed as a
    percentile of its own 1-year range, producing a 0-100 signal.

    Reference: Derived from standard candlestick pattern weights.
    """
    @property
    def name(self):
        return "CandleScore"

    def compute(self, stock: Stock):
        n = len(stock.candles)
        out = [0.0] * n
        for i in range(n):
            out[i] = self._score_at(i, stock)
        # Pre-compute hist stats so classify() is O(1)
        w       = min(HIST_WINDOW, len(out))
        valid_h = [v for v in out[-w:] if v is not None]
        return {"vals": out,
                "hist_lo": min(valid_h) if valid_h else None,
                "hist_hi": max(valid_h) if valid_h else None}

    def _score_at(self, i, stock):
        """Score a single candle for pattern strength (-5 to +5)."""
        if i < 2:
            return 0.0
        o, h, l, c = stock.opens[i], stock.highs[i], stock.lows[i], stock.closes[i]
        po, pc = stock.opens[i-1], stock.closes[i-1]
        body = abs(c - o)
        rng = h - l
        lo_w = min(o,c)-l
        hi_w = h-max(o,c)
        if rng < 1e-9:
            return 0.0
        if body < rng*0.3 and lo_w > body*2:
            return 5.0
        if pc < po and c > o and o < pc and c > po:
            return 5.0
        if i >= 2:
            ppo, ppc = stock.opens[i-2], stock.closes[i-2]
            pb = abs(stock.closes[i-1] - stock.opens[i-1])
            pr = stock.highs[i-1] - stock.lows[i-1]
            if ppc < ppo and pb < pr*0.3 and c > o and c > (ppo+ppc)/2:
                return 5.0
        if c > o and hi_w < body*0.1 and lo_w < body*0.1:
            return 3.0
        if body < rng*0.3 and hi_w > body*2:
            return -3.0
        if pc > po and c < o and o > pc and c < po:
            return -3.0
        return 0.0

    # =============================================================================
    # CandleScore.classify  -  score relative to stock's own 1-year candle score range
    # =============================================================================
    # CandleScore is already a derived numeric score from pattern detection.
    # Min-max normalising it over history converts "what does +3 mean for this stock"
    # into a consistent 0-100.
    # =============================================================================

    def classify(self, stock, index: int = -1):
        """History-normalised candle score classification."""
        data   = stock.get_indicator(self.name)
        series = data.get("vals") if isinstance(data, dict) else data
        n   = len(series)
        idx = _norm_index(index, n)

        if idx < 0 or idx >= n or series[idx] is None:
            return {"verdict": "N/A", "score": 50, "color": CLR.DM, "result": {}}

        cs_val = series[idx]

        # If score is 0, no pattern - return neutral immediately
        if cs_val == 0:
            return {"verdict": "No pattern", "score": 50, "color": CLR.DM,
                    "result": {"candle_score": 0}}

        # O(1): read pre-computed hist stats from compute() cache
        lo       = data.get("hist_lo") if isinstance(data, dict) else None
        hi       = data.get("hist_hi") if isinstance(data, dict) else None
        _vals    = data.get("vals") if isinstance(data, dict) else data
        _cur     = _vals[idx] if (_vals and idx < len(_vals)) else None
        hist_pct = (((_cur - lo) / (hi - lo) * 100.0)
                    if (lo is not None and hi is not None
                        and hi - lo > 1e-9 and _cur is not None)
                    else None)

        if hist_pct is None or (hi - lo) < 0.5:
            # range too narrow or insufficient history - use sign only
            if   cs_val > 0:  return {"verdict": "Bullish pattern",  "score": 70, "color": CLR.G, "result": {"candle_score": cs_val}}
            elif cs_val < 0:  return {"verdict": "Bearish pattern",  "score": 30, "color": CLR.R, "result": {"candle_score": cs_val}}
            else:             return {"verdict": "No pattern",       "score": 50, "color": CLR.DM, "result": {"candle_score": cs_val}}

        score = hist_pct

        if   cs_val > 0:
            color = CLR.G
            direction = "Bullish"
        elif cs_val < 0:
            color = CLR.R
            direction = "Bearish"
        else:
            color = CLR.DM
            direction = "Neutral"

        verdict = (f"{direction} candle score={cs_val:+.1f}  "
                   f"({hist_pct:.0f}th pctile of 1yr range {lo:+.1f}->{hi:+.1f})")

        return {
            "verdict": verdict,
            "score":   int(round(score)),
            "color":   color,
            "result":  {
                "candle_score": cs_val,
                "hist_lo": lo, "hist_hi": hi, "hist_pct": hist_pct,
            },
        }



# =============================================================================
# NOTE for future developers - TTM Squeeze & Keltner Channel ATR period
# =============================================================================
# The classic TTM Squeeze uses Bollinger Bands (20,2) and a Keltner Channel
# built with EMA(20) and **ATR(10)** (multiplier 1.5).
#
# In this codebase we only have an ATR(14) indicator pre-computed by default.
# Adding a separate ATR(10) would require a second ATR instance and extra
# computation.  Using ATR(14) instead of ATR(10) makes the Keltner Channel
# slightly smoother, but the squeeze condition (BB completely inside KC) still
# reliably identifies low-volatility compression.
#
# If you ever need to match the exact classic parameters:
#   1. Add an ATR(10) indicator (e.g. `ATR(10)`)
#   2. Change KeltnerChannel's default `atr_period` to 10
#   3. Update TTMSqueeze and IndicatorTable to reference `KC(20,10,1.5)`
#
# The existing implementation is a pragmatic trade-off that avoids extra
# computation while preserving the quality of the squeeze signal.
# =============================================================================
class KeltnerChannel(BaseTechnicalIndicator):
    """
    Keltner Channel (ATR-based version).

    Construction:
        Middle Band = EMA(close, period)
        Upper Band  = Middle Band + (multiplier * ATR(atr_period))
        Lower Band  = Middle Band - (multiplier * ATR(atr_period))

    Default parameters: period=20, atr_period=14, multiplier=1.5.

    Reuses the pre-computed EMA and ATR to avoid redundant calculation.
    Note: Classic TTM Squeeze uses ATR(10); we use ATR(14) as a pragmatic
    trade-off (see detailed note in source).

    Reference: Chester W. Keltner, "How to Make Money in Commodities" (1960),
               adapted with ATR by Linda Bradford Raschke.
    """
    def __init__(self, period: int = 20, atr_period: int = 14, multiplier: float = 1.5):
        self.period = period
        self.atr_period = atr_period
        self.multiplier = multiplier

    @property
    def name(self):
        return f"KC({self.period},{self.atr_period},{self.multiplier})"

    def compute(self, stock: Stock):
        n = len(stock.closes)
        
        middle = stock.get_indicator(f"EMA({self.period})")

        atr_data = stock.get_indicator(f"ATR({self.atr_period})")
        if isinstance(atr_data, dict):
            atr = atr_data["atr_vals"]
        else:
            atr = atr_data
        
        # Upper and lower bands
        upper = [None] * n
        lower = [None] * n
        for i in range(n):
            if middle[i] is not None and atr[i] is not None:
                upper[i] = middle[i] + self.multiplier * atr[i]
                lower[i] = middle[i] - self.multiplier * atr[i]
        
        return {
            "upper": upper,
            "middle": middle,
            "lower": lower,
        }

    def classify(self, stock: "Stock", index: int = -1) -> Verdict:
        kc = stock.get_indicator(self.name)
        n = len(kc["upper"])
        idx = _norm_index(index, n)

        if idx < 0 or idx >= n or kc["upper"][idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        px = stock.closes[idx]
        up = kc["upper"][idx]
        mid = kc["middle"][idx]
        lo = kc["lower"][idx]
        
        # Continuous scoring based on position within channel
        if up != lo:
            pct = (px - lo) / (up - lo) * 100  # 0% at lower, 100% at upper
        else:
            pct = 50
        
        if pct <= 0:
            # Below lower: 0 to -10% = 30 down to 10
            dist_below = min(10, abs(pct))
            score = max(10, 30 - dist_below * 2)
            verdict = f"BELOW lower — weak momentum ({pct:.0f}%)"
            color = CLR.R
        elif pct >= 100:
            # Above upper: 100 to 110% = 70 up to 90
            dist_above = min(10, pct - 100)
            score = min(90, 70 + dist_above * 2)
            verdict = f"ABOVE upper — strong momentum (+{pct-100:.0f}%)"
            color = CLR.G
        else:
            # Inside channel: 0-100% mapped to 30-70
            score = 30 + (pct / 100) * 40
            verdict = f"inside channel ({pct:.0f}%)"
            color = CLR.Y
        
        return {"verdict": verdict, "score": int(round(score)), "color": color,
                "result": {"upper": up, "middle": mid, "lower": lo}}


class TTMSqueeze(BaseTechnicalIndicator):
    """
    TTM Squeeze (Bollinger Bands inside Keltner Channel).

    Condition:
        Squeeze ON when BB(20,2) upper < KC(20,14,1.5) upper
                    AND BB(20,2) lower > KC(20,14,1.5) lower

    States tracked:
        - squeeze_on  : first bar where squeeze activates.
        - squeeze_off : first bar after squeeze ends (the "firing" signal).

    A squeeze indicates compressed volatility that is likely to expand.
    It does NOT predict direction - the BonusComputer uses this as a
    pure volatility expectation premium.

    Reference: John Carter, "Mastering the Trade" (2005) - TTM Squeeze.
    """
    @property
    def name(self):
        return "TTM_Squeeze"

    def compute(self, stock: Stock):
        bb = stock.get_indicator("BB(20,2.0)")
        kc = stock.get_indicator("KC(20,14,1.5)")
        
        n = len(stock.closes)
        squeeze = [False] * n
        squeeze_on = [False] * n
        squeeze_off = [False] * n
        
        for i in range(n):
            bb_upper = bb["upper"][i]
            bb_lower = bb["lower"][i]
            kc_upper = kc["upper"][i]
            kc_lower = kc["lower"][i]
            
            if None in (bb_upper, bb_lower, kc_upper, kc_lower):
                continue
            
            squeeze[i] = bb_upper < kc_upper and bb_lower > kc_lower
            
            if i > 0:
                if not squeeze[i] and squeeze[i-1]:
                    squeeze_off[i] = True
                elif squeeze[i] and not squeeze[i-1]:
                    squeeze_on[i] = True
        
        return {
            "squeeze": squeeze,
            "squeeze_on": squeeze_on,
            "squeeze_off": squeeze_off,
        }

    def classify(self, stock: "Stock", index: int = -1) -> Verdict:
        data = stock.get_indicator(self.name)
        n = len(data["squeeze"])
        idx = _norm_index(index, n)

        if idx < 0 or idx >= n:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        is_squeeze = data["squeeze"][idx]
        just_fired = data["squeeze_off"][idx]
        just_started = data["squeeze_on"][idx]
        
        if just_fired:
            return {"verdict": "TTM SQUEEZE FIRED", "score": 85, 
                    "color": CLR.M, "result": {"squeeze": True, "fired": True}}
        elif just_started:
            return {"verdict": "TTM Squeeze started", "score": 65, 
                    "color": CLR.Y, "result": {"squeeze": True, "started": True}}
        elif is_squeeze:
            return {"verdict": "TTM In squeeze", "score": 55, 
                    "color": CLR.CY, "result": {"squeeze": True}}
        else:
            return {"verdict": "No squeeze", "score": 50,
                    "color": CLR.DM, "result": {"squeeze": False}}


class Stochastic(BaseTechnicalIndicator):
    """
    Slow Stochastic Oscillator.

    Formula:
        Raw %K(t) = 100 * (close(t) - LLV(low, period)) / (HHV(high, period) - LLV(low, period))
        %K (slow) = SMA(Raw %K, smooth_k)
        %D        = SMA(%K, smooth_d)

    LLV/HHV (lowest-low / highest-high over the trailing window) use the
    shared rolling_min_max() helper (O(n) monotonic deque) rather than a
    per-bar rescan. A flat range (HHV == LLV) leaves Raw %K undefined
    (None) for that bar rather than dividing by zero or fabricating a
    value.

    Classification mirrors BollingerBands' %B: continuous, inverted
    (low %K = oversold = high score), with a %K/%D crossover bonus/
    penalty of +/-10 mirroring MACD's signal-line-cross treatment.

    Reference: George Lane, developed in the late 1950s.
    """
    def __init__(self, period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
        self.period = period
        self.smooth_k = smooth_k
        self.smooth_d = smooth_d

    @property
    def name(self):
        return f"Stoch({self.period},{self.smooth_k},{self.smooth_d})"

    @staticmethod
    def _sma_skip_none(values: List[Optional[float]], period: int) -> List[Optional[float]]:
        """SMA requiring `period` consecutive non-None trailing values;
        None otherwise (no partial-window averaging, no look-ahead)."""
        n = len(values)
        out = [None] * n
        for i in range(n):
            if i < period - 1:
                continue
            window = values[i - period + 1: i + 1]
            if any(v is None for v in window):
                continue
            out[i] = sum(window) / period
        return out

    def compute(self, stock: Stock):
        highs, lows, closes = stock.highs, stock.lows, stock.closes
        n = len(closes)
        raw_k: List[Optional[float]] = [None] * n

        llv, _ = rolling_min_max(lows, self.period, min_valid=self.period)
        _, hhv = rolling_min_max(highs, self.period, min_valid=self.period)

        for i in range(n):
            if llv[i] is None or hhv[i] is None:
                continue
            rng = hhv[i] - llv[i]
            if rng > 1e-9:
                raw_k[i] = (closes[i] - llv[i]) / rng * 100.0
            # else: flat range - Raw %K stays undefined (None)

        k = self._sma_skip_none(raw_k, self.smooth_k)
        d = self._sma_skip_none(k, self.smooth_d)
        return {"k": k, "d": d}

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        data = stock.get_indicator(self.name)
        k_series, d_series = data["k"], data["d"]
        n = len(k_series)
        idx = _norm_index(index, n)
        if idx < 0 or idx >= n or k_series[idx] is None or d_series[idx] is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}

        k_val = k_series[idx]
        d_val = d_series[idx]

        # Continuous %K scoring, mirroring BollingerBands' %B: 0% = at the
        # low of the range (oversold, best entry), 100% = at the high.
        if k_val <= 0:
            score = 90.0 + min(10, abs(k_val))
            verdict = f"BELOW range - oversold ({k_val:.0f}%K)"
            color = CLR.G
        elif k_val <= 20:
            score = 80.0 + ((20 - k_val) / 20) * 10
            verdict = f"oversold ({k_val:.0f}%K)"
            color = CLR.G
        elif k_val <= 50:
            score = 60.0 + ((50 - k_val) / 30) * 20
            verdict = f"lower half ({k_val:.0f}%K)"
            color = CLR.Y
        elif k_val <= 80:
            score = 40.0 + ((80 - k_val) / 30) * 20
            verdict = f"upper half ({k_val:.0f}%K)"
            color = CLR.Y
        elif k_val <= 100:
            score = 20.0 + ((100 - k_val) / 20) * 20
            verdict = f"overbought ({k_val:.0f}%K)"
            color = CLR.R
        else:
            score = max(0, 20.0 - (k_val - 100) * 2)
            verdict = f"ABOVE range - overbought ({k_val:.0f}%K)"
            color = CLR.R

        # %K/%D crossover bonus/penalty, mirroring MACD's signal-line cross.
        prev_k = k_series[idx - 1] if idx >= 1 else None
        prev_d = d_series[idx - 1] if idx >= 1 else None
        if prev_k is not None and prev_d is not None:
            if prev_k <= prev_d and k_val > d_val:
                score = min(100, score + 10)
            elif prev_k >= prev_d and k_val < d_val:
                score = max(0, score - 10)

        return {"verdict": verdict, "score": int(round(score)), "color": color,
                "result": {"k": k_val, "d": d_val}}


class HeikinAshi(BaseTechnicalIndicator):
    """
    Heikin-Ashi smoothed candles, used to detect trend-reversal setups.

    Formula:
        HA_close(t) = (open(t) + high(t) + low(t) + close(t)) / 4
        HA_open(t)  = (HA_open(t-1) + HA_close(t-1)) / 2   (bar 0 seeded
                      from the real open/close average)
        HA_high(t)  = max(high(t), HA_open(t), HA_close(t))
        HA_low(t)   = min(low(t), HA_open(t), HA_close(t))

    Recursive on the indicator's own prior output only (never on another
    indicator's history), so it's inherently look-ahead-safe at any index.
    `streak` (precomputed per-bar) is the signed consecutive-color run
    length - positive for a green run, negative for a red run - so
    classify() can detect reversal setups in O(1) per bar instead of
    rescanning.

    Classification looks for a color flip preceded either by a run of >=3
    same-colored bars, or by a single "indecision" bar (small body, long
    wick opposing the flip direction) - both are look-ahead-safe since
    they only reference bars up to and including the current index.

    Reference: Munehisa Homma's candlestick charting, Heikin-Ashi
    smoothing popularized in the 2000s technical-analysis literature.
    """
    @property
    def name(self):
        return "HeikinAshi"

    def compute(self, stock: Stock):
        n = len(stock.candles)
        opens, highs, lows, closes = stock.opens, stock.highs, stock.lows, stock.closes

        ha_open:  List[float] = [0.0] * n
        ha_high:  List[float] = [0.0] * n
        ha_low:   List[float] = [0.0] * n
        ha_close: List[float] = [0.0] * n
        color:  List[str] = [""] * n
        streak: List[int] = [0] * n

        for i in range(n):
            ha_close[i] = (opens[i] + highs[i] + lows[i] + closes[i]) / 4.0
            ha_open[i] = ((opens[i] + closes[i]) / 2.0 if i == 0
                          else (ha_open[i - 1] + ha_close[i - 1]) / 2.0)
            ha_high[i] = max(highs[i], ha_open[i], ha_close[i])
            ha_low[i] = min(lows[i], ha_open[i], ha_close[i])

            color[i] = "green" if ha_close[i] >= ha_open[i] else "red"
            step = 1 if color[i] == "green" else -1
            streak[i] = step if (i == 0 or color[i] != color[i - 1]) else streak[i - 1] + step

        return {"ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low,
                "ha_close": ha_close, "color": color, "streak": streak}

    @staticmethod
    def _is_indecision(i: int, data: dict, wick_side: str) -> bool:
        """Small body relative to range, with a long wick on `wick_side`
        ("lower" for a bullish tell, "upper" for a bearish tell)."""
        rng = data["ha_high"][i] - data["ha_low"][i]
        if rng <= 1e-9:
            return False
        o, c = data["ha_open"][i], data["ha_close"][i]
        body = abs(c - o)
        if wick_side == "lower":
            wick = min(o, c) - data["ha_low"][i]
        else:
            wick = data["ha_high"][i] - max(o, c)
        return (body / rng) < 0.3 and (wick / rng) > 0.4

    def classify(self, stock: Stock, index: int = -1) -> Verdict:
        data = stock.get_indicator(self.name)
        color = data["color"]
        streak = data["streak"]
        n = len(color)
        idx = _norm_index(index, n)
        if idx < 0 or idx >= n:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}

        cur_color = color[idx]
        cur_streak = streak[idx]

        bullish_setup = bearish_setup = False
        prior_streak = 0
        if idx >= 1:
            prev_color, prev_streak_len = color[idx - 1], streak[idx - 1]
            if cur_color == "green" and prev_color == "red":
                prior_streak = abs(prev_streak_len)
                bullish_setup = prior_streak >= 3 or self._is_indecision(idx - 1, data, "lower")
            elif cur_color == "red" and prev_color == "green":
                prior_streak = prev_streak_len
                bearish_setup = prior_streak >= 3 or self._is_indecision(idx - 1, data, "upper")

        if bullish_setup:
            strength = min(1.0, prior_streak / 6.0) if prior_streak else 0.5
            score = 60.0 + strength * 30.0  # 60-90
            verdict = f"Bullish HA reversal (prior red streak={prior_streak})"
            color_out = CLR.G
        elif bearish_setup:
            strength = min(1.0, prior_streak / 6.0) if prior_streak else 0.5
            score = 40.0 - strength * 30.0  # 10-40
            verdict = f"Bearish HA reversal (prior green streak={prior_streak})"
            color_out = CLR.R
        else:
            score = 50.0
            verdict = f"{cur_color} streak={cur_streak:+d}"
            color_out = CLR.G if cur_color == "green" else CLR.R

        return {"verdict": verdict, "score": int(round(score)), "color": color_out,
                "result": {"color": cur_color, "streak": cur_streak}}


# Shared, stateless instances - built once at import time and reused by
# every Stock. Indicators carry no per-instance state beyond the
# construction-time parameters set in __init__ (period, stddev, ...); they
# are never mutated by compute()/classify(), so one instance per class is
# safe to share across all stocks, including under the ThreadPoolExecutor
# parallel loading in Stonks._load_all_stocks. Previously each Stock built
# its own full set (~35 objects x N stocks) purely to call read-only methods.
TECHNICAL_INDICATORS: List[BaseTechnicalIndicator] = [cls() for cls in BaseTechnicalIndicator._registry]


# =============================================================================
# BASE FUNDAMENTAL + CONCRETE CLASSES
# =============================================================================
class BaseFundamentalIndicator(ABC):
    """
    Abstract base for all fundamental metrics.
    Subclasses auto-register via __init_subclass__.
    classify() accepts optional raw_value to avoid double computation.
    """
    _registry = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseFundamentalIndicator._registry.append(cls)

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def compute(self, stock: Stock) -> Any:
        """Extract raw value(s) from stock.metadata['fundamentals']."""
        pass

    @abstractmethod
    def classify(self, stock: Stock, raw_value: Any = None) -> Verdict:
        """
        Classify the fundamental.
        If raw_value is provided, use it directly (avoids double compute).
        """
        pass
    
    # -- Convenience accessors ------------------------------

    def get_value(self, stock: Stock) -> Any:
        """Return the raw fundamental value."""
        return self.compute(stock)

    def get_verdict(self, stock: Stock) -> str:
        """Return only the verdict string."""
        return self.classify(stock).get("verdict", "N/A")

    def get_score(self, stock: Stock) -> int:
        """Return only the numeric score (0-100)."""
        return self.classify(stock).get("score", 0)

    def get_color(self, stock: Stock) -> str:
        """Return only the ANSI colour code."""
        return self.classify(stock).get("color", CLR.DM)

    def get_result(self, stock: Stock) -> Dict[str, Any]:
        """Return the extra result dict."""
        return self.classify(stock).get("result", {})


class TrailingPE(BaseFundamentalIndicator):
    """
    Trailing Price-to-Earnings Ratio.

    Uses the trailingPE field from Yahoo Finance fundamentals.
    Score is continuous: PE=5 → 90, PE=15 → 70, PE=25 → 50, PE=50+ → 0.
    """
    @property
    def name(self):
        return "TrailingPE"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("trailingPE")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        pe = raw_value if raw_value is not None else self.compute(stock)
        if pe is None or pe <= 0:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        # Continuous: PE 0-50+ mapped to 100-0
        score = max(0, min(100, 100.0 - (pe / 50.0) * 100.0))
        
        if pe <= 12:
            verdict = f"cheap (PE={pe:.1f})"
            color = CLR.G
        elif pe <= 18:
            verdict = f"reasonable (PE={pe:.1f})"
            color = CLR.G
        elif pe <= 25:
            verdict = f"fair (PE={pe:.1f})"
            color = CLR.Y
        elif pe <= 35:
            verdict = f"elevated (PE={pe:.1f})"
            color = CLR.Y
        elif pe <= 50:
            verdict = f"expensive (PE={pe:.1f})"
            color = CLR.R
        else:
            verdict = f"very expensive (PE={pe:.1f})"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"pe": pe}}


class ForwardPE(BaseFundamentalIndicator):
    """
    Forward Price-to-Earnings Ratio.

    Uses the forwardPE field from Yahoo Finance.
    Score is slightly more generous than TrailingPE (max 100 at PE=0, 0 at PE=45+).
    """
    @property
    def name(self):
        return "ForwardPE"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("forwardPE")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        pe = raw_value if raw_value is not None else self.compute(stock)
        if pe is None or pe <= 0:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        # Continuous: PE 0-50+ mapped to 100-0 (forward PE slightly more generous)
        score = max(0, min(100, 100.0 - (pe / 45.0) * 100.0))
        
        if pe <= 12:
            verdict = f"cheap (PE={pe:.1f})"
            color = CLR.G
        elif pe <= 18:
            verdict = f"reasonable (PE={pe:.1f})"
            color = CLR.G
        elif pe <= 25:
            verdict = f"fair (PE={pe:.1f})"
            color = CLR.Y
        elif pe <= 35:
            verdict = f"elevated (PE={pe:.1f})"
            color = CLR.Y
        elif pe <= 50:
            verdict = f"expensive (PE={pe:.1f})"
            color = CLR.R
        else:
            verdict = f"very expensive (PE={pe:.1f})"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"pe": pe}}


class PriceToBook(BaseFundamentalIndicator):
    """
    Price-to-Book Ratio.

    Score: P/B=0.5 → 95, P/B=1 → 90, P/B=3 → 70, P/B=10+ → 0.
    """
    @property
    def name(self):
        return "P/B"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("priceToBook")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        pb = raw_value if raw_value is not None else self.compute(stock)
        if pb is None or pb <= 0:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        # Continuous: P/B 0-10+ mapped to 100-0
        score = max(0, min(100, 100.0 - (pb / 10.0) * 100.0))
        
        if pb <= 1.0:
            verdict = f"below book (P/B={pb:.2f})"
            color = CLR.G
        elif pb <= 2.0:
            verdict = f"reasonable (P/B={pb:.2f})"
            color = CLR.G
        elif pb <= 3.5:
            verdict = f"moderate (P/B={pb:.2f})"
            color = CLR.Y
        elif pb <= 6.0:
            verdict = f"elevated (P/B={pb:.2f})"
            color = CLR.Y
        else:
            verdict = f"high (P/B={pb:.2f})"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"pb": pb}}


class PEGRatio(BaseFundamentalIndicator):
    """
    Price/Earnings-to-Growth (PEG) Ratio.

    Score: PEG=0.5 → 90, PEG=1 → 80, PEG=2 → 60, PEG=5+ → 0.
    Lower PEG = better value relative to growth.
    """
    @property
    def name(self):
        return "PEG"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("pegRatio")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        peg = raw_value if raw_value is not None else self.compute(stock)
        if peg is None or peg <= 0:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        # Continuous: PEG 0-5+ mapped to 100-0 (lower is better)
        score = max(0, min(100, 100.0 - (peg / 5.0) * 100.0))
        
        if peg < 1.0:
            verdict = f"growth at discount (PEG={peg:.2f})"
            color = CLR.G
        elif peg < 1.5:
            verdict = f"fairly valued (PEG={peg:.2f})"
            color = CLR.G
        elif peg < 2.5:
            verdict = f"slightly rich (PEG={peg:.2f})"
            color = CLR.Y
        else:
            verdict = f"expensive for growth (PEG={peg:.2f})"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"peg": peg}}
    

class ReturnOnEquity(BaseFundamentalIndicator):
    """
    Return on Equity (ROE).

    Expressed as a percentage. Score: ROE=25%+ → 95, ROE=0% → 25, negative → 5.
    """
    @property
    def name(self):
        return "ROE"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("returnOnEquity")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        roe = raw_value if raw_value is not None else self.compute(stock)
        if roe is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        pct = roe * 100
        
        # Continuous: ROE -10% to 35%+ mapped to 0-100
        score = max(0, min(100, ((pct + 10) / 45.0) * 100.0))
        
        if pct >= 25:
            verdict = f"{pct:.1f}% excellent"
            color = CLR.G
        elif pct >= 15:
            verdict = f"{pct:.1f}% good"
            color = CLR.G
        elif pct >= 10:
            verdict = f"{pct:.1f}% adequate"
            color = CLR.Y
        elif pct >= 0:
            verdict = f"{pct:.1f}% weak"
            color = CLR.R
        else:
            verdict = f"{pct:.1f}% negative"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"roe_pct": pct}}


class ProfitMargins(BaseFundamentalIndicator):
    """
    Net Profit Margin.

    Score: 25%+ → 95, 15%+ → 75, 0% → 25, negative → 5.
    """
    @property
    def name(self):
        return "ProfitMargin"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("profitMargins")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        m = raw_value if raw_value is not None else self.compute(stock)
        if m is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        pct = m * 100
        
        # Continuous: -10% to 35%+ mapped to 0-100
        score = max(0, min(100, ((pct + 10) / 45.0) * 100.0))
        
        if pct >= 25:
            verdict = f"{pct:.1f}% excellent"
            color = CLR.G
        elif pct >= 15:
            verdict = f"{pct:.1f}% strong"
            color = CLR.G
        elif pct >= 8:
            verdict = f"{pct:.1f}% decent"
            color = CLR.Y
        elif pct >= 0:
            verdict = f"{pct:.1f}% thin"
            color = CLR.R
        else:
            verdict = f"{pct:.1f}% loss-making"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"margin_pct": pct}}


class RevenueGrowth(BaseFundamentalIndicator):
    """
    Year-over-Year Revenue Growth.

    Score: 25%+ → 90, 10% → 70, 0% → 50, -10% → 30, -20% → 10.
    """
    @property
    def name(self):
        return "RevenueGrowth"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("revenueGrowth")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        g = raw_value if raw_value is not None else self.compute(stock)
        if g is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        pct = g * 100
        
        # Continuous: -20% to 40%+ mapped to 0-100
        score = max(0, min(100, ((pct + 20) / 60.0) * 100.0))
        
        if pct >= 25:
            verdict = f"{pct:+.1f}% strong"
            color = CLR.G
        elif pct >= 10:
            verdict = f"{pct:+.1f}% good"
            color = CLR.G
        elif pct >= 0:
            verdict = f"{pct:+.1f}% flat"
            color = CLR.Y
        elif pct >= -10:
            verdict = f"{pct:+.1f}% declining"
            color = CLR.R
        else:
            verdict = f"{pct:+.1f}% weak"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"growth_pct": pct}}


class EarningsGrowth(BaseFundamentalIndicator):
    """
    Year-over-Year Earnings Growth.

    Same scoring scale as RevenueGrowth.
    """
    @property
    def name(self):
        return "EarningsGrowth"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("earningsGrowth")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        g = raw_value if raw_value is not None else self.compute(stock)
        if g is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        pct = g * 100
        
        # Continuous: -20% to 40%+ mapped to 0-100
        score = max(0, min(100, ((pct + 20) / 60.0) * 100.0))
        
        if pct >= 25:
            verdict = f"{pct:+.1f}% strong"
            color = CLR.G
        elif pct >= 10:
            verdict = f"{pct:+.1f}% good"
            color = CLR.G
        elif pct >= 0:
            verdict = f"{pct:+.1f}% flat"
            color = CLR.Y
        elif pct >= -10:
            verdict = f"{pct:+.1f}% declining"
            color = CLR.R
        else:
            verdict = f"{pct:+.1f}% weak"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"growth_pct": pct}}


class Beta(BaseFundamentalIndicator):
    """
    Stock Beta (5-year monthly).

    Score: beta=0.5 → 83, beta=1 → 67, beta=1.5 → 50, beta=2 → 33, beta=3+ → 0.
    Lower beta = less systematic risk = higher score.
    """
    @property
    def name(self):
        return "Beta"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("beta")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        b = raw_value if raw_value is not None else self.compute(stock)
        if b is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        # Continuous: Beta 0-3+ mapped to 100-0 (lower is better for conservative investors)
        score = max(0, min(100, 100.0 - (b / 3.0) * 100.0))
        
        if b < 0.7:
            verdict = f"low risk ({b:.3f})"
            color = CLR.G
        elif b < 1.0:
            verdict = f"moderate ({b:.3f})"
            color = CLR.Y
        elif b < 1.5:
            verdict = f"above average ({b:.3f})"
            color = CLR.R
        else:
            verdict = f"high risk ({b:.3f})"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"beta": b}}


class DividendYield(BaseFundamentalIndicator):
    """
    Dividend Yield.

    Score: 0% → 0, 2% → 42, 4% → 85, 6%+ → 100.
    Diminishing returns above 4% (yield traps possible).
    """
    @property
    def name(self):
        return "DividendYield"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("dividendYield")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        dy = raw_value if raw_value is not None else self.compute(stock)
        if dy is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        pct = dy * 100
        
        # Continuous: 0% to 6%+ mapped to 0-100 (diminishing returns above 4%)
        if pct <= 4:
            score = (pct / 4.0) * 85  # 0 to 85
        else:
            score = 85 + min(15, (pct - 4) / 2 * 15)  # 85 to 100 for 4-6%
        score = max(0, min(100, score))
        
        if pct >= 4:
            verdict = f"{pct:.3f}% high"
            color = CLR.G
        elif pct >= 2:
            verdict = f"{pct:.3f}% decent"
            color = CLR.G
        else:
            verdict = f"{pct:.3f}% low"
            color = CLR.Y
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"yield_pct": pct}}


class PayoutRatio(BaseFundamentalIndicator):
    """
    Dividend Payout Ratio.

    Score peaks at 30-60%: 30% → 70, 60% → 80, 90% → 40, 100%+ → 0.
    Very low (<30%) may indicate growth reinvestment; very high (>90%) is unsustainable.
    """
    @property
    def name(self):
        return "PayoutRatio"
    
    def compute(self, stock):
        return stock.metadata.get("fundamentals", {}).get("payoutRatio")
   
    def classify(self, stock, raw_value=None) -> Verdict:
        pr = raw_value if raw_value is not None else self.compute(stock)
        if pr is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        pct = pr * 100
        
        # Continuous: sweet spot at 30-60%, penalty for extremes
        if pr <= 0.3:
            score = 50 + (pr / 0.3) * 20  # 50 to 70
        elif pr <= 0.6:
            score = 70 + ((pr - 0.3) / 0.3) * 10  # 70 to 80
        elif pr <= 0.9:
            score = 80 - ((pr - 0.6) / 0.3) * 40  # 80 to 40
        else:
            score = max(0, 40 - ((pr - 0.9) / 0.5) * 40)  # 40 to 0
        
        if pr < 0.3:
            verdict = f"low growth ({pct:.1f}%)"
            color = CLR.G
        elif pr < 0.6:
            verdict = f"sustainable ({pct:.1f}%)"
            color = CLR.G
        elif pr < 0.9:
            verdict = f"high ({pct:.1f}%)"
            color = CLR.Y
        else:
            verdict = f"unsustainable ({pct:.1f}%)"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"payout": pr}}


class MarketCap(BaseFundamentalIndicator):
    """
    Market Capitalization (NSE Classification).

    Uses the metadata.capital field ('LARGE', 'MID', 'SMALL').
    Only applicable to EQUITY quoteType.
    Score: Large Cap = 75, Mid Cap = 50, Small Cap = 30.
    """
    @property
    def name(self):
        return "MarketCap"
    
    def compute(self, stock):
        # Only relevant for equities
        funda = stock.metadata.get("fundamentals", {})
        quote_type = funda.get("quoteType", "")
        if quote_type.upper() != "EQUITY":          # case-insensitive, just to be safe
            return None
        
        # 1st choice: explicit classification string at top level
        classification = stock.metadata.get("capital")
        if classification:
            return classification
        
        # 2nd choice: raw market cap from Yahoo Finance fundamentals
        market_cap = funda.get("marketCap")
        if market_cap is not None:
            return float(market_cap)                # numeric fallback
    
        return None                                 # give up
    
    def classify(self, stock, raw_value=None) -> Verdict:
        cap_data = raw_value if raw_value is not None else self.compute(stock)
        if cap_data is None:
            return {"verdict": "N/A", "score": 50, "color": CLR.DM, "result": {}}
        
        if isinstance(cap_data, str):
            cap_str = cap_data.upper()
            if "LARGE" in cap_str:
                return {"verdict": "Large Cap", "score": 50, "color": CLR.DM,
                        "result": {"classification": cap_data}}
            elif "MID" in cap_str:
                return {"verdict": "Mid Cap", "score": 50, "color": CLR.DM,
                        "result": {"classification": cap_data}}
            elif "SMALL" in cap_str:
                return {"verdict": "Small Cap", "score": 50, "color": CLR.DM,
                        "result": {"classification": cap_data}}
            else:
                return {"verdict": "N/A", "score": 50, "color": CLR.DM,
                        "result": {"classification": cap_data}}
        
        # Numeric fallback: classify by size but still neutral score
        if isinstance(cap_data, (int, float)):
            if cap_data >= 1e12:
                return {"verdict": "Mega/Large Cap", "score": 50, "color": CLR.DM,
                        "result": {"market_cap": cap_data}}
            elif cap_data >= 1e11:
                return {"verdict": "Large Cap", "score": 50, "color": CLR.DM,
                        "result": {"market_cap": cap_data}}
            elif cap_data >= 1e10:
                return {"verdict": "Mid Cap", "score": 50, "color": CLR.DM,
                        "result": {"market_cap": cap_data}}
            else:
                return {"verdict": "Small Cap", "score": 50, "color": CLR.DM,
                        "result": {"market_cap": cap_data}}
        
        return {"verdict": "N/A", "score": 50, "color": CLR.DM, "result": {}}


class AnalystRecommendation(BaseFundamentalIndicator):
    """
    Analyst Consensus Recommendation.

    Scale: 1.0 = Strong Buy, 5.0 = Sell.
    Score: 1.0 → 100, 2.5 → 62.5, 3.5 → 37.5, 5.0 → 0.
    """
    @property
    def name(self):
        return "AnalystRec"
    
    def compute(self, stock):
        fund = stock.metadata.get("fundamentals", {})
        return fund.get("recommendationMean"), fund.get("recommendationKey", "")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        rec_mean, rec_key = raw_value if raw_value is not None else self.compute(stock)
        if rec_mean is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        # Already continuous: 1.0 (Strong Buy) to 5.0 (Sell) mapped to 100-0
        score = max(0, min(100, 100.0 - (rec_mean - 1.0) / 4.0 * 100.0))
        
        if rec_mean <= 1.5:
            verdict = "STRONG BUY"
            color = CLR.G
        elif rec_mean <= 2.5:
            verdict = "BUY"
            color = CLR.G
        elif rec_mean <= 3.5:
            verdict = "HOLD"
            color = CLR.Y
        elif rec_mean <= 4.5:
            verdict = "UNDERPERFORM"
            color = CLR.R
        else:
            verdict = "SELL"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color,
                "result": {"mean": rec_mean, "key": rec_key}}


class TargetUpside(BaseFundamentalIndicator):
    """
    Analyst Target Price Upside/Downside.

    Formula: (targetMeanPrice / currentPrice - 1) * 100.
    Score: 60%+ upside → 100, 0% → 25, -20% → 0.
    """
    @property
    def name(self):
        return "TargetUpside"
    
    def compute(self, stock):
        fund = stock.metadata.get("fundamentals", {})
        target = fund.get("targetMeanPrice")
        price = fund.get("currentPrice")
        if target and price and price > 0:
            return (target / price - 1) * 100
        return None
    
    def classify(self, stock, raw_value=None) -> Verdict:
        upside = raw_value if raw_value is not None else self.compute(stock)
        if upside is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        # Continuous: -20% to 60%+ mapped to 0-100
        score = max(0, min(100, ((upside + 20) / 80.0) * 100.0))
        
        if upside >= 40:
            verdict = f"{upside:+.1f}% high upside"
            color = CLR.G
        elif upside >= 15:
            verdict = f"{upside:+.1f}% good upside"
            color = CLR.G
        elif upside >= 0:
            verdict = f"{upside:+.1f}% modest upside"
            color = CLR.Y
        else:
            verdict = f"{upside:+.1f}% above target"
            color = CLR.R
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"upside": upside}}


class EarningsDate(BaseFundamentalIndicator):
    """
    Days Until Next Earnings Report.

    Risk increases as the date approaches:
        - Already reported: score 70 (neutral).
        - Imminent (≤7d): score 10 (high risk).
        - 7-45d: linear from 10 to 75.
        - 45d+: score 75-100 (clear window).
    """
    @property
    def name(self):
        return "EarningsDate"
    
    def compute(self, stock):
        ts = stock.metadata.get("fundamentals", {}).get("earningsTimestampEnd")
        if ts is None:
            return None
        earnings_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        return (earnings_dt - now).days
    
    def classify(self, stock, raw_value=None) -> Verdict:
        days = raw_value if raw_value is not None else self.compute(stock)
        if days is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        # Continuous: most comfortable 45+ days out
        if days < 0:
            score = 70  # Already reported - neutral
            verdict = f"{-days}d ago (reported)"
            color = CLR.DM
        elif days <= 7:
            score = 10  # Imminent - high risk
            verdict = f"in {days}d -- IMMINENT"
            color = CLR.R
        elif days <= 45:
            score = 10 + ((days - 7) / 38) * 65  # 10 to 75
            verdict = f"in {days}d"
            color = CLR.Y if days <= 21 else CLR.G
        else:
            score = 75 + min(25, ((days - 45) / 45) * 25)  # 75 to 100
            verdict = f"in {days}d -- clear window"
            color = CLR.G
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"days": days}}


class ExDividendDate(BaseFundamentalIndicator):
    """
    Days Until Ex-Dividend Date.

    Scoring: already passed → 50, upcoming (≤7d) → 75, otherwise → 60.
    """
    @property
    def name(self):
        return "ExDividendDate"
    
    def compute(self, stock):
        ts = stock.metadata.get("fundamentals", {}).get("exDividendDate")
        if ts is None:
            return None
        ex_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        return (ex_dt - now).days
    
    def classify(self, stock, raw_value=None) -> Verdict:
        days = raw_value if raw_value is not None else self.compute(stock)
        if days is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        
        if days < 0:
            score = 50  # Already passed
            verdict = f"{-days}d ago"
            color = CLR.DM
        elif days <= 7:
            score = 75  # Upcoming - good for dividend capture
            verdict = f"in {days}d -- upcoming"
            color = CLR.G
        else:
            score = 60  # Not imminent
            verdict = f"in {days}d"
            color = CLR.Y
        
        return {"verdict": verdict, "score": int(round(score)), "color": color, "result": {"days": days}}


class FiftyTwoWeekPosition(BaseFundamentalIndicator):
    """
    Position Within 52-Week Range.

    Formula: range_pos = (price - low) / (high - low) * 100.
    Score peaks at 40% from the bottom (optimal balance of upside and support):
        - 0-40%: score 40-80 (value zone).
        - 40-70%: score 80-50 (extended but room to run).
        - 70-100%: score 50-20 (near highs - cautious).
    """
    @property
    def name(self):
        return "52Week"
    
    def compute(self, stock):
        fund = stock.metadata.get("fundamentals", {})
        return fund.get("fiftyTwoWeekHigh"), fund.get("fiftyTwoWeekLow"), fund.get("currentPrice")
    
    def classify(self, stock, raw_value=None) -> Verdict:
        hi, lo, px = raw_value if raw_value is not None else self.compute(stock)
        if hi is None or lo is None or px is None:
            return {"verdict": "N/A", "score": 0, "color": CLR.DM, "result": {}}
        hi_pct = (px / hi - 1) * 100
        lo_pct = (px / lo - 1) * 100
        
        # Continuous: where price sits in the 52-week range
        if hi > lo:
            range_pos = (px - lo) / (hi - lo) * 100
        else:
            range_pos = 50
        
        # Optimal: middle of range (not too high, not too low)
        # Score peaks at 40% from bottom (60% from top)
        if range_pos <= 40:
            score = 40 + (range_pos / 40) * 40  # 40 to 80
        elif range_pos <= 70:
            score = 80 - ((range_pos - 40) / 30) * 30  # 80 to 50
        else:
            score = 50 - ((range_pos - 70) / 30) * 30  # 50 to 20
        
        verdict = f"High: {hi_pct:+.1f}% | Low: {lo_pct:+.1f}% | Range: {range_pos:.0f}%"
        return {"verdict": verdict, "score": int(round(score)), "color": CLR.W,
                "result": {"hi": hi, "lo": lo, "hi_pct": hi_pct, "lo_pct": lo_pct, "range_pos": range_pos}}


# Shared, stateless instances - see TECHNICAL_INDICATORS above for rationale.
FUNDAMENTAL_INDICATORS: List[BaseFundamentalIndicator] = [cls() for cls in BaseFundamentalIndicator._registry]


# =============================================================================
# 4.  STOCK - Main data container
# =============================================================================
class Stock:
    """
    Represents a single stock with all its data and computed indicators.
    
    Indicators and fundamentals are lazily computed and cached.
    Pre-computation can be controlled via PreComputeMode.
    """
    def __init__(self, symbol: str, candles: List[Candle],
                 metadata: dict = None,
                 indicators: Optional[List[BaseTechnicalIndicator]] = None,
                 filepath: Optional[str] = None):
        self.symbol = symbol
        self.candles = candles
        self.candleCount = len(self.candles)
        self.metadata = metadata or {}
        self.filepath = filepath

        # Pre-compute price arrays once - avoids repeated list comprehensions
        self.opens  = [c.open  for c in self.candles]
        self.highs  = [c.high  for c in self.candles]
        self.lows   = [c.low   for c in self.candles]
        self.closes = [c.close for c in self.candles]
        self.volumes = [c.volume for c in self.candles]

        # Shared, stateless instances (see TECHNICAL_INDICATORS /
        # FUNDAMENTAL_INDICATORS / SCORE_FACTORS module-level definitions) -
        # every Stock references the same objects rather than instantiating
        # its own copies; only per-stock *results* are cached below.
        self.indicators = indicators or TECHNICAL_INDICATORS
        # Lazy cache: key = indicator name, value = computed result
        self._indicator_cache: Dict[str, Any] = {}

        self.fundamentals = FUNDAMENTAL_INDICATORS
        self._fundamental_raw_cache: Dict[str, Any] = {}
        self._fundamental_cache: Dict[str, Any] = {}

        self.scoreFactors = SCORE_FACTORS
        self._scoreFactor_cache: Dict[str, Any] = {}

    @staticmethod
    def _load_single_stock(filepath: str,
                        precompute_mode: PreComputeMode = PreComputeMode.PCM_ALL,
                        from_date: Optional[datetime] = None,
                        to_date: Optional[datetime] = None) -> Optional["Stock"]:
        try:
            stock = StockFactory.from_json_file(filepath, from_date=from_date, to_date=to_date)
            if stock:
                stock.precompute(mode=precompute_mode)
            return stock
        except Exception as e:
            print(f"ERROR loading {os.path.basename(filepath)}: {e}")
            return None

    def get_indicator(self, name: str) -> Any:
        """Retrieve an indicator series, computing and caching if needed."""
        if name in self._indicator_cache:
            return self._indicator_cache[name]

        # Find the indicator instance
        ind = next((i for i in self.indicators if i.name == name), None)
        if ind is None:
            raise KeyError(f"Indicator '{name}' not found in stock {self.symbol}")

        # Compute and cache
        result = ind.compute(self)
        self._indicator_cache[name] = result
        return result
        
    def get_indicator_value(self, name: str, key: Optional[str] = None) -> Optional[float]:
        """
        Safely extract a single value from an indicator.
        
        For list-type indicators (SMA, RSI, MFI, etc.):
        returns the last element.
        For dict-type indicators (MACD, ATR, ADX, etc.): specify the key to extract.
        
        Examples:
            stock.get_indicator_value("SMA(20)")           # returns last SMA value
            stock.get_indicator_value("MACD", "macd")      # returns last MACD line value
            stock.get_indicator_value("ATR(14)", "atr_vals") # returns last ATR value
        """
        try:
            data = self.get_indicator(name)
            
            if key is None:
                # Simple list indicator - return last value
                if isinstance(data, list):
                    if len(data) > 0:
                        return data[-1]
                    else:
                        print(f"[WARN] {self.symbol}: Indicator '{name}' returned empty list")
                        return None
                else:
                    print(f"[WARN] {self.symbol}: Indicator '{name}' returned unexpected type {type(data).__name__}")
                    return None
            else:
                # Dict indicator with specific key
                if isinstance(data, dict):
                    if key not in data:
                        print(f"[WARN] {self.symbol}: Indicator '{name}' dict missing key '{key}'. Available: {list(data.keys())}")
                        return None
                    series = data[key]
                    if isinstance(series, list) and len(series) > 0:
                        return series[-1]
                    else:
                        print(f"[WARN] {self.symbol}: Indicator '{name}[\"{key}\"]' is not a valid list")
                        return None
                else:
                    print(f"[WARN] {self.symbol}: Indicator '{name}' returned {type(data).__name__}, expected dict")
                    return None
                    
        except KeyError as e:
            print(f"[ERROR] {self.symbol}: KeyError accessing indicator '{name}' key '{key}': {e}")
            return None
        except IndexError as e:
            print(f"[ERROR] {self.symbol}: IndexError accessing indicator '{name}': {e}")
            return None
        except TypeError as e:
            print(f"[ERROR] {self.symbol}: TypeError accessing indicator '{name}': {e}")
            return None
        except Exception as e:
            print(f"[ERROR] {self.symbol}: Unexpected error accessing indicator '{name}': {type(e).__name__}: {e}")
            return None

    def get_fundamental_raw(self, name: str) -> Any:
        """Get raw fundamental value (compute only, no classify)."""
        if name in self._fundamental_raw_cache:
            return self._fundamental_raw_cache[name]
        for f in self.fundamentals:
            if f.name == name:
                raw_val = f.compute(self)
                self._fundamental_raw_cache[name] = raw_val
                return raw_val
        raise KeyError(name)

    def get_fundamental(self, name: str):
        """Get (raw_value, verdict) tuple for a fundamental."""
        if name in self._fundamental_cache:
            return self._fundamental_cache[name]
        raw_val = self.get_fundamental_raw(name)
        for f in self.fundamentals:
            if f.name == name:
                verdict = f.classify(self, raw_value=raw_val)
                self._fundamental_cache[name] = (raw_val, verdict)
                return (raw_val, verdict)
        raise KeyError(name)

    def get_fundamental_value(self, name: str, default="N/A") -> Any:
        """Return raw fundamental value without running classify()."""
        try:
            raw = self.get_fundamental_raw(name)
            return raw if raw is not None else default
        except KeyError:
            return default

    def get_fundamental_verdict(self, name: str, default="N/A") -> str:
        """Return verdict string only."""
        try:
            _, v = self.get_fundamental(name)
            return v.get("verdict", default)
        except KeyError:
            return default

    def get_score_value(self, factor_name: str) -> Optional[float]:
        """
        Run a single scoring factor for this stock and return its raw score.
        Used by ScoreTable to display individual factor values.
        """
        for factor in self.scoreFactors:
            if factor.name == factor_name:
                try:
                    return factor.score(self)
                except Exception:
                    return None
        return None
    
    def gate_flags(self, index: int = -1) -> Dict[str, bool]:
        """
        Compute display-only gate flags for ranking table.
        Does NOT affect scores - informational only.

            gate_mom   : 20d return > 0
            gate_trend : price > SMA20 or oversold recovery
            gate_adx   : ADX > 20 or RSI < 30
            gate_mfi   : MFI <= 80
            gate_ichi  : price above Ichimoku cloud
        """
        c   = self.closes
        n   = len(c)
        idx = _norm_index(index, n)
        if idx < 0 or idx >= n:
            return {"gate_mom": False, "gate_trend": False, "gate_adx": False,
                    "gate_mfi": False, "gate_ichi": False, "gate_bb_ttm": False}

        m20       = _rolling_ret(c, 20, idx)
        gate_mom  = m20 is not None and m20 > 0

        s20   = _sma_val(c, 20, idx)
        px    = c[idx]
        try:
            rsi_v = self.get_indicator("RSI(14)")[idx] or 50.0
        except (KeyError, IndexError, TypeError):
            rsi_v = 50.0
        near_sma20        = s20 is not None and px > s20 * 0.97
        oversold_recovery = rsi_v < 30 and idx >= 3 and c[idx] > c[idx - 3]
        gate_trend        = bool((s20 and px > s20) or oversold_recovery or near_sma20)

        gate_adx = rsi_v < 30
        try:
            gate_adx = gate_adx or (self.get_indicator("ADX(14)")["adx"][idx] or 0) > 20
        except (KeyError, IndexError, TypeError):
            pass

        gate_mfi = True
        try:
            mfi_v    = self.get_indicator("MFI(14)")[idx]
            gate_mfi = mfi_v is None or mfi_v <= 80
        except (KeyError, IndexError, TypeError):
            pass

        gate_ichi = False
        try:
            cloud_pos = self.get_indicator("Ichimoku").get("cloud_pos", [])
            pos = cloud_pos[idx] if isinstance(cloud_pos, list) and idx < len(cloud_pos) else str(cloud_pos)
            gate_ichi = pos is not None and "Above" in pos
        except (KeyError, TypeError):
            pass

        gate_bb_squeeze = False
        try:
            ttm = self.get_indicator("TTM_Squeeze")
            squeeze_series = ttm["squeeze"]
            gate_bb_squeeze = bool(squeeze_series[idx]) if idx < len(squeeze_series) else False
        except (KeyError, IndexError, TypeError):
            pass

        return {
            "gate_mom":   gate_mom,
            "gate_trend": gate_trend,
            "gate_adx":   gate_adx,
            "gate_mfi":   gate_mfi,
            "gate_ichi":  gate_ichi,
            "gate_bb_ttm": gate_bb_squeeze,
        }

    def precompute(self, mode: PreComputeMode = PreComputeMode.PCM_ALL) -> None:
        """
        Eagerly compute and cache indicators and/or fundamental raw values.
        """
        if mode in (PreComputeMode.PCM_ALL, PreComputeMode.PCM_TECHNICAL):
            for ind in self.indicators:
                try:
                    self.get_indicator(ind.name)
                except Exception as e:
                    _log_swallowed(f"precompute_indicator:{ind.name}", self.symbol, e)

        if mode in (PreComputeMode.PCM_ALL, PreComputeMode.PCM_FUNDAMENTAL):
            for f in self.fundamentals:
                try:
                    self.get_fundamental_raw(f.name)
                except Exception as e:
                    _log_swallowed(f"precompute_fundamental:{f.name}", self.symbol, e)

    def __repr__(self):
        return f"Stock({self.symbol!r}, candles={len(self.candles)})"


# =============================================================================
# 5.  STOCK FACTORY
# =============================================================================
class StockFactory:
    """Creates Stock instances from JSON files."""
    @staticmethod
    def from_json_file(filepath: str,
                       from_date: Optional[datetime] = None,
                       to_date: Optional[datetime] = None) -> Stock:
        with open(filepath, 'r') as f:
            raw = json.load(f)
        # Symbol resolution: prefer file name, fallback to metadata without suffix
        file_sym = os.path.splitext(os.path.basename(filepath))[0].upper()
        metadata = raw.get("metadata", {})
        meta_sym = (metadata.get("fundamentals", {}).get("symbol", "") or "").split(".")[0].upper() #yfin {"symbol": "ITC.NS"}
        symbol = file_sym or meta_sym or "UNKNOWN"
        candles = []
        for item in raw.get("data", []):
            ts = datetime.fromisoformat(item["date"])
            # --- Date range filter ---
            if from_date and ts < from_date:
                continue
            if to_date and ts > to_date:
                continue
            # -------------------------
            candle = Candle(
                symbol=symbol,
                timestamp=ts,
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=float(item["volume"]),
                open_interest=float(item.get("oi", 0)) if "oi" in item else None
            )
            candles.append(candle)
        candles.sort(key=lambda c: c.timestamp)
        if len(candles) == 0:
            # possibly no canles in date range !
            return None
        return Stock(symbol=symbol, candles=candles, metadata=metadata, filepath=filepath)


# =============================================================================
# TABLE RENDERING HELPERS
# =============================================================================

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences for accurate column width calculation."""
    return re.sub(r'\033\[[0-9;]*m', '', str(text))

def _print_table(headers: List[str], rows: List[List[str]]) -> None:
    """Print a formatted table with ANSI-color-safe column alignment."""
    col_widths = [
        max(len(_strip_ansi(str(h))), 
            max((len(_strip_ansi(str(r[i]))) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    sep = "-+-".join("-" * w for w in col_widths)
    
    def fmt_row(vals):
        parts = []
        for i, v in enumerate(vals):
            v_str = str(v)
            # Pad based on visible length, not raw length
            visible_len = len(_strip_ansi(v_str))
            padding = col_widths[i] - visible_len
            parts.append(v_str + " " * padding)
        return " | ".join(parts) + " |"
    
    print(sep)
    print(fmt_row(headers))
    print(sep)
    for r in rows:
        print(fmt_row(r))
    print(sep)

def _fmt_val(v: Any) -> str:
    """Format a value for display."""
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.2f}" if abs(v) < 1 else f"{v:,.2f}"
    return str(v)

def _format_cap(cap) -> str:
    """Format market cap in readable units."""
    if cap is None:
        return "N/A"
    if cap >= 1e12:
        return f"Rs.{cap/1e12:.3f}T"
    if cap >= 1e9:  return f"Rs.{cap/1e9:.3f}B"
    if cap >= 1e6:  return f"Rs.{cap/1e6:.3f}M"
    return f"Rs.{cap:.0f}"

def _fmt_pct(val) -> str:
    """Format a decimal as percentage."""
    if val is None or val == "N/A":
        return "N/A"
    return f"{val*100:.1f}%" if isinstance(val, float) else str(val)

def _fmt_pct_signed(val) -> str:
    """Format a decimal as signed percentage."""
    if val is None or val == "N/A":
        return "N/A"
    return f"{val*100:+.1f}%" if isinstance(val, float) else str(val)

def _fmt_float2(val) -> str:
    """Format a float with 2 decimal places and sign."""
    if val is None or val == "N/A":
        return "N/A"
    return f"{val:+.2f}%" if isinstance(val, float) else str(val)

# =============================================================================
# 6. DISPLAY TABLES
# =============================================================================
class IndicatorTable:
    """Technical indicator summary table."""
    DEFAULT_INDICATORS = [
        ("Close",      None,         lambda stock: stock.closes[-1]),
        ("SMA(20)",    "SMA(20)",    lambda stock: stock.get_indicator_value("SMA(20)")),
        ("RSI(14)",    "RSI(14)",    lambda stock: stock.get_indicator_value("RSI(14)")),
        ("MACD Line",  "MACD",       lambda stock: stock.get_indicator_value("MACD", "macd")),
        ("MACD Signal","MACD",       lambda stock: stock.get_indicator_value("MACD", "signal")),
        ("MACD Hist",  "MACD",       lambda stock: stock.get_indicator_value("MACD", "histogram")),
        ("BB Upper",   "BB(20,2.0)", lambda stock: stock.get_indicator_value("BB(20,2.0)", "upper")),
        ("BB Lower",   "BB(20,2.0)", lambda stock: stock.get_indicator_value("BB(20,2.0)", "lower")),
        ("KC Upper",   "KC(20,14,1.5)", lambda stock: stock.get_indicator_value("KC(20,14,1.5)", "upper")),
        ("KC Lower",   "KC(20,14,1.5)", lambda stock: stock.get_indicator_value("KC(20,14,1.5)", "lower")),
        ("ATR(14)",    "ATR(14)",    lambda stock: stock.get_indicator_value("ATR(14)", "atr_vals")),
        ("ADX",        "ADX(14)",    lambda stock: stock.get_indicator_value("ADX(14)", "adx")),
        ("+DI",        "ADX(14)",    lambda stock: stock.get_indicator_value("ADX(14)", "+di")),
        ("-DI",        "ADX(14)",    lambda stock: stock.get_indicator_value("ADX(14)", "-di")),
        ("Ret(5)%",    "Ret(5)",     lambda stock: stock.get_indicator_value("Ret(5)", "vals")),
        ("Vol(20)%",   "AnnVol(20)", lambda stock: stock.get_indicator_value("AnnVol(20)", "vals")),
        ("Candle",     "CandleScore",lambda stock: stock.get_indicator_value("CandleScore", "vals")),
        ("%K",         "Stoch(14,3,3)", lambda stock: stock.get_indicator_value("Stoch(14,3,3)", "k")),
        ("%D",         "Stoch(14,3,3)", lambda stock: stock.get_indicator_value("Stoch(14,3,3)", "d")),
        ("HA",         "HeikinAshi", lambda stock: stock.get_indicator_value("HeikinAshi", "streak")),
    ]

    @classmethod
    def print_summary(cls, stocks: List[Stock], indicators=None) -> None:
        if indicators is None:
            indicators = cls.DEFAULT_INDICATORS

        headers = ["Symbol"] + [name for name, _, _ in indicators]
        rows = []

        for stock in stocks:
            row = [stock.symbol]
            for header_name, _, extractor in indicators:
                try:
                    val = extractor(stock)
                except (IndexError, TypeError, KeyError) as e:
                    val = None
                    _log_swallowed(f"indicator_table:{header_name}", stock.symbol, e)
                if val is None:
                    cell = " N/A "
                elif isinstance(val, float):
                    cell = f"{val:.3f}"
                else:
                    cell = str(val)
                row.append(cell)
            rows.append(row)
        _print_table(headers, rows)


class FundamentalTable:
    """Fundamental metrics summary table."""
    DEFAULT_FUNDAMENTALS = [
        ("Trailing P/E",      "TrailingPE",    lambda s,n: s.get_fundamental_value(n)),
        ("Forward P/E",       "ForwardPE",     lambda s,n: s.get_fundamental_value(n)),
        ("P/B",               "P/B",           lambda s,n: s.get_fundamental_value(n)),
        ("PEG",               "PEG",           lambda s,n: s.get_fundamental_value(n)),
        ("ROE %",             "ROE",           lambda s,n: _fmt_pct(s.get_fundamental_value(n))),
        ("Profit Margin %",   "ProfitMargin",  lambda s,n: _fmt_float2(s.get_fundamental_value(n))),
        ("Rev Growth %",      "RevenueGrowth", lambda s,n: _fmt_pct_signed(s.get_fundamental_value(n))),
        ("Earn Growth %",     "EarningsGrowth",lambda s,n: s.get_fundamental_value(n)),
        ("Beta",              "Beta",          lambda s,n: s.get_fundamental_value(n)),
        ("Div Yield %",       "DividendYield", lambda s,n: s.get_fundamental_value(n)),
        ("Payout Ratio %",    "PayoutRatio",   lambda s,n: s.get_fundamental_value(n)),
        ("Market Cap",        "MarketCap",     lambda s,n: s.get_fundamental_verdict(n)),
        ("Analyst Rec",       "AnalystRec",    lambda s,n: s.get_fundamental_verdict(n)),
        ("Target Upside %",   "TargetUpside",  lambda s,n: _fmt_float2(s.get_fundamental_value(n))),
        ("Earnings Date",     "EarningsDate",  lambda s,n: s.get_fundamental_verdict(n)),
        ("Ex-Div Date",       "ExDividendDate",lambda s,n: s.get_fundamental_verdict(n)),
    ]


    @classmethod
    def print_summary(cls, stocks: List[Stock], fundamentals=None) -> None:
        if fundamentals is None:
            fundamentals = cls.DEFAULT_FUNDAMENTALS

        headers = ["Symbol"] + [h for h, _, _ in fundamentals]
        rows = []

        for stock in stocks:
            row = [stock.symbol]
            for _, name, fmt in fundamentals:
                try:
                    cell = fmt(stock, name)
                except Exception as e:
                    cell = "N/A"
                    _log_swallowed(f"fundamental_table:{name}", stock.symbol, e)
                row.append(str(cell))
            rows.append(row)
        _print_table(headers, rows)

class ScoreTable:
    """Scoring factor summary table — shows raw factor scores for each stock."""
    
    DEFAULT_SCORES = [
        ("Momentum 20",    "momentum_20",     lambda s, n: s.get_score_value(n)),
        ("Trend MA",       "trend_ma",        lambda s, n: s.get_score_value(n)),
        ("RSI Quality",    "rsi_quality",     lambda s, n: s.get_score_value(n)),
        ("Sharpe 20",      "sharpe_20",       lambda s, n: s.get_score_value(n)),
        ("Volume Trend",   "volume_trend",    lambda s, n: s.get_score_value(n)),
        ("BB Entry",       "bb_entry",        lambda s, n: s.get_score_value(n)),
        ("Breakout",       "breakout",        lambda s, n: s.get_score_value(n)),
        ("Pullback Entry", "pullback_entry",  lambda s, n: s.get_score_value(n)),
        ("Crossover",      "crossover",       lambda s, n: s.get_score_value(n)),
        ("Overextension",  "overextension",   lambda s, n: s.get_score_value(n)),
        ("Pivot Proximity","pivot_proximity", lambda s, n: s.get_score_value(n)),
        ("Fib Retrace",    "fib_retrace",     lambda s, n: s.get_score_value(n)),
        ("Gate Strength",  "gate_strength",   lambda s, n: s.get_score_value(n)),
        ("Bullish Setup",  "bullish_setup",   lambda s, n: s.get_score_value(n)),
    ]

    @classmethod
    def print_summary(cls, stocks: List[Stock], scores=None) -> None:
        if scores is None:
            scores = cls.DEFAULT_SCORES

        headers = ["Symbol"] + [h for h, _, _ in scores]
        rows = []

        for stock in stocks:
            row = [stock.symbol]
            for _, name, fmt in scores:
                try:
                    val = fmt(stock, name)
                    if val is None:
                        cell = " N/A "
                    elif isinstance(val, float):
                        cell = f"{val:.1f}"  # Scores are 0-100, not percentages
                    else:
                        cell = str(val)
                except Exception as e:
                    cell = "N/A"
                    _log_swallowed(f"score_table:{name}", stock.symbol, e)
                row.append(cell)
            rows.append(row)
        _print_table(headers, rows)

# =============================================================================
# SCORING ENGINE  - portfolio_bot-style cross-section normalised ranking
# =============================================================================
# Architecture
# -----------------------------------------------------------------------------
# BaseScoreFactor.score(stock, index) → Optional[float]  (raw, un-normalised)
#   Derived composite scoring functions that combine raw indicator values into
#   a single float optimised for cross-section ranking.  Unlike Indicators
#   (which classify for display), Factors exist solely for ranking.
#
# xnorm(vals) → Dict[str, float]
#   Min-max scale a {symbol: raw_score} dict to [0, 100] across the universe.
#   Stocks with None scores get 50 (neutral).  Identical values all get 50.
#
# BonusComputer.compute(stock, index) → float
#   Candlestick + Ichimoku bonus + peak-distance penalty.
#   Added to the TA composite *after* normalisation (never normalised itself).
#
# Stonks.rank_stocks_xnorm(...)
#   Two-pass cross-section normalised ranking.
#   Pass 1 : collect raw factor scores across the universe.
#   Normalise: xnorm each factor column.
#   Pass 2 : weighted TA composite per stock.
#   Blend   : (1-fw)*TA + fw*FA.
# =============================================================================

FACTOR_WEIGHTS: Dict[str, float] = {
    "momentum_20":     0.0,   # KILLED - predicts reversal
    "sharpe_20":       0.0,   # KILLED - high sharpe = recent strength = reversal
    "breakout":        0.0,   # KILLED - breakout already happened
    "trend_ma":        0.10,
    "rsi_quality":     0.10,
    "crossover":       0.10,
    "volume_trend":    0.05,
    "bb_entry":        0.15,
    "pullback_entry":  0.15,
    "overextension":   0.10,
    "pivot_proximity": 0.05,
    "fib_retrace":     0.05,
    "bullish_setup":   0.10,
    "gate_strength":   0.05,
    "stoch_entry":     0.0,   # NEW (Phase 7) - awaiting backtest A/B before earning weight
    "ha_reversal":     0.0,   # NEW (Phase 7) - awaiting backtest A/B before earning weight
}
# Sum of active weights = 1.00

# Default fundamental sub-category weights (sum = 1.0)
FUNDAMENTAL_SUB_WEIGHTS: Dict[str, float] = {
    "f_valuation":     0.25,
    "f_quality":       0.25,
    "f_growth":        0.20,
    "f_analyst":       0.15,
    "f_eps_momentum":  0.10,
    "f_earnings_risk": 0.03,
    "f_ownership":     0.02,
}

# Fundamental sub-category -> member metrics + intra-group weights
FUNDAMENTAL_GROUPS: Dict[str, List[Tuple[str, float]]] = {
    "f_valuation":     [("TrailingPE", 0.3), ("ForwardPE", 0.4),
                        ("P/B", 0.2), ("PEG", 0.1)],
    "f_quality":       [("ROE", 0.5), ("ProfitMargin", 0.5)],
    "f_growth":        [("RevenueGrowth", 0.4), ("EarningsGrowth", 0.6)],
    "f_analyst":       [("AnalystRec", 0.4), ("TargetUpside", 0.6)],
    "f_eps_momentum":  [("ForwardPE", 1.0)],
    "f_earnings_risk": [("EarningsDate", 1.0)],
    "f_ownership":     [("Beta", 1.0)],
}


def _renormalize_weights(weights: Dict[str, float], label: str, source: str) -> Dict[str, float]:
    """Renormalize a weight dict to sum to 1.0, printing an info line if it
    didn't already. Raises ValueError if the weights sum to <= 0."""
    total = sum(weights.values())
    if total <= 0:
        raise ValueError(f"{label} in {source} sum to {total} (must be > 0)")
    if abs(total - 1.0) > 1e-9:
        print(f"  [weights] {label} in {source} sum to {total:.4f} - renormalising to 1.0")
        return {k: v / total for k, v in weights.items()}
    return weights


def load_weights_file(path: str) -> Tuple[Dict[str, float], Dict[str, float], Optional[float]]:
    """
    Load factor_weights / fundamental_sub_weights / blend.fund_weight
    overrides from a TOML file (see weights.example.toml). Returns the
    FULL weight dicts (built-in defaults merged with the file's overrides)
    plus fund_weight (None if the file has no [blend] section, in which
    case the caller keeps whatever fund_weight it already has).

    Any key under [factor_weights] or [fundamental_sub_weights] that isn't
    one of the built-in FACTOR_WEIGHTS/FUNDAMENTAL_SUB_WEIGHTS names is a
    hard error - a typo'd factor name should not be silently ignored (it
    would otherwise just never contribute to the ranking, indistinguishable
    from an intentional weight of 0). Each section is renormalised to sum
    to 1.0 if the file's overrides don't already add up.
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)

    factor_weights = dict(FACTOR_WEIGHTS)
    file_factor_weights = data.get("factor_weights", {})
    unknown = sorted(set(file_factor_weights) - set(FACTOR_WEIGHTS))
    if unknown:
        raise ValueError(
            f"Unknown factor_weights key(s) in {path}: {unknown}. "
            f"Valid keys: {sorted(FACTOR_WEIGHTS)}"
        )
    factor_weights.update(file_factor_weights)
    factor_weights = _renormalize_weights(factor_weights, "factor_weights", path)

    fundamental_sub_weights = dict(FUNDAMENTAL_SUB_WEIGHTS)
    file_sub_weights = data.get("fundamental_sub_weights", {})
    unknown_sub = sorted(set(file_sub_weights) - set(FUNDAMENTAL_SUB_WEIGHTS))
    if unknown_sub:
        raise ValueError(
            f"Unknown fundamental_sub_weights key(s) in {path}: {unknown_sub}. "
            f"Valid keys: {sorted(FUNDAMENTAL_SUB_WEIGHTS)}"
        )
    fundamental_sub_weights.update(file_sub_weights)
    fundamental_sub_weights = _renormalize_weights(fundamental_sub_weights, "fundamental_sub_weights", path)

    fund_weight = data.get("blend", {}).get("fund_weight")
    return factor_weights, fundamental_sub_weights, fund_weight


# ---------------------------------------------------------------------------
# Cross-section normalisation
# ---------------------------------------------------------------------------
def xnorm(vals: Dict[str, Optional[float]]) -> Dict[str, float]:
    """
    Percentile-rank scale {symbol: raw_value} to [0, 100] across the
    universe. None maps to 50 (neutral) - callers that need to distinguish
    "genuinely average" from "no data" must check the raw value before
    calling xnorm(), not rely on its output alone (see rank_stocks_xnorm's
    per-stock weight renormalization for missing factors).

    Uses fractional (average) ranking: tied values share the average rank
    of their group, so a cluster of identical values doesn't arbitrarily
    favor one member by insertion order. Unlike min-max scaling, a single
    extreme outlier can only ever claim the top (or bottom) rank slot - it
    cannot compress the spacing between every other value the way min-max
    does, which is what previously required a separate winsorize() step
    for fundamentals; that step is now redundant and has been removed.
    """
    valid = {k: v for k, v in vals.items() if v is not None}
    n = len(valid)
    if n <= 1:
        return {k: 50.0 for k in vals}

    sorted_syms = sorted(valid, key=lambda k: valid[k])
    ranks: Dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and valid[sorted_syms[j + 1]] == valid[sorted_syms[i]]:
            j += 1
        avg_rank = (i + j) / 2.0  # 0-based average rank for the tied group
        for k in range(i, j + 1):
            ranks[sorted_syms[k]] = avg_rank
        i = j + 1

    return {
        k: (50.0 if v is None else ranks[k] / (n - 1) * 100.0)
        for k, v in vals.items()
    }


def _weighted_composite(sym: str, weights: Dict[str, float],
                         raw: Dict[str, Dict[str, Optional[float]]],
                         normed: Dict[str, Dict[str, float]],
                         use_raw: bool) -> Tuple[float, float]:
    """
    Weighted average of per-factor (or per-fundamental-sub-category) scores
    for one stock, counting only members that have real (non-None) raw
    data for this stock. Missing members are excluded rather than defaulted
    to a neutral 50, and the remaining weights are renormalized so they
    still sum to 1 - the same pattern _fund_sub_scores already uses one
    level down, applied here across the top-level composite.

    Returns (composite, coverage), where coverage is the fraction of this
    stock's total possible weight that was backed by real data (1.0 =
    everything present, 0.0 = nothing) - used by rank_stocks_xnorm to flag
    thin-coverage stocks in the ranking table instead of silently ranking
    them on mostly-neutral filler.
    """
    total_weight = sum(weights.values()) or 1.0
    available = {fname: w for fname, w in weights.items() if raw[fname].get(sym) is not None}
    available_weight = sum(available.values())
    if available_weight <= 0:
        return 50.0, 0.0
    composite = sum(
        (w / available_weight) * (raw[fname][sym] if use_raw else normed[fname].get(sym, 50.0))
        for fname, w in available.items()
    )
    return composite, available_weight / total_weight


# ---------------------------------------------------------------------------
# Shared helpers used by factor implementations
# ---------------------------------------------------------------------------
def _sma_val(closes: list, period: int, index: int) -> Optional[float]:
    """Simple moving average at index (handles negative indices)."""
    idx = _norm_index(index, len(closes))
    if idx < period - 1:
        return None
    return sum(closes[idx - period + 1: idx + 1]) / period

def _rolling_ret(closes: list, period: int, index: int) -> Optional[float]:
    """(closes[index] / closes[index-period] - 1) * 100, or None."""
    idx = _norm_index(index, len(closes))
    if idx < period or closes[idx - period] == 0:
        return None
    return (closes[idx] / closes[idx - period] - 1.0) * 100.0

def _ann_vol(closes: list, period: int, index: int) -> Optional[float]:
    """Annualised daily-return volatility over period bars ending at index."""
    idx = _norm_index(index, len(closes))
    if idx < period:
        return None
    rets = [(closes[i] / closes[i - 1] - 1.0)
            for i in range(idx - period + 1, idx + 1)
            if closes[i - 1] != 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var  = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 252) * 100.0


# ---------------------------------------------------------------------------
# Base Factor
# ---------------------------------------------------------------------------
class BaseScoreFactor(ABC):
    """Abstract scoring factor for cross-section ranking."""
    _registry: List[Type["BaseScoreFactor"]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseScoreFactor._registry.append(cls)

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique factor name, e.g. 'momentum_20'."""
        pass

    @abstractmethod
    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        """Compute the raw factor value for one stock at the given candle index.
           Return None if not enough data."""
        pass

# ---------------------------------------------------------------------------
# 13 Factor implementations
# ---------------------------------------------------------------------------

class Momentum20Factor(BaseScoreFactor):
    """20-day momentum acceleration."""
    @property
    def name(self):
        return "momentum_20"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        c   = stock.closes
        n   = len(c)
        idx = index if index >= 0 else n + index
        if idx < 20:
            return None
        r_recent = _rolling_ret(c, 10, idx)
        r_prior  = _rolling_ret(c, 10, idx - 10)
        if r_recent is None or r_prior is None:
            return None
        return r_recent - r_prior


# class Momentum60Factor(BaseScoreFactor):
#     """60d momentum acceleration: recent 30d return minus prior 30d return."""
#     @property
#     def name(self):
#         return "momentum_60"
# 
#     def score(self, stock: Stock, index: int = -1) -> Optional[float]:
#         c   = stock.closes
#         n   = len(c)
#         idx = index if index >= 0 else n + index
#         if idx < 60:
#             return _rolling_ret(c, 60, idx)  # fallback to raw 60d return
#         r_recent = _rolling_ret(c, 30, idx)
#         r_prior  = _rolling_ret(c, 30, idx - 30)
#         if r_recent is None or r_prior is None:
#             return None
#         return r_recent - r_prior


class TrendMAFactor(BaseScoreFactor):
    """Fraction of SMA20/50/200 that price is above, scaled 0-100."""
    @property
    def name(self):
        return "trend_ma"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        c   = stock.closes
        n   = len(c)
        idx = index if index >= 0 else n + index
        px  = c[idx]
        s20  = _sma_val(c, 20,  idx)
        s50  = _sma_val(c, 50,  idx)
        s200 = _sma_val(c, 200, idx)
        hits  = sum([1 if s20  and px > s20  else 0,
                     1 if s50  and px > s50  else 0,
                     1 if s200 and px > s200 else 0])
        denom = 1 + (1 if s50 else 0) + (1 if s200 else 0)
        return hits / denom * 100.0


class RSIQualityFactor(BaseScoreFactor):
    """
    Continuous RSI scoring using a sigmoid (S-curve) function.

    The sigmoid is centered at a market-cap-adjusted midpoint:
      - Small caps:  mid=50,  steepness=0.12  (wider acceptance)
      - Large caps:  mid=45,  steepness=0.15  (tighter acceptance)
      - Default:     mid=47,  steepness=0.13

    Score ranges smoothly from ~100 (deeply oversold) to ~0 (extremely
    overbought). No piecewise boundaries — a small RSI change always
    produces a proportional score change.

    Formula:  score = 100 / (1 + exp((RSI - midpoint) * steepness))
    """
    @property
    def name(self):
        return "rsi_quality"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        try:
            rsi_s = stock.get_indicator("RSI(14)")
            rsi_v = rsi_s[index] if rsi_s[index] is not None else 50.0
            
            # Get market cap for adjusted thresholds
            cap = stock.metadata.get("capital", "").upper()
            if "SMALL" in cap:
                sweet_mid = 50  # Center of sweet spot for small caps
                steepness = 0.12
            elif "LARGE" in cap:
                sweet_mid = 45  # Center for large caps
                steepness = 0.15
            else:
                sweet_mid = 47
                steepness = 0.13
            
            # Sigmoid: smooth S-curve centered at sweet_mid
            # Score 100 when RSI << sweet_mid, Score 0 when RSI >> sweet_mid
            raw = 100.0 / (1.0 + math.exp((rsi_v - sweet_mid) * steepness))
            return raw  

        except (KeyError, IndexError, TypeError):
            return 50.0


class Sharpe20Factor(BaseScoreFactor):
    """20-day Sharpe ratio: 20d return / annualised volatility."""
    @property
    def name(self):
        return "sharpe_20"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        c   = stock.closes
        n   = len(c)
        idx = index if index >= 0 else n + index
        m20 = _rolling_ret(c, 20, idx)
        v20 = _ann_vol(c, 20, idx)
        if m20 is None or not v20:
            return None
        return m20 / (v20 / 100.0)


class VolumeTrendFactor(BaseScoreFactor):
    @property
    def name(self): return "volume_trend"

    def score(self, stock, index=-1):
        v = stock.volumes
        n = len(v)
        idx = index if index >= 0 else n + index
        if idx < 20:
            return None
        avg = sum(v[max(0, idx - 19): idx + 1]) / 20
        v_ratio = v[idx] / avg if avg else 1.0
        
        # Sigmoid centered at 1.0 (average volume)
        # 0.5x = ~35, 1x = 50, 2x = ~73, 3x = ~88, 5x = ~98
        return 100.0 / (1.0 + math.exp(-(v_ratio - 1.0) * 1.5))


class BBEntryFactor(BaseScoreFactor):
    """
    Inverted Bollinger %B - rewards low %B (price near lower band).
    Score 100 = at lower band, 0 = at upper band.
    """
    @property
    def name(self):
        return "bb_entry"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        try:
            bb    = stock.get_indicator("BB(20,2.0)")
            n     = len(bb["upper"])
            idx   = index if index >= 0 else n + index
            upper = bb["upper"][idx]
            lower = bb["lower"][idx]
            px    = stock.closes[idx]
            rng   = upper - lower
            if rng < 1e-9:
                return 50.0
            pctb = (px - lower) / rng
            return max(0.0, min(100.0, (1.0 - pctb) * 100.0))
        except (KeyError, IndexError, TypeError):
            return None


class BreakoutFactor(BaseScoreFactor):
    """SMA20 breakout with volume confirmation - continuous scoring."""
    @property
    def name(self):
        return "breakout"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        c   = stock.closes
        v   = stock.volumes
        n   = len(c)
        idx = index if index >= 0 else n + index
        if idx < 20:
            return 30.0  # Neutral for insufficient data
        
        s20 = _sma_val(c, 20, idx)
        if not s20:
            return 30.0
        
        px = c[idx]
        
        # Distance above SMA20 as percentage (continuous)
        distance_above = (px / s20 - 1.0) * 100 if s20 > 0 else 0
        
        # Recent crossover detection (0-40 points)
        crossover_score = 0.0
        for k in range(1, 5):
            if idx - k >= 19:
                ps20 = _sma_val(c, 20, idx - k)
                if ps20 and c[idx - k] <= ps20:
                    # How recently did it cross? More recent = higher score
                    #crossover_score = 40.0 - (k - 1) * 10.0
                    crossover_score = 50.0 - (k - 1) * 15.0  # 50, 35, 20, 5 - more differentiation
                    break
        
        # Volume surge (0-30 points)
        volume_score = 0.0
        v20 = sum(v[max(0, idx - 19): idx + 1]) / 20
        if v20 > 0:
            vol_ratio = v[idx] / v20
            if vol_ratio > 1.0 and c[idx] > c[idx - 1]:
                volume_score = min(30.0, (vol_ratio - 1.0) * 20.0)
        
        # Distance above SMA20 (0-30 points)
        distance_score = min(30.0, max(0.0, distance_above * 5.0))
        
        # Combine: 40 crossover + 30 volume + 30 distance = 100 max
        return min(100.0, crossover_score + volume_score + distance_score)


class PullbackEntryFactor(BaseScoreFactor):
    """
    Dip-and-recovery pattern: pulled back below SMA20 then recovered above it.
    Bonus for broad uptrend (SMA50 > SMA200) and RSI < 65.
    """
    @property
    def name(self):
        return "pullback_entry"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        c = stock.closes
        n = len(c)
        idx = index if index >= 0 else n + index
        if idx < 20:
            return 30.0
        
        s20 = _sma_val(c, 20, idx)
        if not s20 or c[idx] <= s20:
            return 20.0 + min(30.0, (c[idx] / s20 - 1.0) * 200) if s20 else 20.0
        
        try:
            rsi_v = stock.get_indicator("RSI(14)")[idx] or 50.0
        except (KeyError, IndexError, TypeError):
            rsi_v = 50.0
        
        if rsi_v >= 65:
            return 30.0 + (65 / max(rsi_v, 1)) * 20.0  # Penalize high RSI
        
        s50 = _sma_val(c, 50, idx)
        s200 = _sma_val(c, 200, idx)
        broad_uptrend = bool(s50 and s200 and s50 > s200)
        
        # Count how many bars were below SMA20 in recent window
        dip_count = 0
        for k in range(1, 11):
            ps20 = _sma_val(c, 20, idx - k) if idx - k >= 19 else None
            if ps20 and c[idx - k] < ps20:
                dip_count += 1
        
        # How recently it crossed above
        cross_recent = False
        for k in range(1, 4):
            ps20 = _sma_val(c, 20, idx - k) if idx - k >= 19 else None
            if ps20 and c[idx - k] <= ps20:
                cross_recent = True
                break
        
        if dip_count == 0 or not cross_recent:
            return 30.0
        
        # Continuous: more dips = stronger signal, broad uptrend = bonus
        base = 40.0 + dip_count * 4.0  # 44-80 based on dip depth
        if broad_uptrend:
            base += 15.0
        if rsi_v < 55:
            base += (55 - rsi_v) * 1.0  # 0-20 bonus for lower RSI
        
        return min(100.0, base)


class CrossoverFactor(BaseScoreFactor):
    """MACD + ADX + MA crossover composite - continuous scoring."""
    @property
    def name(self):
        return "crossover"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        c   = stock.closes
        n   = len(c)
        idx = index if index >= 0 else n + index

        # MACD component - continuous
        macd_score = 50.0
        try:
            macd_data = stock.get_indicator("MACD")
            hist_now  = macd_data["histogram"][idx]
            hist_prev = macd_data["histogram"][idx - 1] if idx >= 1 else 0
            
            # Continuous mapping based on histogram value and direction
            if hist_now > 0 and hist_prev <= 0:
                macd_score = 90.0  # Strong bullish crossover
            elif hist_now < 0 and hist_prev >= 0:
                macd_score = 10.0  # Strong bearish crossover
            elif hist_now > 0:
                # Bullish zone: 60-90 based on momentum
                macd_score = 60.0 + min(30, abs(hist_now) * 100)
            else:
                # Bearish zone: 10-40 based on momentum
                macd_score = 40.0 - min(30, abs(hist_now) * 100)
        except (KeyError, IndexError, TypeError):
            pass

        # ADX component - continuous
        adx_score = 50.0
        try:
            adx_data = stock.get_indicator("ADX(14)")
            adx_now  = adx_data["adx"][idx]
            adx_prev = adx_data["adx"][idx - 3] if idx >= 3 else adx_now
            ar = adx_now - adx_prev
            
            # Continuous: stronger ADX with positive momentum = higher score
            if adx_now > 25:
                adx_score = 50.0 + min(30, adx_now - 25) + (ar * 5)
            else:
                adx_score = 30.0 + (adx_now / 25) * 20 + (ar * 5)
            adx_score = max(10, min(90, adx_score))
        except (KeyError, IndexError, TypeError):
            pass

        # MA cross component - continuous
        ma_score = 50.0
        s50  = _sma_val(c, 50,  idx)
        s200 = _sma_val(c, 200, idx)
        if s50 and s200:
            gap_pct = (s50 / s200 - 1.0) * 100  # How far apart are the MAs?
            if idx >= 1:
                s50p  = _sma_val(c, 50,  idx - 1)
                s200p = _sma_val(c, 200, idx - 1)
                if s50p and s200p:
                    # Golden cross (just crossed) = 90-100
                    if s50 > s200 and s50p <= s200p:
                        ma_score = 90.0 + min(10, gap_pct * 2)
                    # Death cross (just crossed) = 0-10
                    elif s50 < s200 and s50p >= s200p:
                        ma_score = max(0, 10.0 + gap_pct * 2)
                    # Above but not crossed = 60-80
                    elif s50 > s200:
                        ma_score = 60.0 + min(20, gap_pct * 2)
                    # Below but not crossed = 20-40
                    else:
                        ma_score = 40.0 + max(-20, gap_pct * 2)
            else:
                # No history: score based on current spread
                ma_score = 50.0 + max(-30, min(30, gap_pct * 3))

        return macd_score * 0.5 + adx_score * 0.3 + ma_score * 0.2


class OverextensionFactor(BaseScoreFactor):
    """
    Continuous scoring: price distance from SMA50, inverted.
    
    Score 100 = at or below SMA50 (ideal entry)
    Score decreases linearly as price extends above SMA50:
    - 0-5% above: 100 -> 70
    - 5-15% above: 70 -> 20
    - 15%+ above: 20 -> 0
    """
    @property
    def name(self):
        return "overextension"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        c   = stock.closes
        n   = len(c)
        idx = index if index >= 0 else n + index
        px  = c[idx]
        s50 = _sma_val(c, 50, idx)
        ref = s50 or _sma_val(c, 20, idx)
        if ref is None:
            return 50.0
        
        gap_pct = (px / ref - 1.0) * 100.0
        
        if gap_pct <= 0:
            return 100.0
        elif gap_pct <= 5:
            return 100.0 - (gap_pct / 5) * 30.0  # 100 to 70
        elif gap_pct <= 15:
            return 70.0 - ((gap_pct - 5) / 10) * 50.0  # 70 to 20
        else:
            return max(0.0, 20.0 - ((gap_pct - 15) / 35) * 20.0)  # 20 to 0


class PivotProximityFactor(BaseScoreFactor):
    """Monthly pivot proximity score. Delegates to cached MonthlyPivot indicator."""
    @property
    def name(self):
        return "pivot_proximity"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        try:
            piv = stock.get_indicator("MonthlyPivot")
            if not piv or piv.get("pp") is None:
                return 50.0
            n   = len(stock.closes)
            idx = index if index >= 0 else n + index
            px  = stock.closes[idx]
            pp  = piv["pp"]
            r1 = piv["r1"]
            r2 = piv["r2"]
            r3 = piv["r3"]
            s1 = piv["s1"]
            s2 = piv["s2"]
            s3 = piv["s3"]
            
            # Continuous scoring based on which zone price is in
            total_range = r3 - s3 if r3 > s3 else 1
            
            if px > r3:
                # Above R3: 0-10 depending on distance
                distance = (px - r3) / total_range
                return max(0.0, 10.0 - distance * 20.0)
            elif px > r2:
                # R2 to R3: 10-25
                zone_pct = (r3 - px) / (r3 - r2) if r3 > r2 else 0.5
                return 10.0 + zone_pct * 15.0
            elif px > r1:
                # R1 to R2: 25-45
                zone_pct = (r2 - px) / (r2 - r1) if r2 > r1 else 0.5
                return 25.0 + zone_pct * 20.0
            elif px > pp:
                # PP to R1: 45-90 (optimal entry zone)
                zone_pct = (r1 - px) / (r1 - pp) if r1 > pp else 0.5
                return 45.0 + zone_pct * 45.0
            elif px > s1:
                # S1 to PP: 60-85 (pullback to support)
                zone_pct = (pp - px) / (pp - s1) if pp > s1 else 0.5
                return 60.0 + zone_pct * 25.0
            elif px > s2:
                # S2 to S1: 35-60
                zone_pct = (s1 - px) / (s1 - s2) if s1 > s2 else 0.5
                return 35.0 + zone_pct * 25.0
            elif px > s3:
                # S3 to S2: 15-35
                zone_pct = (s2 - px) / (s2 - s3) if s2 > s3 else 0.5
                return 15.0 + zone_pct * 20.0
            else:
                # Below S3: 0-15
                distance = (s3 - px) / total_range
                return max(0.0, 15.0 - distance * 30.0)
                
        except (KeyError, IndexError, TypeError, ZeroDivisionError):
            return 50.0


class FibRetraceFactor(BaseScoreFactor):
    """
    Fibonacci retracement zone quality for cross-sectional ranking.

    Recalculates the retracement percentage and score from raw indicator
    data rather than pulling from FibonacciLevels.classify(). This keeps
    the factor independent of display-layer changes and ensures the
    scoring is always continuous (no flattened tiers).
    
    Optimal zone: 38.2%–61.8% retracement (the "golden pocket").
    """
    @property
    def name(self): return "fib_retrace"

    def score(self, stock, index=-1):
        try:
            fib = stock.get_indicator("FibLevels(126)")
            n = len(stock.closes)
            idx = index if index >= 0 else n + index
            if idx >= len(fib) or not fib[idx]:
                return 50.0
            
            # Pull the already-computed score from the indicator
            hi = fib[idx]["hi"]
            lo = fib[idx]["lo"]
            px = stock.closes[idx]
            if hi is None or lo is None or hi == lo:
                return 50.0
            
            retrace = (hi - px) / (hi - lo) * 100.0
            
            # Continuous scoring (same as indicator, not flattened)
            if retrace <= 0:       return 5.0
            elif retrace < 23.6:   return 5.0 + (retrace / 23.6) * 30.0
            elif retrace < 38.2:   return 35.0 + ((retrace - 23.6) / 14.6) * 25.0
            elif retrace <= 50:    return 60.0 + ((retrace - 38.2) / 11.8) * 30.0
            elif retrace <= 61.8:  return 90.0 - ((retrace - 50.0) / 11.8) * 10.0
            elif retrace <= 78.6:  return 80.0 - ((retrace - 61.8) / 16.8) * 25.0
            elif retrace <= 100:   return 55.0 - ((retrace - 78.6) / 21.4) * 30.0
            else:                  return max(0, 25.0 - ((retrace - 100) / 27.2) * 25.0)
        except:
            return 50.0


class GateStrengthFactor(BaseScoreFactor):
    """
    Continuous gate strength for cross-sectional ranking.

    Converts the six gate conditions into smooth 0-100 scores using
    sigmoid functions instead of binary thresholds. This eliminates
    the hard cutoff problem (e.g., RSI 29.9 vs 30.1).

    Components:
      - Momentum: 20-day return → sigmoid centered at 0%
      - Trend: distance from SMA20 → sigmoid centered at 0%
      - ADX: trend strength → sigmoid centered at ADX 25
      - MFI: inverted money flow → sigmoid centered at MFI 50
      - Ichimoku: cloud position → categorical (80/50/20)
      - BB Squeeze: active → 100 if squeezing
    """
    @property
    def name(self): return "gate_strength"

    def score(self, stock, index=-1):
        c = stock.closes
        n = len(c)
        if n < 20:
            return None
        
        idx = index if index >= 0 else n + index
        strengths = []
        
        # Momentum: sigmoid around 0% return
        m20 = _rolling_ret(c, 20, idx)
        if m20 is not None:
            mom = 100.0 / (1.0 + math.exp(-m20 * 0.3))
            strengths.append(mom)
        
        # Trend: sigmoid around 0% deviation from SMA20
        s20 = _sma_val(c, 20, idx)
        if s20 and s20 > 0:
            dev = (c[idx] / s20 - 1.0) * 100
            trend = 100.0 / (1.0 + math.exp(-dev * 0.5))
            strengths.append(trend)
        
        # ADX: sigmoid centered at ADX 25
        try:
            adx_v = stock.get_indicator("ADX(14)")["adx"][idx]
            if adx_v is not None:
                adx = 100.0 / (1.0 + math.exp(-(adx_v - 25) * 0.1))
                strengths.append(adx)
        except: pass
        
        # MFI: sigmoid centered at MFI 50 (lower MFI = higher score)
        try:
            mfi_v = stock.get_indicator("MFI(14)")[idx]
            if mfi_v is not None:
                mfi = 100.0 / (1.0 + math.exp((mfi_v - 50) * 0.08))
                strengths.append(mfi)
        except: pass
        
        # Ichimoku: cloud position
        try:
            ichi = stock.get_indicator("Ichimoku")
            cloud_pos = ichi.get("cloud_pos", "")
            if isinstance(cloud_pos, list):
                pos = str(cloud_pos[idx]) if idx < len(cloud_pos) else ""
            else:
                pos = str(cloud_pos)
            if "Above" in pos: strengths.append(80.0)
            elif "Inside" in pos: strengths.append(50.0)
            elif "Below" in pos: strengths.append(20.0)
        except: pass
        
        # BB Squeeze: bonus when active
        try:
            ttm = stock.get_indicator("TTM_Squeeze")
            squeeze_series = ttm["squeeze"]
            if idx < len(squeeze_series) and squeeze_series[idx]:
                strengths.append(100.0)
        except: pass
        
        if not strengths:
            return 50.0
        
        return sum(strengths) / len(strengths)

class BullishSetupFactor(BaseScoreFactor):
    """
    Quality of the bullish reversal setup for cross-sectional ranking.

    Combines three conditions identified as critical for a successful bounce:
      1. ADX direction: +DI > -DI (bullish trend alignment)
      2. CandleScore: uses the indicator's own classify score (0-100)
      3. Ret(5) extension: rewards pullbacks, penalizes recent run-ups
    
    Each component is scored 0-100, then averaged equally.
    """
    @property
    def name(self): return "bullish_setup"

    def score(self, stock: "Stock", index: int = -1) -> Optional[float]:
        c = stock.closes
        n = len(c)
        idx = index if index >= 0 else n + index

        # ---------- ADX direction ----------
        adx_score = 0.0
        try:
            adx_data = stock.get_indicator("ADX(14)")
            pdi = adx_data["+di"][idx]
            mdi = adx_data["-di"][idx]
            if pdi is not None and mdi is not None and pdi > mdi:
                adx_score = 100.0 / (1.0 + math.exp(-(pdi - mdi) * 0.3))
        except:
            pass

        # ---------- CandleScore ----------
        candle_score = 0.0
        try:
            cs_data = stock.get_indicator("CandleScore")
            cs_vals = cs_data.get("vals", []) if isinstance(cs_data, dict) else cs_data
            cs_val = cs_vals[idx] if idx < len(cs_vals) else None
            if cs_val is not None and cs_val >= 3:
                candle_score = min(100.0, cs_val * 20.0)  # +3→60, +5→100
        except:
            pass

        # ---------- Ret(5) extension ----------
        ret5_score = 0.0
        try:
            ret5_data = stock.get_indicator("Ret(5)")
            ret5_vals = ret5_data.get("vals", []) if isinstance(ret5_data, dict) else ret5_data
            ret5 = ret5_vals[idx] if idx < len(ret5_vals) else None
            if ret5 is not None:
                ret5_score = 100.0 / (1.0 + math.exp(ret5 * 0.3))
        except:
            pass

        # Combine (equal weight average)
        combined = (adx_score + candle_score + ret5_score) / 3.0
        return max(0.0, min(100.0, combined))


class StochEntryFactor(BaseScoreFactor):
    """
    Stochastic entry-quality factor.

    Registered at weight 0.0 in FACTOR_WEIGHTS (see PLAN.md Phase 7) -
    added alongside the Stochastic/HeikinAshi indicators for a future
    backtest A/B, not yet validated to add ranking signal on its own.
    Continuous, inverted like the BB entry factor: oversold %K scores
    high (entry-friendly).
    """
    @property
    def name(self):
        return "stoch_entry"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        try:
            data = stock.get_indicator("Stoch(14,3,3)")
            k_series = data["k"]
            idx = _norm_index(index, len(k_series))
            if idx < 0 or idx >= len(k_series) or k_series[idx] is None:
                return None
            return max(0.0, min(100.0, 100.0 - k_series[idx]))
        except (KeyError, IndexError, TypeError):
            return None


class HAReversalFactor(BaseScoreFactor):
    """
    Heikin-Ashi reversal-setup factor.

    Registered at weight 0.0, same status as StochEntryFactor above.
    Deliberately simpler than HeikinAshi.classify()'s pattern-based
    verdict (which also checks for "indecision" candle wicks) - a factor
    just needs a robust continuous ranking signal, not a rich display
    verdict, so this reads the streak directly through a sigmoid (same
    shape as RSI's sweet-spot scoring): a long red streak scores high
    (potential bullish reversal), a long green streak scores low.
    """
    @property
    def name(self):
        return "ha_reversal"

    def score(self, stock: Stock, index: int = -1) -> Optional[float]:
        try:
            data = stock.get_indicator("HeikinAshi")
            streak = data["streak"]
            idx = _norm_index(index, len(streak))
            if idx < 0 or idx >= len(streak):
                return None
            return 100.0 / (1.0 + math.exp(streak[idx] * 0.35))
        except (KeyError, IndexError, TypeError):
            return None


# Shared, stateless instances - see TECHNICAL_INDICATORS (Stock class) for
# rationale. Referenced by Stock.__init__ despite being defined textually
# after it - fine, since that reference only executes at call time (when a
# Stock is actually constructed), by which point the whole module has
# finished loading and every BaseScoreFactor subclass has registered.
SCORE_FACTORS: List[BaseScoreFactor] = [cls() for cls in BaseScoreFactor._registry]


# ---------------------------------------------------------------------------
# Bonus computer  (added after normalisation, never normalised itself)
# ---------------------------------------------------------------------------

def _fund_sub_scores(stock: Stock) -> Dict[str, Optional[float]]:
    """
    Compute the fundamental sub-category scores for one stock.
    Each is a weighted average of member fundamental Verdict scores (0-100).
    Returns None for a sub-category if no members have real data.

    A member "has data" iff its verdict isn't "N/A" - every fundamental's
    classify() returns a numeric score even in its no-data case (0 for
    most, 50 for MarketCap's display-only case), so checking `score is not
    None` never actually excludes anything: it would silently treat a
    stock with zero real fundamentals (e.g. an index) as if it had
    genuinely scored the worst possible value in every category, rather
    than as missing data. Checking the verdict is the stable "no data"
    signal (see tests/test_fundamentals_goldens.py).
    """
    subs: Dict[str, Optional[float]] = {}
    for sub_name, members in FUNDAMENTAL_GROUPS.items():
        w_sum = 0.0
        w_tot = 0.0
        for fname, w in members:
            try:
                _, v = stock.get_fundamental(fname)
                if v.get("verdict") != "N/A":
                    w_sum += v.get("score", 0) * w
                    w_tot += w
            except Exception as e:
                _log_swallowed(f"fund_sub_score:{fname}", stock.symbol, e)
        subs[sub_name] = (w_sum / w_tot) if w_tot > 0 else None
    return subs


class BonusComputer:
    """
    Net score bonus/penalty added to the TA composite after xnorm.
    """
    MFI_OVERSOLD_BONUS:     float =  0.0
    MFI_OVERBOUGHT_PENALTY: float =  0.0
    ICHI_ABOVE_BONUS:       float =  2.0
    ICHI_BELOW_PENALTY:     float = -3.0
    BB_SQUEEZE_BONUS:       float =  3.0   # Squeeze building energy
    BB_SQUEEZE_FIRED:       float =  5.0   # Squeeze just fired

    @classmethod
    def compute(cls, stock: Stock, index: int = -1) -> float:
        n   = len(stock.closes)
        idx = index if index >= 0 else n + index
        total = 0.0

        # Candlestick bonus
        try:
            cs_data = stock.get_indicator("CandleScore")
            cs_vals = cs_data.get("vals", []) if isinstance(cs_data, dict)else cs_data
            val = cs_vals[idx] if idx < len(cs_vals) else None
            if val is not None:
                total += float(val)
        except (KeyError, IndexError, TypeError):
            pass

        # MFI bonus/penalty
        try:
            mfi_series = stock.get_indicator("MFI(14)")
            mfi_v = mfi_series[idx] if idx < len(mfi_series) else None
            if mfi_v is not None:
                if   mfi_v < 20: total += cls.MFI_OVERSOLD_BONUS
                elif mfi_v > 80: total += cls.MFI_OVERBOUGHT_PENALTY
        except (KeyError, IndexError, TypeError):
            pass

        # Ichimoku bonus/penalty
        try:
            ichi = stock.get_indicator("Ichimoku")
            cloud_pos = ichi.get("cloud_pos", [])
            pos = cloud_pos[idx] if isinstance(cloud_pos, list) and idx < len(cloud_pos) else str(cloud_pos)
            if "Above" in pos:
                total += cls.ICHI_ABOVE_BONUS
            elif "Below" in pos:
                total += cls.ICHI_BELOW_PENALTY
        except (KeyError, TypeError):
            pass

        # Peak-proximity penalty
        try:
            hi52 = max(stock.highs[max(0, idx - 251): idx + 1])
            px   = stock.closes[idx]
            pct  = (hi52 - px) / hi52 * 100.0 if hi52 > 0 else 10.0
            if   pct < 1: total += -5.0
            elif pct < 3: total += -2.0
            elif pct < 8: total +=  1.0
        except (ValueError, ZeroDivisionError):
            pass

        # TTM Squeeze bonus
        try:
            ttm = stock.get_indicator("TTM_Squeeze")
            squeeze_series = ttm.get("squeeze", [])
            fired_series = ttm.get("squeeze_off", [])
            
            if idx < len(squeeze_series):
                if idx < len(fired_series) and fired_series[idx]:
                    total += cls.BB_SQUEEZE_FIRED
                elif squeeze_series[idx]:
                    total += cls.BB_SQUEEZE_BONUS
        except (KeyError, IndexError, TypeError):
            pass

        return total


class RankingTable:
    """Renders the cross-sectional ranking with gate flags."""
    @staticmethod
    def print_ranking(results: List[dict]) -> None:
        """Print a ranked table.  Detects xnorm vs legacy format from keys."""
        if not results:
            print("  No stocks to rank.")
            return

        xnorm_mode = "ta_composite" in results[0]

        if xnorm_mode:

            # -- Legend ----------------------------------------------
            print(f"\n{CLR.BD}Gate Flags:{CLR.E}")
            print(f"  {CLR.G}M{CLR.E} = Momentum (20d return > 0)")
            print(f"  {CLR.G}T{CLR.E} = Trend (price > SMA20 or oversold recovery)")
            print(f"  {CLR.G}A{CLR.E} = ADX (ADX > 20 or RSI < 30)")
            print(f"  {CLR.G}F{CLR.E} = MFI (MFI <= 80)")
            print(f"  {CLR.G}I{CLR.E} = Ichimoku (price above cloud)")
            print(f"  {CLR.G}B{CLR.E} = BB/TTM Squeeze (volatility compression)")
            print(f"  {CLR.DM}✓ = passed  ✗ = failed{CLR.E}")
            if any(r.get("low_coverage") for r in results):
                print(f"  {CLR.Y}*{CLR.E} = low data coverage (score blended over mostly-missing factors - treat with caution)")

            show_fa = any(r.get("fundamental_avg", 0) > 0 for r in results)
            headers = ["#", "Symbol", "Score", "TA"] + (["FA"] if show_fa else []) + \
                      ["M", "T", "A", "F", "I", "B"]  # Added "B" for BB/TTM Squeeze
            rows = []
            for rank, r in enumerate(results, 1):
                def gf(k):
                    return f"{CLR.G}✓{CLR.E}" if r.get(k, False) else f"{CLR.R}✗{CLR.E}"
                symbol = r["symbol"] + (f"{CLR.Y}*{CLR.E}" if r.get("low_coverage") else "")
                row = [
                    str(rank),
                    symbol,
                    f"{r['overall']:.1f}",
                    f"{r['ta_composite']:.1f}",
                ]
                if show_fa:
                    row.append(f"{r['fundamental_avg']:.1f}" if r.get("fundamental_avg") else "-")
                row += [gf("gate_mom"), gf("gate_trend"), gf("gate_adx"),
                        gf("gate_mfi"), gf("gate_ichi"), gf("gate_bb_ttm")]  # Added squeeze
                rows.append(row)
        else:
            headers = ["#", "Symbol", "Overall", "Technical", "Fundamental", "Tech #", "Fund #"]
            rows = []
            for rank, r in enumerate(results, 1):
                rows.append([
                    str(rank),
                    r["symbol"],
                    f"{r['overall']:.1f}",
                    f"{r.get('technical_avg', 0):.1f}",
                    f"{r.get('fundamental_avg', 0):.1f}",
                    str(r.get("technical_count", "-")),
                    str(r.get("fundamental_count", "-")),
                ])

        _print_table(headers, rows)


# =============================================================================
# 7.  STOCK REPORTER
# =============================================================================
class StockReporter:
    """Handles all output - tables and full reports."""

    def __init__(self, stocks: List[Stock]):
        self.stocks = stocks

    def print_technical_summary(self, indicators=None):
        """Print table of technical indicators."""
        IndicatorTable.print_summary(self.stocks, indicators)

    def print_fundamental_summary(self, fundamentals=None):
        """Print table of fundamental metrics."""
        FundamentalTable.print_summary(self.stocks, fundamentals)

    def print_scoreFactor_summary(self, scoreFactors=None):
        """Print table of fundamental metrics."""
        ScoreTable.print_summary(self.stocks, scoreFactors)

    def print_full_report(self, stock: Stock):
        """Detailed, color-coded single-stock report."""
        last_idx = len(stock.candles) - 1
        latest = stock.candles[last_idx]
        W = 72
        print(f"\n{CLR.BD}{CLR.CY}{'='*W}{CLR.E}")
        print(f"{CLR.BD}{CLR.CY}  Full Report: {stock.symbol}{CLR.E}")
        print(f"{CLR.BD}{CLR.CY}{'='*W}{CLR.E}")
        print(f"  {CLR.W}Date   : {CLR.CY}{latest.timestamp.date()}{CLR.E}   "
              f"{CLR.W}Close  : {CLR.CY}{latest.close:,.3f}{CLR.E}   "
              f"{CLR.W}Volume : {CLR.CY}{latest.volume:,.0f}{CLR.E}\n")

        summary = stock.metadata.get("fundamentals", {}).get("longBusinessSummary", "")
        if summary:
            print(f"{CLR.BD}  Business Summary:{CLR.E}")
            print(textwrap.fill(summary, width=100, initial_indent="\t\t\t", subsequent_indent="\t\t"))

            print()

        print(f"{CLR.BD}  Technical Indicators:{CLR.E}")
        for ind in stock.indicators:
            try:
                v = ind.classify(stock, last_idx)
                result = v.get("result", {})
                if result:
                    raw_str = "  ".join(f"{k}={_fmt_val(val)}" for k, val in result.items())
                else:
                    raw = stock.get_indicator(ind.name)
                    raw_str = _fmt_val(raw[last_idx]) if isinstance(raw, list) and raw[last_idx] is not None else "-"
                print(f"  {ind.name:15s}: {raw_str:90s}{v['color']} {ind.name:15s}:(S:{v['score']:.0f}) {v['verdict']:80s}{CLR.E}")
            except Exception as e:
                print(f"  {ind.name:22s}  error: {e}")

        print(f"\n{CLR.BD}  Fundamental Analysis:{CLR.E}")
        for f in stock.fundamentals:
            try:
                raw, v = stock.get_fundamental(f.name)
                raw_str = _fmt_val(raw) if raw is not None else "N/A"
                print(f"  {f.name:22s}  {raw_str:40s}  {v['color']}{v['verdict']:30s}{CLR.E}  {CLR.DM}score {v['score']:3d}{CLR.E}")
            except Exception as e:
                print(f"  {f.name:22s}  error: {e}")

        print(f"\n{CLR.BD}{CLR.CY}{'='*W}{CLR.E}\n")


# =============================================================================
# 8. STONKS ORCHESTRATOR
# =============================================================================
class Stonks:
    """Main orchestrator - loads stocks, runs rankings, delegates to reporters."""
    def __init__(self, path: str,
                 precompute_mode: PreComputeMode = PreComputeMode.PCM_ALL,
                 from_date: Optional[datetime] = None,
                 to_date: Optional[datetime] = None):
        self._path = path
        self.stocks = self._load_all_stocks(path, precompute_mode, from_date, to_date)
        self.reporter = StockReporter(self.stocks)

    @staticmethod
    def _load_all_stocks(path: str,
                        precompute_mode: PreComputeMode = PreComputeMode.PCM_ALL,
                        from_date: Optional[datetime] = None,
                        to_date: Optional[datetime] = None) -> List[Stock]:
        stocks = []
        if os.path.isfile(path):
            stock = Stock._load_single_stock(path, precompute_mode, from_date, to_date)
            if stock:
                stocks.append(stock)
        elif os.path.isdir(path):
            files = [os.path.join(path, f) for f in sorted(os.listdir(path))
                    if f.endswith(".json")]

            max_workers = min(32, (os.cpu_count() or 1) * 2)

            def load_with_mode(f):
                return Stock._load_single_stock(f, precompute_mode, from_date, to_date)
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {executor.submit(load_with_mode, f): f for f in files}
                for future in as_completed(future_to_file):
                    try:
                        stock = future.result()
                        if stock:
                            stocks.append(stock)
                    except Exception as e:
                        print(f"ERROR loading {os.path.basename(future_to_file[future])}: {e}")
        else:
            print(f"ERROR: Path not found or unsupported: '{path}'")
        return stocks

    def technical_summary(self):
        self.reporter.print_technical_summary()

    def fundamental_summary(self):
        self.reporter.print_fundamental_summary()

    def score_summary(self):
        ScoreTable.print_summary(self.stocks)
    
    def full_report(self, symbol: str):
        stock = next((s for s in self.stocks if s.symbol.upper() == symbol.upper()), None)
        if stock is None:
            print(f"Symbol '{symbol}' not found.")
        else:
            self.reporter.print_full_report(stock)

    def rank_stocks_xnorm(self,
            tech_weight: float = 0.5,
            fund_weight: float = 0.5,
            factor_weights: Optional[Dict[str, float]] = None,
            fundamental_sub_weights: Optional[Dict[str, float]] = None,
            max_workers: int = None
    ) -> List[Tuple[Stock, Dict]]:
        """
        Two-pass cross-section normalised ranking - mirrors portfolio_bot exactly.

        Pass 1 : collect raw factor scores for every stock.
        Normalise: xnorm() each factor column across the universe.
        Pass 2 : weighted TA composite per stock.
        Bonus  : BonusComputer.compute() added after normalisation.
        Blend  : norm_fund_weight*FA + (1-norm_fund_weight)*TA, where
                 norm_fund_weight = fund_weight / (tech_weight + fund_weight).

        tech_weight and fund_weight are normalised to sum to 1 (matching
        the README's documented contract) rather than tech_weight being
        silently ignored - so the default 0.85/0.15 (already summing to 1)
        behaves identically to before, and only non-summing-to-1 inputs
        change behaviour (from "silently used 1-fund_weight regardless of
        tech_weight" to "both weights actually matter").

        fund_weight = 0 → pure technical ranking (default).
        """
        if not self.stocks:
            return []

        weight_total = tech_weight + fund_weight
        fund_weight = (fund_weight / weight_total) if weight_total > 0 else 0.0

        fweights = factor_weights or FACTOR_WEIGHTS
        sfw   = fundamental_sub_weights or FUNDAMENTAL_SUB_WEIGHTS

        # -- Pass 1: collect raw factor scores for all stocks ----------
        raw_scores: Dict[str, Dict[str, Optional[float]]] = {
            fname: {} for fname in fweights
        }

        # Use all registered factors that have a weight
        factors = [f for f in SCORE_FACTORS if f.name in fweights]

        for stock in self.stocks:
            for factor in factors:
                try:
                    raw_scores[factor.name][stock.symbol] = factor.score(stock)
                except KeyError:
                    # An unknown indicator name is a programmer error (typo
                    # in a factor's get_indicator() call), not a legitimate
                    # per-stock data gap - don't let it masquerade as one.
                    raise
                except Exception as e:
                    raw_scores[factor.name][stock.symbol] = None
                    _log_swallowed(f"factor_score:{factor.name}", stock.symbol, e)

        # -- Normalise each factor column -----------------------------
        normed: Dict[str, Dict[str, float]] = {
            fname: xnorm(col) for fname, col in raw_scores.items()
        }

        # -- Fundamental sub-scores (cross-section normalised) --------
        fund_raw: Dict[str, Dict[str, Optional[float]]] = {
            sub: {} for sub in sfw
        }
        if fund_weight > 0:
            for stock in self.stocks:
                sub_scores = _fund_sub_scores(stock)
                for sub, val in sub_scores.items():
                    if sub in fund_raw:
                        fund_raw[sub][stock.symbol] = val

            fund_normed = {sub: xnorm(col) for sub, col in fund_raw.items()}

        # -- Pass 2: Compute composites (weighted sum of normalised scores) -----
        # Single-stock: xnorm collapses every factor to 50 (lo==hi), so use
        # raw factor/sub-scores directly instead - see _weighted_composite.
        MIN_COVERAGE = 0.6   # below this fraction of weight backed by real
                             # data, a stock is flagged rather than ranked
                             # on mostly-neutral filler (see ranking table's
                             # "*" suffix)
        results = []
        _single = len(self.stocks) == 1
        for stock in self.stocks:
            sym = stock.symbol

            ta_comp, ta_coverage = _weighted_composite(sym, fweights, raw_scores, normed, use_raw=_single)
            ta_comp += BonusComputer.compute(stock)
            ta_comp = max(0.0, min(100.0, ta_comp))

            fa_comp, fa_coverage = 0.0, 0.0
            if fund_weight > 0 and fund_normed:
                fa_comp, fa_coverage = _weighted_composite(sym, sfw, fund_raw, fund_normed, use_raw=_single)

            # Blend
            if fund_weight > 0:
                overall  = ((1.0 - fund_weight) * ta_comp) + (fund_weight * fa_comp)
                coverage = ((1.0 - fund_weight) * ta_coverage) + (fund_weight * fa_coverage)
            else:
                overall  = ta_comp
                coverage = ta_coverage
            overall = max(0.0, min(100.0, overall))

            # Gate flags
            gates = stock.gate_flags()

            results.append((stock, {
                "overall":         round(overall, 1),
                "ta_composite":    round(ta_comp,  1),
                "fa_composite":    round(fa_comp,  1),
                "low_coverage":    coverage < MIN_COVERAGE,
                "factor_detail":   {  # Optional: useful for backtest analysis
                    fname: {
                        "raw":    raw_scores[fname].get(sym),
                        "normed": normed[fname].get(sym, 50.0),
                        "weight": fweights.get(fname, 0),
                    }
                    for fname in fweights
                },
                **gates,
            }))

        results.sort(key=lambda x: x[1]["overall"], reverse=True)
        return results

     # -- Public ranking call (uses the normalised ranking) ------------

    def ranking(self,
                tech_weight: float = 0.5,
                fund_weight: float = 0.5,
                factor_weights: Optional[Dict[str, float]] = None,
                fundamental_sub_weights: Optional[Dict[str, float]] = None) -> None:
        ranked = self.rank_stocks_xnorm(
            tech_weight=tech_weight,
            fund_weight=fund_weight,
            factor_weights=factor_weights,
            fundamental_sub_weights=fundamental_sub_weights,
        )
        # Convert to the flat dicts that RankingTable expects
        table_rows = []
        for stock, score in ranked:
            table_rows.append({
                "symbol":            stock.symbol,
                "overall":           score["overall"],
                "ta_composite":      score["ta_composite"],
                "fundamental_avg":   score["fa_composite"],
                "low_coverage":      score.get("low_coverage", False),
                "technical_count":   len(factor_weights or FACTOR_WEIGHTS),
                "fundamental_count": len(fundamental_sub_weights or FUNDAMENTAL_SUB_WEIGHTS) if fund_weight > 0 else 0,
                # Gate flags for display
                "gate_mom":    score.get("gate_mom",   False),
                "gate_trend":  score.get("gate_trend", False),
                "gate_adx":    score.get("gate_adx",   False),
                "gate_mfi":    score.get("gate_mfi",   False),
                "gate_ichi":   score.get("gate_ichi",  False),
                "gate_bb_ttm": score.get("gate_bb_ttm", False),
            })
        RankingTable.print_ranking(table_rows)

# =============================================================================
# 9.  CLI
# =============================================================================
def parse_args():
    parser = argparse.ArgumentParser(description="stonks - Stock Analysis Tool")
    parser.add_argument("path", nargs="?", default=os.path.join(".", "data"),            help="JSON file or directory with stock data")
    parser.add_argument("-r", "--ranking", action="store_true",                          help="Show composite score ranking")
    parser.add_argument("--tech-weight",   dest="tech_weight", type=float, default=0.85, help="Technical weight for ranking")
    parser.add_argument("--fund-weight",   dest="fund_weight", type=float, default=0.15, help="Fundamental weight for ranking")
    parser.add_argument("--from-date",     dest="from_date",   type=str,   default=None, help="Start date (YYYY-MM-DD) for analysis window")
    parser.add_argument("--to-date",       dest="to_date",     type=str,   default=None, help="End date (YYYY-MM-DD) for analysis window")
    parser.add_argument("--no-color",      dest="no_color",    action="store_true",      help="Disable ANSI color output")
    parser.add_argument("--debug",         dest="debug",       action="store_true",      help="Log swallowed exceptions (typos, indicator bugs) to stderr")
    parser.add_argument("--weights",       dest="weights",     type=str,   default=None, help="TOML file overriding factor/fundamental weights and blend (see weights.example.toml)")

    return parser.parse_args()

def main():

    args = parse_args()
    if args.no_color or os.environ.get("NO_COLOR"):
        CLR.disable()
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.addHandler(logging.StreamHandler())

    print(_STONKS_BANNER)

    factor_weights = None
    fundamental_sub_weights = None
    if args.weights:
        try:
            factor_weights, fundamental_sub_weights, file_fund_weight = load_weights_file(args.weights)
        except (ValueError, OSError, tomllib.TOMLDecodeError) as e:
            print(f"ERROR: failed to load weights file '{args.weights}': {e}")
            return
        if file_fund_weight is not None:
            # The file's fund_weight is authoritative - set tech_weight to
            # its exact complement so rank_stocks_xnorm's normalisation is
            # a no-op instead of re-diluting it against the CLI default.
            args.fund_weight = file_fund_weight
            args.tech_weight = 1.0 - file_fund_weight

    # Parse dates with IST timezone (matching the candle data)
    ist = timezone(timedelta(hours=5, minutes=30))
    from_date = datetime.fromisoformat(args.from_date).replace(tzinfo=ist) if args.from_date else None
    to_date = datetime.fromisoformat(args.to_date).replace(tzinfo=ist) if args.to_date else None

    # Determine precompute mode
    precompute_mode = PreComputeMode.PCM_ALL
    if args.ranking and args.fund_weight == 0:
        precompute_mode = PreComputeMode.PCM_TECHNICAL
    elif args.ranking and args.tech_weight == 0:
        precompute_mode = PreComputeMode.PCM_FUNDAMENTAL

    app = Stonks(args.path, precompute_mode=precompute_mode, from_date=from_date, to_date=to_date)

    if not app.stocks:
        print("ERROR: No valid stock data found.")
        return

    if os.path.isfile(args.path):
        # Single file → full report
        app.reporter.print_full_report(app.stocks[0])
    elif args.ranking:
        app.ranking(args.tech_weight, args.fund_weight, factor_weights, fundamental_sub_weights)
    else:
        # Directory → both summary tables
        app.technical_summary()
        app.fundamental_summary()
        app.score_summary()

if __name__ == "__main__":
    main()