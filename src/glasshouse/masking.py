"""Output masking. Which positions to mask is decided by lineage in access.py; this module
only transforms values."""

from __future__ import annotations

from typing import Any, Sequence

from glasshouse.access import MaskMode

MASK_CHAR = "X"
FULL_MASK = "XXXXXXXX"  # fixed width: a fully masked value reveals nothing, not even its length
VISIBLE_TAIL = 4


def mask_partial(value: Any) -> Any:
    """Show only the last 4 *digits*, right-aligned; mask everything else; preserve length.

        PAN            'ABCDE1234F'     -> 'XXXXXX1234'      (assignment's reference example)
        phone          '+919876543210'  -> 'XXXXXXXXX3210'
        account number '5010123456780012' -> 'XXXXXXXXXXXX0012'

    For phone and account numbers this equals "keep the last 4 characters"; for PAN it hides
    the trailing check letter and shows the 4-digit serial instead. Values with fewer than 4
    digits, or no longer than 4 characters, are masked completely (nothing to reveal safely).
    """
    if value is None:
        return None
    s = str(value)
    digits = [c for c in s if c.isdigit()]
    if len(s) <= VISIBLE_TAIL or len(digits) < VISIBLE_TAIL:
        return MASK_CHAR * len(s)
    return MASK_CHAR * (len(s) - VISIBLE_TAIL) + "".join(digits[-VISIBLE_TAIL:])


def mask_full(value: Any) -> Any:
    return None if value is None else FULL_MASK


def apply_mask_plan(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], plan: Sequence[MaskMode | None]
) -> tuple[list[list[Any]], list[str]]:
    """Mask row values by output position. Returns (masked_rows, masked_column_names)."""
    if len(plan) != len(columns):
        # Lineage and result disagree on shape: refuse rather than guess which column is which.
        raise ValueError(f"mask plan covers {len(plan)} columns but result has {len(columns)}")
    fns = [mask_partial if m == "partial" else mask_full if m == "full" else None for m in plan]
    masked_rows = [[fn(v) if fn else v for v, fn in zip(row, fns)] for row in rows]
    masked_names = [c for c, m in zip(columns, plan) if m is not None]
    return masked_rows, masked_names
