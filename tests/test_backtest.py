"""
Tests for the Phase 6 backtest engine (BacktestEngine, _index_as_of,
_build_trading_calendar). Unit tests pin down the forward-return/index
arithmetic against hand-computed values; the mini-backtest golden exercises
the full rebalance loop over the Phase 0 fixture universe deterministically
(no wall-clock dependency - unlike EarningsDate's fundamentals, an all-
technical backtest over fixed historical candles never changes with the
current date).
"""
from datetime import datetime, timedelta, timezone

import pytest

import stonks
from conftest import FIXTURES_DIR, FIXTURE_NAMES
from goldenutils import check_golden, round_floats

IST = timezone(timedelta(hours=5, minutes=30))


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=IST)


class _FakeCandle:
    def __init__(self, timestamp):
        self.timestamp = timestamp


class _FakeStock:
    def __init__(self, candles=None, closes=None, symbol="FAKE"):
        self.candles = candles or []
        self.closes = closes or []
        self.symbol = symbol


class _FakeApp:
    def __init__(self, stocks):
        self.stocks = stocks


def _mk_engine(hold=5, stocks=None):
    app = _FakeApp(stocks or [])
    return stonks.BacktestEngine(
        app, _dt(2020, 1, 1), _dt(2020, 12, 31),
        rebalance=21, top_n=10, hold=hold,
    )


# ---------------------------------------------------------------------------
# _compound_cagr
# ---------------------------------------------------------------------------

def test_compound_cagr_matches_hand_computation():
    # +10% then +10%: equity = 1.1*1.1 = 1.21 over 2 periods of 126 bars
    # each (252 bars total = 1 year) -> CAGR = 21%.
    cagr = stonks._compound_cagr([10.0, 10.0], bars_per_period=126)
    assert cagr == pytest.approx(21.0, abs=1e-9)


def test_compound_cagr_flat_returns_zero():
    cagr = stonks._compound_cagr([0.0, 0.0, 0.0], bars_per_period=21)
    assert cagr == pytest.approx(0.0, abs=1e-9)


def test_compound_cagr_empty_is_none():
    assert stonks._compound_cagr([], bars_per_period=21) is None


def test_compound_cagr_total_wipeout_is_none():
    # A single -100% period drives equity to exactly 0 - undefined CAGR
    # past a total loss, not a crash from a negative-base fractional power.
    assert stonks._compound_cagr([-100.0], bars_per_period=21) is None


def test_compound_cagr_annualizes_by_rebalance_cadence_not_period_count():
    # Same +5% single period, but spanning a full year (252 bars) instead
    # of one month (21 bars) - annualizing should differ a lot.
    monthly = stonks._compound_cagr([5.0], bars_per_period=21)
    yearly = stonks._compound_cagr([5.0], bars_per_period=252)
    assert yearly == pytest.approx(5.0, abs=1e-9)
    assert monthly > yearly  # same raw return compressed into 1/12th the time


# ---------------------------------------------------------------------------
# _index_as_of
# ---------------------------------------------------------------------------

def test_index_as_of_exact_match():
    ts = [_dt(2024, 1, i) for i in (1, 2, 3, 4, 5)]
    assert stonks._index_as_of(ts, _dt(2024, 1, 3)) == 2


def test_index_as_of_between_bars_floors_to_prior_bar():
    ts = [_dt(2024, 1, 1), _dt(2024, 1, 5)]
    assert stonks._index_as_of(ts, _dt(2024, 1, 3)) == 0


def test_index_as_of_before_first_bar_is_none():
    ts = [_dt(2024, 1, 5), _dt(2024, 1, 6)]
    assert stonks._index_as_of(ts, _dt(2024, 1, 1)) is None


def test_index_as_of_after_last_bar_returns_last_index():
    ts = [_dt(2024, 1, 1), _dt(2024, 1, 2)]
    assert stonks._index_as_of(ts, _dt(2099, 1, 1)) == 1


# ---------------------------------------------------------------------------
# _build_trading_calendar
# ---------------------------------------------------------------------------

