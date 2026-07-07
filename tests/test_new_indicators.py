"""
True unit tests (hand-computed expected values, not golden files) for the
two Phase 7 indicators: Stochastic and HeikinAshi. See PLAN.md Phase 7.

Golden-file coverage for these (compute()/classify() over the real
fixtures) lives in test_indicator_goldens.py alongside every other
indicator - these tests instead pin down the exact arithmetic against
small, hand-verifiable synthetic series.
"""
import pytest

import stonks


class _FakeStock:
    """Minimal stand-in exposing just what compute()/classify() need,
    without going through StockFactory/real candle loading."""
    def __init__(self, **attrs):
        self.__dict__.update(attrs)
        self._indicator_cache = {}

    def get_indicator(self, name):
        if name not in self._indicator_cache:
            raise KeyError(name)
        return self._indicator_cache[name]


# ---------------------------------------------------------------------------
# Stochastic
# ---------------------------------------------------------------------------

def test_stochastic_raw_k_matches_hand_computation():
    # period=3, smooth_k=1, smooth_d=1 (identity smoothing) isolates Raw %K.
    # Single-price bars (high=low=close) make LLV/HHV trivial to verify by
    # hand: %K = (close - LLV)/(HHV - LLV) * 100 over the trailing 3 bars.
    closes = [10, 12, 8, 14, 6, 20, 4]
    stock = _FakeStock(closes=closes, highs=closes, lows=closes)
    stoch = stonks.Stochastic(period=3, smooth_k=1, smooth_d=1)
    result = stoch.compute(stock)
    # i=0,1: insufficient history. i=2: window=[10,12,8] -> (8-8)/(12-8)=0.
    # i=3: window=[12,8,14] -> (14-8)/(14-8)=100. i=4: window=[8,14,6] ->
    # (6-6)/(14-6)=0. i=5: window=[14,6,20] -> (20-6)/(20-6)=100.
    # i=6: window=[6,20,4] -> (4-4)/(20-4)=0.
    assert result["k"] == [None, None, 0.0, 100.0, 0.0, 100.0, 0.0]
    assert result["d"] == result["k"]  # smooth_d=1 is identity too


def test_stochastic_smoothing_matches_hand_computation():
    closes = [10, 12, 8, 14, 6, 20, 4]
    stock = _FakeStock(closes=closes, highs=closes, lows=closes)
    stoch = stonks.Stochastic(period=3, smooth_k=3, smooth_d=1)
    result = stoch.compute(stock)
    # Raw %K = [None,None,0,100,0,100,0] (from the test above). SMA(3) of
    # that, requiring 3 consecutive non-None values: first valid at i=4
    # (window [0,100,0] -> avg 33.33), i=5 ([100,0,100] -> 66.67),
    # i=6 ([0,100,0] -> 33.33).
    expected = [None, None, None, None, pytest.approx(100/3), pytest.approx(200/3), pytest.approx(100/3)]
    assert result["k"] == expected


def test_stochastic_flat_range_is_none_not_division_by_zero():
    closes = [100.0] * 20
    stock = _FakeStock(closes=closes, highs=closes, lows=closes)
    stoch = stonks.Stochastic()
    result = stoch.compute(stock)
    assert all(v is None for v in result["k"])
    assert all(v is None for v in result["d"])


def test_stochastic_classify_out_of_range_index_is_na():
    stoch = stonks.Stochastic()
    stock = _FakeStock()
    stock._indicator_cache[stoch.name] = {"k": [50.0, 60.0], "d": [55.0, 58.0]}
    v = stoch.classify(stock, index=-100)
    assert v["verdict"] == "N/A"
    assert v["score"] == 0


def test_stochastic_classify_oversold_scores_high():
    stoch = stonks.Stochastic()
    stock = _FakeStock()
    stock._indicator_cache[stoch.name] = {"k": [None, 5.0], "d": [None, 10.0]}
    v = stoch.classify(stock, index=1)
    assert v["score"] > 80
    assert "oversold" in v["verdict"]


def test_stochastic_classify_overbought_scores_low():
    stoch = stonks.Stochastic()
    stock = _FakeStock()
    stock._indicator_cache[stoch.name] = {"k": [None, 95.0], "d": [None, 90.0]}
    v = stoch.classify(stock, index=1)
    # %K in (80,100] maps to score in [20,40) - the deepest scores (<20)
    # are reserved for %K > 100 (the "ABOVE range" branch, smoothing
    # overshoot only). 95%K should still land clearly in the lower half.
    assert v["score"] < 50
    assert "overbought" in v["verdict"]


def test_stochastic_classify_bullish_crossover_bonus():
    stoch = stonks.Stochastic()
    stock = _FakeStock()
    # %K crosses above %D between bar 0 and bar 1 (50<=52 then 55>53).
    stock._indicator_cache[stoch.name] = {"k": [50.0, 55.0], "d": [52.0, 53.0]}
    with_cross = stoch.classify(stock, index=1)["score"]
    # Same bar-1 k/d values but no prior bar to compare against -> no bonus.
    stock._indicator_cache[stoch.name] = {"k": [None, 55.0], "d": [None, 53.0]}
    without_cross = stoch.classify(stock, index=1)["score"]
    assert with_cross == without_cross + 10


# ---------------------------------------------------------------------------
# HeikinAshi
# ---------------------------------------------------------------------------

