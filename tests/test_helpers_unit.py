"""
True unit tests (independent expected values, not golden files) for the
small pure-function helpers used by the scoring engine: xnorm,
_weighted_composite, rolling_min_max, _sma_val, _rolling_ret, _ann_vol.
"""
import math
import random

import pytest

from stonks import xnorm, _weighted_composite, rolling_min_max, _sma_val, _rolling_ret, _ann_vol


# ---------------------------------------------------------------------------
# xnorm (percentile-rank normalization - see Phase 3, PLAN.md)
# ---------------------------------------------------------------------------

def test_xnorm_empty():
    assert xnorm({}) == {}


def test_xnorm_all_none():
    assert xnorm({"a": None, "b": None}) == {"a": 50.0, "b": 50.0}


def test_xnorm_single_value_collapses_to_50():
    assert xnorm({"a": 5.0}) == {"a": 50.0}


def test_xnorm_tied_values_collapse_to_50():
    assert xnorm({"a": 5.0, "b": 5.0}) == {"a": 50.0, "b": 50.0}


def test_xnorm_normal_spread():
    result = xnorm({"a": 0.0, "b": 5.0, "c": 10.0})
    assert result == {"a": 0.0, "b": 50.0, "c": 100.0}


def test_xnorm_none_maps_to_50_alongside_real_spread():
    result = xnorm({"a": None, "b": 0.0, "c": 10.0})
    assert result == {"a": 50.0, "b": 0.0, "c": 100.0}


def test_xnorm_three_way_tie_gets_average_rank():
    # a=1 (lowest), b/c/d tied in the middle, e=10 (highest). The tied trio
    # shares the average of ranks 1-3 (0-based: 1,2,3 -> avg 2), which maps
    # to the same percentile a single untied middle value would get.
    result = xnorm({"a": 1.0, "b": 5.0, "c": 5.0, "d": 5.0, "e": 10.0})
    assert result == {"a": 0.0, "b": 50.0, "c": 50.0, "d": 50.0, "e": 100.0}


def test_xnorm_outlier_does_not_compress_the_rest_of_the_distribution():
    # This is the whole point of switching from min-max to percentile-rank:
    # under min-max, the extreme "d" value would compress a/b/c to ~0-0.2,
    # making them look nearly identical despite being evenly spaced.
    result = xnorm({"a": 1.0, "b": 2.0, "c": 3.0, "d": 1000.0})
    assert result == pytest.approx({"a": 0.0, "b": 100.0 / 3, "c": 200.0 / 3, "d": 100.0})
    # Evenly spaced non-outlier values remain evenly spaced in the output.
    assert result["b"] - result["a"] == pytest.approx(result["c"] - result["b"])


# ---------------------------------------------------------------------------
# _weighted_composite (per-stock weight renormalization over available data)
# ---------------------------------------------------------------------------

def test_weighted_composite_full_coverage_uses_normed_values():
    weights = {"f1": 0.5, "f2": 0.5}
    raw     = {"f1": {"x": 10.0}, "f2": {"x": 20.0}}
    normed  = {"f1": {"x": 100.0}, "f2": {"x": 0.0}}
    composite, coverage = _weighted_composite("x", weights, raw, normed, use_raw=False)
    assert composite == pytest.approx(50.0)
    assert coverage == pytest.approx(1.0)


def test_weighted_composite_renormalizes_over_available_factors():
    # f1 is missing (raw None) for this stock - it must be excluded rather
    # than defaulted to a neutral 50, and f2's weight must absorb all of
    # f1's share (0.7/0.7 = full weight) instead of only counting for 70%.
    weights = {"f1": 0.3, "f2": 0.7}
    raw     = {"f1": {"x": None}, "f2": {"x": 20.0}}
    normed  = {"f1": {"x": 50.0}, "f2": {"x": 80.0}}
    composite, coverage = _weighted_composite("x", weights, raw, normed, use_raw=False)
    assert composite == pytest.approx(80.0)
    assert coverage == pytest.approx(0.7)


def test_weighted_composite_zero_coverage_returns_neutral():
    weights = {"f1": 1.0}
    raw     = {"f1": {"x": None}}
    normed  = {"f1": {"x": 50.0}}
    composite, coverage = _weighted_composite("x", weights, raw, normed, use_raw=False)
    assert composite == 50.0
    assert coverage == 0.0


def test_weighted_composite_use_raw_bypasses_normed():
    # This is the single-stock ranking bypass: normed values are meaningless
    # (xnorm collapses everything to 50 with only one stock in the universe),
    # so use_raw=True must read straight from `raw`, ignoring `normed`.
    weights = {"f1": 1.0}
    raw     = {"f1": {"x": 42.0}}
    normed  = {"f1": {"x": 999.0}}
    composite, coverage = _weighted_composite("x", weights, raw, normed, use_raw=True)
    assert composite == pytest.approx(42.0)
    assert coverage == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# rolling_min_max (O(n) monotonic-deque rolling window, replacing the
