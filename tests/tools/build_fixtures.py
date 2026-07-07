"""
One-off script to (re)generate tests/fixtures/*.json from real candle data.

Not part of the pytest suite and not run automatically - fixtures are
checked into git as static files. Re-run manually only if you intentionally
want to refresh the fixture data (e.g. swap in a different source stock).

Usage (from repo root):
    python tests/tools/build_fixtures.py
"""
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")

# (output fixture name, source file, candle window)
# Window is applied as data[-n:] (most recent n candles), or None for "all".
SOURCES = [
    ("LARGECAP_TREND.json", os.path.join(REPO_ROOT, "data", "WIPRO.json"), 300),
    ("SMALLCAP_CHOP.json",  os.path.join(REPO_ROOT, "data", "AARTIDRUGS.json"), 300),
    ("INDEX_NOFUND.json",   os.path.join(REPO_ROOT, "candles", "NIFTY 50.json"), 300),
    ("SHORTHIST.json",      os.path.join(REPO_ROOT, "data", "VIKRAMSOLR.json"), None),
]


def build():
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    for out_name, src_path, window in SOURCES:
        with open(src_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data = raw.get("data", [])
        if window is not None:
            data = data[-window:]
        trimmed = {"metadata": raw.get("metadata", {}), "data": data}
        out_path = os.path.join(FIXTURES_DIR, out_name)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, indent=2)
        print(f"{out_name}: {len(data)} candles  (source: {os.path.basename(src_path)})")


if __name__ == "__main__":
    build()