def test_heikin_ashi_compute_matches_hand_computation():
    # Bar 0: O=10,H=12,L=9,C=11 -> HA_close=10.5, HA_open=(10+11)/2=10.5 (tie -> green)
    # Bar 1: O=11,H=13,L=10,C=12 -> HA_close=11.5, HA_open=(10.5+10.5)/2=10.5 -> green
    # Bar 2: O=12,H=9,L=7,C=8 (down day) -> HA_close=9.0, HA_open=(10.5+11.5)/2=11.0 -> red
    stock = _FakeStock(
        opens=[10, 11, 12], highs=[12, 13, 9], lows=[9, 10, 7], closes=[11, 12, 8],
        candles=[None, None, None],
    )
    ha = stonks.HeikinAshi()
    result = ha.compute(stock)
    assert result["ha_open"] == [10.5, 10.5, 11.0]
    assert result["ha_close"] == [10.5, 11.5, 9.0]
    assert result["ha_high"] == [12, 13, 11.0]
    assert result["ha_low"] == [9, 10, 7]
    assert result["color"] == ["green", "green", "red"]
    assert result["streak"] == [1, 2, -1]


def test_heikin_ashi_streak_resets_on_color_flip():
    stock = _FakeStock(
        opens=[10, 12, 14, 10],
        highs=[13, 15, 17, 15],
        lows=[9, 11, 13, 8],
        closes=[12, 14, 16, 9],  # up, up, up, then a sharp down bar
        candles=[None] * 4,
    )
    ha = stonks.HeikinAshi()
    result = ha.compute(stock)
    assert result["streak"][:3] == [1, 2, 3]
    assert result["color"][3] == "red"
    assert result["streak"][3] == -1  # resets, doesn't continue from +3


def test_heikin_ashi_classify_out_of_range_index_is_na():
    ha = stonks.HeikinAshi()
    stock = _FakeStock()
    stock._indicator_cache[ha.name] = {
        "color": ["green"], "streak": [1],
        "ha_open": [1], "ha_high": [1], "ha_low": [1], "ha_close": [1],
    }
    v = ha.classify(stock, index=-100)
    assert v["verdict"] == "N/A"


def test_heikin_ashi_classify_bullish_setup_via_long_streak():
    ha = stonks.HeikinAshi()
    stock = _FakeStock()
    stock._indicator_cache[ha.name] = {
        "color":  ["red", "red", "red", "green"],
        "streak": [-1, -2, -3, 1],
        "ha_open":  [10, 9, 8, 6],
        "ha_high":  [10.5, 9.2, 8.1, 9],
        "ha_low":   [8, 7, 6, 5.5],
        "ha_close": [9, 8, 7, 8.5],
    }
    v = ha.classify(stock, index=3)
    assert v["verdict"].startswith("Bullish HA reversal")
    assert 60 <= v["score"] <= 90


def test_heikin_ashi_classify_bearish_setup_via_long_streak():
    ha = stonks.HeikinAshi()
    stock = _FakeStock()
    stock._indicator_cache[ha.name] = {
        "color":  ["green", "green", "green", "red"],
        "streak": [1, 2, 3, -1],
        "ha_open":  [6, 7, 8, 10],
        "ha_high":  [8, 9, 10.2, 10.5],
        "ha_low":   [5.5, 6, 7, 8],
        "ha_close": [7, 8, 9, 8.5],
    }
    v = ha.classify(stock, index=3)
    assert v["verdict"].startswith("Bearish HA reversal")
    assert 10 <= v["score"] <= 40


def test_heikin_ashi_classify_short_streak_without_indecision_is_neutral():
    # prior_streak=1 (< 3) and bar idx-1 has a large body (not indecision)
    # -> no reversal signal, just the neutral streak readout.
    ha = stonks.HeikinAshi()
    stock = _FakeStock()
    stock._indicator_cache[ha.name] = {
        "color":  ["green", "red", "green"],
        "streak": [1, -1, 1],
        "ha_open":  [8, 10, 6],
        "ha_high":  [8.2, 10.2, 9],
        "ha_low":   [7.8, 4.8, 5.8],
        "ha_close": [8.1, 5, 8.5],
    }
    v = ha.classify(stock, index=2)
    assert v["score"] == 50
    assert v["verdict"] == "green streak=+1"


def test_heikin_ashi_classify_indecision_candle_triggers_setup_despite_short_streak():
    # prior_streak=1 but bar idx-1 is a bearish-indecision candle (tiny
    # body, long upper wick) - should still trigger, just like a >=3 streak.
    ha = stonks.HeikinAshi()
    stock = _FakeStock()
    stock._indicator_cache[ha.name] = {
        "color":  ["green", "green", "red"],
        "streak": [1, 1, -1],
        "ha_open":  [8, 9.5, 10],
        "ha_high":  [8.2, 10.2, 10.2],  # bar 1: small body, long upper wick
        "ha_low":   [7.8, 9.4, 8],
        "ha_close": [8.1, 9.6, 8.5],
    }
    v = ha.classify(stock, index=2)
    assert v["verdict"].startswith("Bearish HA reversal")


def test_heikin_ashi_no_color_flip_is_neutral():
    ha = stonks.HeikinAshi()
    stock = _FakeStock()
    stock._indicator_cache[ha.name] = {
        "color":  ["green", "green"],
        "streak": [1, 2],
        "ha_open":  [8, 8.1],
        "ha_high":  [9, 9.1],
        "ha_low":   [7.9, 8],
        "ha_close": [8.5, 8.8],
    }
    v = ha.classify(stock, index=1)
    assert v["score"] == 50
    assert v["verdict"] == "green streak=+2"
