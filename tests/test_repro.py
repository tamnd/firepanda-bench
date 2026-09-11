"""Tests for what counts as a reproduction.

The timing half of this is a tolerance and nothing to test. The answer half is
not: a rerun that returns a different ten rows out of a tie has reproduced, and a
rerun that returns nine rows has not, and the difference between those two is the
whole reason twelve ClickBench queries carry a reason string in the registry.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import repro


def _pair(suite: str, query: str, was: dict, now: dict) -> tuple[dict, dict]:
    """Builds an original and a rerun document differing only in one pairing.

    Args:
        suite: Which suite the query belongs to.
        query: The query name.
        was: What the published file recorded for it.
        now: What the rerun produced.

    Returns:
        The two documents.
    """
    key = f"{query}/pandas"
    base = {"ok": True, "median_s": 1.0, "rows_out": 10, "checksum": "aaaa"}
    return (
        {"suite": suite, "results": {key: base | was}},
        {"suite": suite, "results": {key: base | now}},
    )


def test_a_changed_answer_is_not_a_timing_difference():
    """The ordinary case, on a query whose statement determines its answer."""
    original, fresh = _pair("clickbench", "q0", {}, {"checksum": "bbbb"})
    assert repro.compare(original, fresh) == 1


def test_a_different_ten_rows_out_of_a_tie_has_reproduced():
    """q32 ties on every row, so the answer is any ten rows of the table and the
    engine is free to return different ones on a second run. Comparing the digest
    there reports a dozen changed answers on a reproduction that reproduced."""
    original, fresh = _pair("clickbench", "q32", {}, {"checksum": "bbbb"})
    assert repro.compare(original, fresh) == 0


def test_the_row_count_of_an_undetermined_query_is_still_checked():
    """Which ten rows come back is the engine's choice. How many is not."""
    original, fresh = _pair("clickbench", "q32", {}, {"checksum": "bbbb", "rows_out": 9})
    assert repro.compare(original, fresh) == 1


def test_a_pairing_that_did_not_run_either_time_is_not_a_drift():
    """An engine that cannot run a query is a row in the report and not a failed
    reproduction."""
    original, fresh = _pair("clickbench", "q0", {"ok": False}, {"ok": False})
    assert repro.compare(original, fresh) == 0
