"""
Tests for --weights TOML loading (load_weights_file) added in Phase 5.
See PLAN.md and weights.example.toml.
"""
import os

import pytest

import stonks
from goldenutils import check_golden, round_floats
from conftest import FIXTURES_DIR


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write_toml(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_example_weights_file_matches_builtin_defaults():
    """weights.example.toml is documented as mirroring the in-code
    defaults exactly - if it drifts, --weights weights.example.toml would
    silently stop being a no-op."""
    example_path = os.path.join(REPO_ROOT, "weights.example.toml")
    fw, sfw, fund_weight = stonks.load_weights_file(example_path)
    assert fw == stonks.FACTOR_WEIGHTS
    assert sfw == stonks.FUNDAMENTAL_SUB_WEIGHTS
    assert fund_weight == pytest.approx(0.15)


def test_partial_override_keeps_other_defaults(tmp_path):
    path = _write_toml(tmp_path, "w.toml", """
[factor_weights]
bb_entry = 0.30
""")
    fw, sfw, fund_weight = stonks.load_weights_file(path)
    assert sfw == stonks.FUNDAMENTAL_SUB_WEIGHTS
    assert fund_weight is None
    # Other factor_weights keys keep their built-in default value, but the
    # whole dict is renormalized to sum to 1.0 since 0.30 pushed the total
    # above 1 - so bb_entry's *share* should still be the largest.
    assert fw["bb_entry"] == max(fw.values())


def test_unknown_factor_weight_key_raises(tmp_path):
    path = _write_toml(tmp_path, "w.toml", """
[factor_weights]
bb_entrry = 0.5
""")
    with pytest.raises(ValueError, match="Unknown factor_weights key"):
        stonks.load_weights_file(path)


def test_unknown_fundamental_sub_weight_key_raises(tmp_path):
    path = _write_toml(tmp_path, "w.toml", """
[fundamental_sub_weights]
f_valuatoin = 0.5
""")
    with pytest.raises(ValueError, match="Unknown fundamental_sub_weights key"):
        stonks.load_weights_file(path)


def test_non_positive_weights_raise(tmp_path):
    path = _write_toml(tmp_path, "w.toml", """
[factor_weights]
trend_ma = 0.0
rsi_quality = 0.0
crossover = 0.0
volume_trend = 0.0
bb_entry = 0.0
pullback_entry = 0.0
overextension = 0.0
pivot_proximity = 0.0
fib_retrace = 0.0
bullish_setup = 0.0
gate_strength = 0.0
""")
    with pytest.raises(ValueError, match="must be > 0"):
        stonks.load_weights_file(path)


def test_blend_fund_weight_is_read(tmp_path):
    path = _write_toml(tmp_path, "w.toml", """
[blend]
fund_weight = 0.4
""")
    fw, sfw, fund_weight = stonks.load_weights_file(path)
    assert fund_weight == pytest.approx(0.4)
    assert fw == stonks.FACTOR_WEIGHTS
    assert sfw == stonks.FUNDAMENTAL_SUB_WEIGHTS


def test_missing_blend_section_returns_none(tmp_path):
    path = _write_toml(tmp_path, "w.toml", """
[factor_weights]
bb_entry = 0.5
""")
    _, _, fund_weight = stonks.load_weights_file(path)
    assert fund_weight is None


def test_ranking_with_custom_weights_golden(tmp_path, regen):
    """A heavily-skewed weights file over the fixture universe should
    produce a materially different ranking than the defaults - this is
    the fixture-universe golden variant called for in PLAN.md Phase 5."""
    path = _write_toml(tmp_path, "w.toml", """
[factor_weights]
bb_entry = 0.9
trend_ma = 0.02
rsi_quality = 0.02
crossover = 0.02
volume_trend = 0.01
pullback_entry = 0.01
overextension = 0.005
pivot_proximity = 0.005
fib_retrace = 0.005
bullish_setup = 0.005
gate_strength = 0.005

[blend]
fund_weight = 0.0
""")
    fw, sfw, fund_weight = stonks.load_weights_file(path)
    app = stonks.Stonks(FIXTURES_DIR)
    results = app.rank_stocks_xnorm(
        tech_weight=1.0 - fund_weight, fund_weight=fund_weight,
        factor_weights=fw, fundamental_sub_weights=sfw,
    )
    dumped = [
        {"symbol": stock.symbol, "overall": r["overall"], "ta_composite": r["ta_composite"]}
        for stock, r in results
    ]
    check_golden("ranking_custom_weights_file", round_floats(dumped), regen)
