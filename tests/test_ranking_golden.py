"""
Characterization goldens for Stonks.rank_stocks_xnorm() - the two-pass
cross-sectional ranking engine, using percentile-rank normalization and
per-stock weight renormalization over available (non-None) factors/
sub-categories (see PLAN.md Phase 3).
"""
import os

import stonks
from goldenutils import check_golden, round_floats
from conftest import FIXTURE_NAMES, FIXTURES_DIR


def _dump_ranking(results):
    """results: List[Tuple[Stock, Dict]] from rank_stocks_xnorm, already
    sorted by overall score descending."""
    return [
        {
            "symbol": stock.symbol,
            "overall": r["overall"],
            "ta_composite": r["ta_composite"],
            "fa_composite": r["fa_composite"],
            "low_coverage": r.get("low_coverage"),
            "gate_mom": r.get("gate_mom"),
            "gate_trend": r.get("gate_trend"),
            "gate_adx": r.get("gate_adx"),
            "gate_mfi": r.get("gate_mfi"),
            "gate_ichi": r.get("gate_ichi"),
            "gate_bb_ttm": r.get("gate_bb_ttm"),
        }
        for stock, r in results
    ]


def test_ranking_default_weights_golden(regen):
    """Whole fixture universe (4 stocks), default README weights."""
    app = stonks.Stonks(FIXTURES_DIR)
    assert {s.symbol for s in app.stocks} == set(FIXTURE_NAMES)

    results = app.rank_stocks_xnorm(tech_weight=0.85, fund_weight=0.15)
    check_golden("ranking_default_weights", round_floats(_dump_ranking(results)), regen)


def test_ranking_fund_weight_zero_golden(regen):
    """Pure-technical ranking (fund_weight=0) over the same universe."""
    app = stonks.Stonks(FIXTURES_DIR)
    results = app.rank_stocks_xnorm(tech_weight=1.0, fund_weight=0.0)
    check_golden("ranking_fund_weight_zero", round_floats(_dump_ranking(results)), regen)


def test_ranking_low_coverage_flag_golden(regen):
    """With fundamentals weighted heavily and INDEX_NOFUND having zero real
    fundamental data (all sub-categories None, see _fund_sub_scores), its
    blended coverage should drop below the 60% threshold and get flagged -
    unlike the default 0.15 fund_weight, where technical data alone (100%
    covered) keeps blended coverage comfortably above the threshold."""
    app = stonks.Stonks(FIXTURES_DIR)
    results = app.rank_stocks_xnorm(tech_weight=0.0, fund_weight=0.7)
    dumped = _dump_ranking(results)
    index_row = next(r for r in dumped if r["symbol"] == "INDEX_NOFUND")
    assert index_row["low_coverage"] is True
    others = [r for r in dumped if r["symbol"] != "INDEX_NOFUND"]
    assert all(r["low_coverage"] is False for r in others)
    check_golden("ranking_low_coverage_flag", round_floats(dumped), regen)


def test_ranking_single_stock_bypass_golden(regen):
    """rank_stocks_xnorm has a documented single-stock bypass: with only one
    stock in the universe, xnorm/min-max would collapse every factor to 50,
    so raw factor/sub-scores are used directly instead (stonks.py ~4499)."""
    single_fixture_path = os.path.join(FIXTURES_DIR, "LARGECAP_TREND.json")
    app = stonks.Stonks(single_fixture_path)
    assert len(app.stocks) == 1

    results = app.rank_stocks_xnorm(tech_weight=0.85, fund_weight=0.15)
    check_golden("ranking_single_stock_bypass", round_floats(_dump_ranking(results)), regen)
