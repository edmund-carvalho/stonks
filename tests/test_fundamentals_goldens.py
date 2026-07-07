"""
Characterization goldens for BaseFundamentalIndicator.compute() / .classify().
"""
import pytest

from goldenutils import check_golden, round_floats
from conftest import FIXTURE_NAMES


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_fundamentals_golden(fixture_name, load_stock, regen):
    stock = load_stock(fixture_name)

    out = {}
    for f in stock.fundamentals:
        try:
            raw = f.compute(stock)
            verdict = f.classify(stock, raw_value=raw)
        except Exception as e:
            out[f.name] = {"__error__": f"{type(e).__name__}: {e}"}
            continue
        out[f.name] = round_floats({
            "raw": raw,
            "verdict": verdict.get("verdict"),
            "score": verdict.get("score"),
            "result": verdict.get("result", {}),
        })

    check_golden(f"{fixture_name}_fundamentals", out, regen)


def test_index_fixture_has_no_fundamentals(load_stock):
    """INDEX_NOFUND carries metadata.fundamentals = {'index': True, ...} -
    every registered fundamental should degrade to an 'N/A' verdict rather
    than raising or fabricating a value. Some compute() implementations
    return a bare None (e.g. TrailingPE), others a tuple of Nones (e.g.
    FiftyTwoWeekPosition) - classify() is the stable contract to check, not
    the raw shape. Score is NOT asserted here: most N/A verdicts score 0,
    but MarketCap intentionally scores a neutral 50 for N/A too (it's
    display-only, not ranked) - see the per-fixture golden for exact scores."""
    stock = load_stock("INDEX_NOFUND")
    for f in stock.fundamentals:
        raw = f.compute(stock)
        verdict = f.classify(stock, raw_value=raw)
        assert verdict.get("verdict") == "N/A", (
            f"{f.name} produced a non-N/A verdict for an index fixture: {verdict!r} (raw={raw!r})"
        )
