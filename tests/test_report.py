"""Tests for the arithmetic and the exclusions in the report.

The report is where a number becomes a claim, so the two things worth pinning are
that the summary is a geometric mean and that a query the engines disagreed on
never reaches a table.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import report


def test_geometric_mean_is_not_dominated_by_one_win():
    """One hundred times win and nine ties is not an eleven times engine.

    The arithmetic mean of those ten ratios is 10.9. The geometric mean is 1.58,
    and the second number is the one that describes the engine.
    """
    ratios = [100.0] + [1.0] * 9
    assert report.geometric_mean(ratios) == pytest.approx(1.5849, rel=1e-3)


def test_geometric_mean_of_nothing_is_zero():
    """No comparable queries is not a score of one."""
    assert report.geometric_mean([]) == 0.0


def test_geometric_mean_ignores_non_positive_ratios():
    """A zero median is a measurement failure, not a ratio."""
    assert report.geometric_mean([4.0, 0.0, 1.0]) == pytest.approx(2.0)


def _document(agreed: bool) -> dict:
    """Builds a minimal result document with one query and two engines.

    Args:
        agreed: Whether the engines are recorded as having agreed.

    Returns:
        The document.
    """
    return {
        "suite": "db-benchmark",
        "size": "0.5GB",
        "io": "memory",
        "runs": 5,
        "engines": {"pandas": "3.0.5", "polars": "1.44.1"},
        "machine": {},
        "results": {
            "q1/pandas": {"ok": True, "median_s": 2.0, "peak_rss_bytes": 200},
            "q1/polars": {"ok": True, "median_s": 1.0, "peak_rss_bytes": 100},
        },
        "agreement": {"q1": {"agreed": agreed, "by_engine": {}}},
    }


def test_a_disagreed_query_is_not_scored():
    """A different answer is not a faster answer."""
    scores = report.ratios(_document(False), ["pandas", "polars"], "pandas", "polars")
    assert scores["speed"] == {}
    assert scores["speed_geomean"] == 0.0


def test_an_agreed_query_is_scored():
    """The same document with agreement produces the ratio."""
    scores = report.ratios(_document(True), ["pandas", "polars"], "pandas", "polars")
    assert scores["speed"]["q1"] == pytest.approx(2.0)
    assert scores["memory"]["q1"] == pytest.approx(2.0)


def test_a_disagreed_query_is_kept_out_of_the_table():
    """It goes in a section of its own rather than in the timings."""
    text = report.render(_document(False), Path("x.json"))
    assert "| q1 |" not in text
    assert "did not agree" in text


def test_a_pairing_that_did_not_run_is_named_with_its_reason():
    """Dropping it would turn a partial implementation into a clean sweep."""
    document = _document(True)
    document["engines"]["firepanda"] = "0.6.3"
    document["results"]["q1/firepanda"] = {
        "ok": False,
        "note": "groups by a string column",
    }
    text = report.render(document, Path("x.json"))
    assert "groups by a string column" in text