def test_build_trading_calendar_unions_stocks_within_range():
    s1 = _FakeStock(candles=[_FakeCandle(_dt(2024, 1, 1)), _FakeCandle(_dt(2024, 1, 3))])
    s2 = _FakeStock(candles=[_FakeCandle(_dt(2024, 1, 2)), _FakeCandle(_dt(2024, 1, 5))])
    cal = stonks._build_trading_calendar([s1, s2], _dt(2024, 1, 1), _dt(2024, 1, 3))
    assert cal == [_dt(2024, 1, 1), _dt(2024, 1, 2), _dt(2024, 1, 3)]


def test_build_trading_calendar_excludes_dates_outside_range():
    s1 = _FakeStock(candles=[_FakeCandle(_dt(2024, 1, 1)), _FakeCandle(_dt(2024, 6, 1))])
    cal = stonks._build_trading_calendar([s1], _dt(2024, 1, 1), _dt(2024, 1, 1))
    assert cal == [_dt(2024, 1, 1)]


# ---------------------------------------------------------------------------
# BacktestEngine._forward_return
# ---------------------------------------------------------------------------

def test_forward_return_basic_math():
    engine = _mk_engine(hold=3)
    stock = _FakeStock(closes=[100, 101, 102, 110, 90, 80])
    # idx=1 (close=101), exit_idx=1+3=4 (close=90)
    ret = engine._forward_return(stock, 1)
    assert ret == pytest.approx((90 / 101 - 1) * 100)


def test_forward_return_none_when_holding_window_runs_off_the_end():
    engine = _mk_engine(hold=5)
    stock = _FakeStock(closes=[100, 101, 102])
    assert engine._forward_return(stock, 1) is None  # exit_idx=6, out of range


def test_forward_return_boundary_exit_idx_equals_last_valid_index():
    engine = _mk_engine(hold=2)
    stock = _FakeStock(closes=[100, 101, 102])
    # idx=0, exit_idx=2 == len-1 -> still valid (not off the end)
    assert engine._forward_return(stock, 0) == pytest.approx((102 / 100 - 1) * 100)
    # idx=1, exit_idx=3 == len(closes) -> off the end
    assert engine._forward_return(stock, 1) is None


def test_forward_return_none_on_zero_entry_price():
    engine = _mk_engine(hold=1)
    stock = _FakeStock(closes=[0, 100])
    assert engine._forward_return(stock, 0) is None


# ---------------------------------------------------------------------------
# Mini-backtest golden: full rebalance loop over the fixture universe
# ---------------------------------------------------------------------------

def test_mini_backtest_golden(regen):
    """
    Deterministic, all-technical (fund_weight=0) backtest over the 4
    Phase 0 fixtures. Window chosen so every fixture (including the
    latest-starting, SHORTHIST at 2025-08-26) has bars from day one -
    no partial-universe edge case here (that's covered by the
    _index_as_of unit tests above).
    """
    app = stonks.Stonks(FIXTURES_DIR, precompute_mode=stonks.PreComputeMode.PCM_TECHNICAL)
    assert {s.symbol for s in app.stocks} == set(FIXTURE_NAMES)

    engine = stonks.BacktestEngine(
        app, _dt(2025, 8, 26), _dt(2026, 6, 2),
        rebalance=21, top_n=2, hold=10,
        tech_weight=1.0, fund_weight=0.0,
    )
    result = engine.run()
    check_golden("mini_backtest", round_floats(result), regen)


def test_mini_backtest_skips_picks_whose_forward_window_runs_off_the_end():
    """A rebalance date right at the end of history can't have a full
    `hold`-bar forward window - those picks must show fwd_return=None
    rather than crashing or fabricating a value from missing bars. A
    single-rebalance engine (rebalance window wider than the data span)
    anchored at the very last available bar guarantees this deterministically."""
    app = stonks.Stonks(FIXTURES_DIR, precompute_mode=stonks.PreComputeMode.PCM_TECHNICAL)
    last_bar = max(c.timestamp for s in app.stocks for c in s.candles)
    engine = stonks.BacktestEngine(
        app, last_bar, last_bar,
        rebalance=1, top_n=2, hold=10,
        tech_weight=1.0, fund_weight=0.0,
    )
    result = engine.run()
    assert len(result["rebalances"]) == 1
    picks = result["rebalances"][0]["picks"]
    assert all(p["fwd_return"] is None for p in picks)
    assert result["summary"]["valid_pick_count"] == 0
    assert result["summary"]["pick_count"] == len(picks)
