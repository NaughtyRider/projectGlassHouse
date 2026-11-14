"""Masking rule: last four digits visible, everything else masked, length preserved."""

import pytest

from glasshouse.masking import FULL_MASK, apply_mask_plan, mask_full, mask_partial


def test_assignment_reference_example():
    assert mask_partial("ABCDE1234F") == "XXXXXX1234"


@pytest.mark.parametrize("value,expected", [
    ("ABCDE1234F", "XXXXXX1234"),              # PAN: serial digits shown, check letter hidden
    ("+919876543210", "XXXXXXXXX3210"),        # phone: same as "last 4 characters"
    ("5010123456780012", "XXXXXXXXXXXX0012"),  # account number: same as "last 4 characters"
    (5010123456780012, "XXXXXXXXXXXX0012"),    # non-string values are masked as text
    ("AB12", "XXXX"),                          # too short to reveal anything
    ("ABCDEFGHIJ", "XXXXXXXXXX"),              # no digits -> fully masked, length kept
    ("ABC12DEF", "XXXXXXXX"),                  # fewer than 4 digits -> fully masked
    ("", ""),
    (None, None),
])
def test_mask_partial(value, expected):
    assert mask_partial(value) == expected


def test_mask_partial_preserves_length_and_reveals_only_four_chars():
    for v in ("ABCDE1234F", "+919876543210", "5010123456780012"):
        m = mask_partial(v)
        assert len(m) == len(v)
        assert len(m.replace("X", "")) <= 4


def test_mask_full():
    assert mask_full("ABCDE1234F") == FULL_MASK and mask_full(None) is None


def test_apply_mask_plan_by_position():
    rows, masked = apply_mask_plan(["id", "pan", "x"], [["C1", "ABCDE1234F", "ABCDE1234F"]], [None, "partial", "full"])
    assert rows == [["C1", "XXXXXX1234", FULL_MASK]] and masked == ["pan", "x"]


def test_apply_mask_plan_shape_mismatch_fails_closed():
    with pytest.raises(ValueError):
        apply_mask_plan(["a", "b"], [["1", "2"]], [None])
