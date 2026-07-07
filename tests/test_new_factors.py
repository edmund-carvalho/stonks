"""
True unit tests (hand-computed expected values, not golden files) for the
two new score factors added in the 2026-07-07 ranking-methodology review:
Momentum12_1Factor and Dist52WkHighFactor. Both are registered at weight
0.0 in FACTOR_WEIGHTS pending a backtest A/B (see PLAN.md) - these tests
pin down their arithmetic in isolation, the same way test_new_indicators.py
does for the Phase 7 indicators. Golden-file coverage over the real
fixtures (compute at weight 0, so purely additive to existing goldens)
lives in test_score_factor_goldens.py.
"""
import pytest

import stonks


class _FakeStock:
    """Minimal stand-in exposing just what score() needs."""
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


# ---------------------------------------------------------------------------
# Momentum12_1Factor
# ---------------------------------------------------------------------------

def test_mom_12_1_matches_hand_computation():
    factor = stonks.Momentum12_1Factor()
    closes = [100.0] * 200
    idx = 199
    # PERIOD=130, SKIP=10: (closes[idx-10] / closes[idx-130] - 1) * 100
    closes[idx - 130] = 80.0
    closes[idx - 10] = 100.0
    stock = _FakeStock(closes=closes)
    expected = (100.0 / 80.0 - 1.0) * 100.0
    assert factor.score(stock, idx) == pytest.approx(expected)


def test_mom_12_1_insufficient_history_is_none():
    factor = stonks.Momentum12_1Factor()
    stock = _FakeStock(closes=[100.0] * 100)  # < PERIOD (130) bars
    assert factor.score(stock, 99) is None


def test_mom_12_1_zero_denominator_is_none():
    factor = stonks.Momentum12_1Factor()
    closes = [100.0] * 200
    closes[199 - 130] = 0.0
    stock = _FakeStock(closes=closes)
    assert factor.score(stock, 199) is None


def test_mom_12_1_negative_index_normalizes_like_positive():
    factor = stonks.Momentum12_1Factor()
    closes = [float(i) for i in range(1, 201)]  # 1..200
    stock = _FakeStock(closes=closes)
    assert factor.score(stock, -1) == factor.score(stock, 199)


# ---------------------------------------------------------------------------
# Dist52WkHighFactor
# ---------------------------------------------------------------------------

def test_dist_52wk_high_at_the_high_is_zero():
    factor = stonks.Dist52WkHighFactor()
    highs = [100.0] * 10
    closes = [100.0] * 10
    stock = _FakeStock(highs=highs, closes=closes)
    assert factor.score(stock, 9) == pytest.approx(0.0)


def test_dist_52wk_high_below_high_matches_hand_computation():
    factor = stonks.Dist52WkHighFactor()
    highs = [100.0] * 10
    closes = [100.0] * 10
    closes[9] = 90.0
    stock = _FakeStock(highs=highs, closes=closes)
    expected = (90.0 / 100.0 - 1.0) * 100.0  # -10.0
    assert factor.score(stock, 9) == pytest.approx(expected)


def test_dist_52wk_high_uses_252_bar_lookback_window():
    factor = stonks.Dist52WkHighFactor()
    n = 300
    highs = [50.0] * n
    highs[10] = 200.0   # a spike more than 252 bars before the end
    closes = [50.0] * n
    stock = _FakeStock(highs=highs, closes=closes)
    # idx=299: lookback window is [299-251, 299] = [48, 299] - excludes
    # the spike at index 10, so the 52wk high should just be 50.0.
    assert factor.score(stock, 299) == pytest.approx(0.0)
    # idx=100: lookback window is [0, 100] - includes the spike at 10.
    expected = (50.0 / 200.0 - 1.0) * 100.0
    assert factor.score(stock, 100) == pytest.approx(expected)


def test_dist_52wk_high_out_of_range_index_is_none():
    factor = stonks.Dist52WkHighFactor()
    stock = _FakeStock(highs=[100.0, 101.0], closes=[100.0, 101.0])
    assert factor.score(stock, -100) is None
    assert factor.score(stock, 50) is None
