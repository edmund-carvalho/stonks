"""
Characterization goldens for BaseTechnicalIndicator.compute() and .classify().

These lock in *current* output (bugs included) so refactors and bug fixes can
be verified against an intentional, reviewed diff instead of by eyeballing
terminal tables. See PLAN.md Phase 0 / Phase 1.
"""
import pytest

from goldenutils import check_golden, round_floats
from conftest import FIXTURE_NAMES


def _mid_index(n):
    """A historical (non-last-bar) index used to catch look-ahead bugs
    like the ADX +DI/-DI backfill (PLAN.md Phase 1, Bug B)."""
    return max(0, n - 100)


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_indicator_series_golden(fixture_name, load_stock, regen):
    stock = load_stock(fixture_name)

    series = {}
    for ind in stock.indicators:
        try:
            series[ind.name] = round_floats(ind.compute(stock))
        except Exception as e:  # characterize failures too - don't hide them
            series[ind.name] = {"__error__": f"{type(e).__name__}: {e}"}

    check_golden(f"{fixture_name}_indicators", series, regen)


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_indicator_classify_golden(fixture_name, load_stock, regen):
    stock = load_stock(fixture_name)
    n = len(stock.candles)
    mid = _mid_index(n)

    classify_out = {}
    for ind in stock.indicators:
        for label, idx in (("last", n - 1), ("mid", mid)):
            key = f"{ind.name}@{label}"
            try:
                verdict = ind.classify(stock, idx)
            except Exception as e:
                classify_out[key] = {"__error__": f"{type(e).__name__}: {e}"}
                continue
            # color is an ANSI code (blanked out for the test session, see
            # conftest.py) - keep the key so a future color regression on a
            # test-disabled run is still visible via non-empty string.
            classify_out[key] = round_floats({
                "verdict": verdict.get("verdict"),
                "score": verdict.get("score"),
                "color_blanked": verdict.get("color") == "",
                "result": verdict.get("result", {}),
            })

    check_golden(f"{fixture_name}_classify", classify_out, regen)
