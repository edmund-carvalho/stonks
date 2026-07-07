"""
True unit tests (independent expected values, not golden files) for the
small pure-function helpers used by the scoring engine: xnorm, winsorize,
_sma_val, _rolling_ret, _ann_vol.
"""
import math

import pytest

from stonks import xnorm, winsorize, _sma_val, _rolling_ret, _ann_vol


# ---------------------------------------------------------------------------
# xnorm
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


# ---------------------------------------------------------------------------
# winsorize
# ---------------------------------------------------------------------------

def test_winsorize_fewer_than_10_valid_returns_unchanged():
    values = [1.0, 2.0, None, 4.0, 1000.0]
    assert winsorize(values) == values


def test_winsorize_clips_a_single_extreme_outlier():
    # 99 normal values + 1 extreme outlier -> at n=100 the default 2.5/97.5
    # percentile indices (2 and 97) both land inside the "normal" block, so
    # lo == hi == 10.0 and the outlier gets clipped down to 10.0 while every
    # normal value passes through unchanged.
    values = [10.0] * 99 + [1000.0]
    result = winsorize(values)
    assert result[:99] == [10.0] * 99
    assert result[99] == 10.0


def test_winsorize_preserves_none_positions():
    values = [10.0] * 50 + [None] * 5 + [10.0] * 50
    result = winsorize(values)
    assert result[50:55] == [None] * 5


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
