"""
Shared helpers for golden-file (characterization) tests.

Goldens capture *current* behavior of stonks.py, bugs included - they are a
change detector for refactors, not a correctness oracle. See PLAN.md Phase 0.
"""
import json
import math
import os

GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goldens")

ROUND_NDIGITS = 6


def round_floats(obj):
    """Recursively round floats in nested dict/list structures for stable goldens."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return str(obj)  # JSON has no NaN/Inf; make it explicit and stable
        return round(obj, ROUND_NDIGITS)
    if isinstance(obj, dict):
        return {k: round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v) for v in obj]
    return obj


def golden_path(name: str) -> str:
    return os.path.join(GOLDENS_DIR, f"{name}.json")


def check_golden(name: str, data, regen: bool):
    """
    Compare `data` (already JSON-safe, e.g. via round_floats) against the
    stored golden file `name`. If `regen` is True, or the golden doesn't
    exist yet, write it instead of comparing.
    """
    path = golden_path(name)
    if regen or not os.path.exists(path):
        os.makedirs(GOLDENS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        return

    with open(path, "r", encoding="utf-8") as f:
        expected = json.load(f)

    # Round-trip `data` through JSON so comparison is apples-to-apples
    # (e.g. tuples become lists, matching what's stored on disk).
    actual = json.loads(json.dumps(data, sort_keys=True))

    assert actual == expected, (
        f"Golden mismatch for '{name}'. If this change is intentional, "
        f"regenerate with: pytest --regen-goldens -k {name!r}"
    )
