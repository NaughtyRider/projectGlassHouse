"""Result-set comparison and metrics. Pure functions, no I/O, so they're easy to unit-test.

Execution accuracy compares *results*, never SQL text:
  * rows are compared as a multiset (duplicate counts matter) unless the gold query's root has
    ORDER BY, in which case row order must match too;
  * columns are compared by position (aliases may differ freely), and column count must match;
  * values are normalised so equivalent SQL isn't penalised for representation:
    1 == 1.0, floats compared after rounding to 4 decimal places.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Sequence

import sqlglot
from sqlglot import exp

FLOAT_DECIMALS = 4


def normalize_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        f = float(v)
        if math.isnan(f):
            return "NaN"
        return round(f, FLOAT_DECIMALS) + 0.0  # +0.0 folds -0.0 into 0.0
    return str(v)


def normalize_rows(rows: Sequence[Sequence[Any]]) -> list[tuple]:
    return [tuple(normalize_value(v) for v in row) for row in rows]


def gold_is_ordered(gold_sql: str) -> bool:
    """True if the gold query's *outermost* query has ORDER BY (parsed, not string-matched)."""
    tree = sqlglot.parse_one(gold_sql, read="sqlite")
    return tree.args.get("order") is not None


def compare_results(
    actual_rows: Sequence[Sequence[Any]],
    gold_rows: Sequence[Sequence[Any]],
    ordered: bool,
    actual_columns: Sequence[str] | None = None,
    gold_columns: Sequence[str] | None = None,
) -> tuple[bool, str | None]:
    """Return (correct, reason_if_not)."""
    if actual_columns is not None and gold_columns is not None and len(actual_columns) != len(gold_columns):
        return False, f"column count differs: got {len(actual_columns)}, expected {len(gold_columns)}"
    a, g = normalize_rows(actual_rows), normalize_rows(gold_rows)
    if a and g and len(a[0]) != len(g[0]):
        return False, f"column count differs: got {len(a[0])}, expected {len(g[0])}"
    if len(a) != len(g):
        return False, f"row count differs: got {len(a)}, expected {len(g)}"
    if ordered:
        if a != g:
            first = next(i for i, (x, y) in enumerate(zip(a, g)) if x != y)
            same_set = Counter(a) == Counter(g)
            what = "row order differs (gold has ORDER BY)" if same_set else "row values differ"
            return False, f"{what}; first mismatch at row {first}: got {a[first]}, expected {g[first]}"
        return True, None
    ca, cg = Counter(a), Counter(g)
    if ca != cg:
        missing = list((cg - ca).elements())[:1]
        extra = list((ca - cg).elements())[:1]
        return False, f"result multiset differs; missing e.g. {missing}, unexpected e.g. {extra}"
    return True, None


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation): the smallest value >= pct% of observations."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]
