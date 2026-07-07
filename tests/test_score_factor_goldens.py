"""
Characterization goldens for BaseScoreFactor.score() - the raw, un-normalised
inputs to cross-sectional ranking (see FACTOR_WEIGHTS / rank_stocks_xnorm).
"""
import pytest

from goldenutils import check_golden, round_floats
from conftest import FIXTURE_NAMES


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_score_factors_golden(fixture_name, load_stock, regen):
    stock = load_stock(fixture_name)

    out = {}
    for factor in stock.scoreFactors:
        try:
            out[factor.name] = round_floats(factor.score(stock))
        except Exception as e:
            out[factor.name] = {"__error__": f"{type(e).__name__}: {e}"}

    check_golden(f"{fixture_name}_factors", out, regen)
