"""
Tests for the --debug swallowed-exception logging added in Phase 3
(see PLAN.md) - verifies the dedup behavior of _log_swallowed() directly,
and that get_indicator's KeyError (unknown indicator name = programmer
error) propagates out of the factor-scoring loop in rank_stocks_xnorm
instead of being silently swallowed like a normal per-stock data gap.
"""
import logging

import pytest

import stonks


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    """_LOGGED_SWALLOWED is module-global (dedup is meant to persist across
    a whole run) - reset it around each test so tests don't see stale
    dedup entries left by an earlier test or fixture load in this session."""
    stonks._LOGGED_SWALLOWED.clear()
    yield
    stonks._LOGGED_SWALLOWED.clear()


def test_log_swallowed_dedupes_same_site_and_symbol(caplog):
    with caplog.at_level(logging.DEBUG, logger="stonks"):
        stonks._log_swallowed("some_site", "SYM", ValueError("boom"))
        stonks._log_swallowed("some_site", "SYM", ValueError("boom again"))
    assert len(caplog.records) == 1
    assert "SYM" in caplog.records[0].message


def test_log_swallowed_logs_separately_per_symbol(caplog):
    with caplog.at_level(logging.DEBUG, logger="stonks"):
        stonks._log_swallowed("some_site", "SYM_A", ValueError("boom"))
        stonks._log_swallowed("some_site", "SYM_B", ValueError("boom"))
    assert len(caplog.records) == 2


def test_log_swallowed_logs_separately_per_site(caplog):
    with caplog.at_level(logging.DEBUG, logger="stonks"):
        stonks._log_swallowed("site_a", "SYM", ValueError("boom"))
        stonks._log_swallowed("site_b", "SYM", ValueError("boom"))
    assert len(caplog.records) == 2


def test_factor_scoring_keyerror_propagates(load_stock, monkeypatch):
    """An unknown-indicator-name KeyError from inside a factor's score()
    must not be swallowed alongside legitimate per-stock data gaps."""
    stock = load_stock("LARGECAP_TREND")
    app = stonks.Stonks.__new__(stonks.Stonks)
    app.stocks = [stock]

    factor = next(f for f in stonks.SCORE_FACTORS if f.name == "trend_ma")

    def _boom(self, stock, index=-1):
        raise KeyError("Indicator 'RSI(41)' not found in stock LARGECAP_TREND")

    monkeypatch.setattr(type(factor), "score", _boom)

    with pytest.raises(KeyError):
        app.rank_stocks_xnorm(tech_weight=1.0, fund_weight=0.0)
