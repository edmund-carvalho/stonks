import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import stonks  # noqa: E402  (path must be set up first)

# Goldens must never contain ANSI color codes - disable once, globally, for
# the whole test session so golden output is stable across --no-color states.
stonks.CLR.disable()

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

FIXTURE_NAMES = [
    "LARGECAP_TREND",   # WIPRO, LARGE cap, 300 bars, trending
    "SMALLCAP_CHOP",    # AARTIDRUGS, SMALL cap, 300 bars, choppy
    "INDEX_NOFUND",     # NIFTY 50, 300 bars, no fundamentals
    "SHORTHIST",        # VIKRAMSOLR, 188 bars (< HIST_WINDOW), warmup edge cases
]


def pytest_addoption(parser):
    parser.addoption(
        "--regen-goldens",
        action="store_true",
        default=False,
        help="Regenerate golden files instead of asserting against them.",
    )


@pytest.fixture
def regen(request):
    return request.config.getoption("--regen-goldens")


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


def _fixture_path(name):
    return os.path.join(FIXTURES_DIR, f"{name}.json")


@pytest.fixture
def load_stock():
    """Factory fixture: load_stock('LARGECAP_TREND') -> Stock, fully precomputed."""
    def _load(name, precompute_mode=stonks.PreComputeMode.PCM_ALL):
        stock = stonks.StockFactory.from_json_file(_fixture_path(name))
        assert stock is not None, f"Fixture '{name}' produced no candles"
        stock.precompute(mode=precompute_mode)
        return stock
    return _load
