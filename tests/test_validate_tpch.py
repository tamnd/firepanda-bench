"""Tests for the comparison against the specification's published answers.

This decides whether a query goes in the table at all, so it has to be strict
about arithmetic and forgiving about formatting. Getting that the wrong way round
either publishes a wrong answer or rejects a right one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

pa = pytest.importorskip("pyarrow")

import validate_tpch  # noqa: E402


def test_a_decimal_and_a_float_are_the_same_answer():
    """Engines differ on type. That is not a disagreement about the answer."""
    assert validate_tpch.cell_matches(3.75, "3.75")
    assert validate_tpch.cell_matches("3.75", "3.75")


def test_last_place_noise_is_tolerated():
    """Six million values summed in a different order land a few ulps away."""
    assert validate_tpch.cell_matches(1234567.8900001, "1234567.89")


def test_a_real_difference_is_not_tolerated():
    """A part in a million is far looser than noise and far tighter than an error."""
    assert not validate_tpch.cell_matches(1234.56, "1234.99")


def test_a_midnight_timestamp_is_that_date():
    """Dataframe engines return dates as timestamps; the answer file has dates."""
    assert validate_tpch.cell_matches("1995-03-15 00:00:00", "1995-03-15")


def test_text_still_has_to_match():
    """The tolerance is for numbers, not for names."""
    assert not validate_tpch.cell_matches("FRANCE", "GERMANY")


def test_a_wrong_row_count_is_reported_before_anything_else():
    """A query returning the wrong number of rows is not a formatting question."""
    table = pa.table({"n": pa.array([1, 2], pa.int64())})
    verdict = validate_tpch.compare(table, "n\n1\n2\n3\n")
    assert "2 rows" in verdict and "3" in verdict


def test_a_matching_answer_returns_nothing():
    """An empty verdict is agreement."""
    table = pa.table({"n": pa.array([1, 2], pa.int64())})
    assert validate_tpch.compare(table, "n\n1\n2\n") == ""


def test_a_column_named_differently_is_reported():
    """Because the fingerprint sums by name, a rename reads as a disagreement."""
    table = pa.table({"total": pa.array([1], pa.int64())})
    assert "columns named" in validate_tpch.compare(table, "n\n1\n")


def test_a_produced_name_may_extend_the_published_one():
    """The Q18 case.

    The specification's select list has no alias on `sum(l_quantity)` and the
    stored answer header truncates it to `sum`, so DuckDB running the
    specification's own text disagrees with the specification's own answer file.
    The select list is the better authority and both spellings are accepted.
    """
    table = pa.table({"sum(l_quantity)": pa.array([1], pa.int64())})
    assert validate_tpch.compare(table, "sum\n1\n") == ""