# O(n*window) copy-paste previously duplicated in MACD/ATR/ADX/AnnVol)
# ---------------------------------------------------------------------------

def _naive_rolling_min_max(values, window, min_valid=5):
    """Reference implementation matching the pre-Phase-4 per-bar
    slice-and-rescan that was copy-pasted across 4 indicators."""
    n = len(values)
    lo = [None] * n
    hi = [None] * n
    for i in range(n):
        start = max(0, i - window + 1)
        valid = [v for v in values[start:i + 1] if v is not None]
        if len(valid) >= min_valid:
            lo[i] = min(valid)
            hi[i] = max(valid)
    return lo, hi


def test_rolling_min_max_empty():
    assert rolling_min_max([], window=10) == ([], [])


def test_rolling_min_max_all_none():
    lo, hi = rolling_min_max([None, None, None], window=2, min_valid=1)
    assert lo == [None, None, None]
    assert hi == [None, None, None]


def test_rolling_min_max_below_min_valid_stays_none():
    lo, hi = rolling_min_max([1.0, 2.0], window=5, min_valid=5)
    assert lo == [None, None]
    assert hi == [None, None]


def test_rolling_min_max_simple_window():
    # window=3, min_valid=1: at i=4 the trailing window is values[2:5]=[3,4,5]
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    lo, hi = rolling_min_max(values, window=3, min_valid=1)
    assert lo == [1.0, 1.0, 1.0, 2.0, 3.0]
    assert hi == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_rolling_min_max_none_entries_are_invisible_not_zero():
    # A None in the window must be skipped entirely, not treated as 0 or
    # counted toward min_valid - matching every existing caller's intent
    # (a sentinel/no-data bar shouldn't distort the historical range).
    values = [10.0, None, None, 20.0, 5.0]
    lo, hi = rolling_min_max(values, window=5, min_valid=1)
    assert lo == [10.0, 10.0, 10.0, 10.0, 5.0]
    assert hi == [10.0, 10.0, 10.0, 20.0, 20.0]


@pytest.mark.parametrize("seed", range(20))
def test_rolling_min_max_matches_naive_on_random_data(seed):
    rnd = random.Random(seed)
    n = rnd.randint(0, 60)
    window = rnd.randint(1, 25)
    min_valid = rnd.randint(1, 6)
    values = [
        rnd.choice([None, None, round(rnd.uniform(-100, 100), 3)])
        for _ in range(n)
    ]
    assert rolling_min_max(values, window, min_valid) == _naive_rolling_min_max(values, window, min_valid)


# ---------------------------------------------------------------------------
# _sma_val
# ---------------------------------------------------------------------------

def test_sma_val_insufficient_history_returns_none():
    assert _sma_val([1, 2, 3, 4, 5], period=3, index=1) is None


def test_sma_val_at_minimum_valid_index():
    assert _sma_val([1, 2, 3, 4, 5], period=3, index=2) == pytest.approx(2.0)


def test_sma_val_normal_case():
    assert _sma_val([1, 2, 3, 4, 5], period=3, index=4) == pytest.approx(4.0)


def test_sma_val_negative_index():
    closes = [1, 2, 3, 4, 5]
    assert _sma_val(closes, period=3, index=-1) == _sma_val(closes, period=3, index=4)


# ---------------------------------------------------------------------------
# _rolling_ret
# ---------------------------------------------------------------------------

def test_rolling_ret_insufficient_history_returns_none():
    assert _rolling_ret([100, 110, 121], period=1, index=0) is None


def test_rolling_ret_normal_case():
    result = _rolling_ret([100, 110, 121], period=1, index=1)
    assert result == pytest.approx(10.0)


def test_rolling_ret_zero_denominator_returns_none():
    assert _rolling_ret([0, 50], period=1, index=1) is None


# ---------------------------------------------------------------------------
# _ann_vol
# ---------------------------------------------------------------------------

def test_ann_vol_insufficient_history_returns_none():
    assert _ann_vol([1, 2], period=5, index=1) is None


def test_ann_vol_fewer_than_2_valid_returns_returns_none():
    # closes[0] == 0 makes the first candidate return unusable, leaving only
    # 1 valid return in the window - below the len(rets) >= 2 requirement.
    assert _ann_vol([0, 10, 20], period=2, index=2) is None


def test_ann_vol_normal_case_matches_manual_formula():
    closes = [1.0, 2.0, 1.0, 2.0]
    result = _ann_vol(closes, period=3, index=3)
    # rets = [1.0, -0.5, 1.0] -> mean=0.5, sample var=0.75
    expected = math.sqrt(0.75 * 252) * 100.0
    assert result == pytest.approx(expected)
