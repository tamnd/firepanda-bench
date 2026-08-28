"""Tests for the rule that decides whether two engines computed the same answer.

Agreement used to be hash equality, and the hash rounds to nine significant
figures before it hashes. That is exactly wrong for the one case this harness
cannot avoid: pandas has to carry TPC-H money in float64, because Arrow refuses
the precision the exact arithmetic needs, so its totals land a few parts in a
billion from DuckDB's. On a run where all three engines had just reproduced the
specification's published answers, four queries were reported as disagreements
purely because the rounding happened to straddle a boundary.

So the comparison is on the numbers, with a tolerance, and the tests here are
mostly about that tolerance being loose enough for the noise and tight enough for
the mistakes it exists to catch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

# Before the import below, because `run` reaches `engines`, which imports pyarrow
# at module scope, and these tests are meant to skip rather than error where it is
# not installed.
pytest.importorskip("pyarrow")

import metrics
import run


def answer(engine, rows, sums, hashes=None):
    """Builds a measurement carrying just the answer, for comparing.

    Args:
        engine: The engine name.
        rows: The row count of the answer.
        sums: The per column sums.
        hashes: The per column text digests, if any.

    Returns:
        A successful `Measurement`.
    """
    return metrics.Measurement(
        engine=engine,
        query="q1",
        ok=True,
        rows_out=rows,
        sums=dict(sums),
        hashes=dict(hashes or {}),
    )


def test_float_noise_is_not_a_disagreement():
    """The regression. A part in a billion is two engines adding in a different order."""
    exact = 104_949_708.90
    drifted = exact * (1 + 3e-10)
    assert run.answers_match(
        answer("duckdb", 1, {"revenue": exact}),
        answer("pandas", 1, {"revenue": drifted}),
    )


def test_a_real_difference_is_a_disagreement():
    """A part in a thousand is not rounding, it is a different set of rows."""
    assert not run.answers_match(
        answer("a", 1, {"revenue": 104_949_708.90}),
        answer("b", 1, {"revenue": 104_844_759.19}),
    )


def test_a_different_row_count_is_a_disagreement():
    """An engine that dropped a group is not in agreement about the answer."""
    assert not run.answers_match(answer("a", 4, {"v": 10.0}), answer("b", 5, {"v": 10.0}))


def test_a_different_set_of_columns_is_a_disagreement():
    """Same numbers under different names is a different answer, and Q18 proved it."""
    assert not run.answers_match(answer("a", 1, {"sum_qty": 10.0}), answer("b", 1, {"qty": 10.0}))


def test_a_zero_total_does_not_divide_by_itself():
    """The tolerance is relative, and a column that sums to zero still has to compare."""
    assert run.answers_match(answer("a", 1, {"v": 0.0}), answer("b", 1, {"v": 0.0}))
    assert not run.answers_match(answer("a", 1, {"v": 0.0}), answer("b", 1, {"v": 1.0}))


def test_text_is_compared_exactly():
    """Two engines that grouped by different string keys have to disagree.

    The numbers here are identical, which is the whole point: before the text
    digest existed this pairing was recorded as agreement.
    """
    assert not run.answers_match(
        answer("a", 2, {"v1": 7.0}, {"id1": 111}),
        answer("b", 2, {"v1": 7.0}, {"id1": 222}),
    )


def test_an_engine_that_sends_no_text_digest_abstains():
    """The firepanda driver cannot return a string column yet.

    Treating a missing digest as a mismatch would report it as computing a
    different answer when what it did was not answer that part at all.
    """
    assert run.answers_match(
        answer("firepanda", 2, {"v1": 7.0}),
        answer("polars", 2, {"v1": 7.0}, {"id1": 111}),
    )


def test_agreement_ignores_the_pairings_that_failed():
    """A skipped query is not a disagreement, it is an absence."""
    ran = answer("polars", 1, {"v": 1.0})
    skipped = metrics.Measurement(engine="firepanda", query="q1", ok=False, note="no reader")
    report = run.agreement([ran, skipped])
    assert report["q1"]["agreed"]
    assert list(report["q1"]["by_engine"]) == ["polars"]


def test_the_result_file_name_carries_the_machine():
    """Two machines running the same thing on the same day must not collide.

    server3 and the gaming PC both run every suite, and before the host was in
    the name the second file copied into `results/` replaced the first with no
    warning from anything.
    """
    assert run.machine_slug({"host": "GamingPC"}) == "gamingpc"
    assert run.machine_slug({"host": "vmi3391933.contaboserver.net"}) == (
        "vmi3391933-contaboserver-net"
    )
    assert run.machine_slug({}) == "unknown-host"


def test_agreement_reports_the_query_that_disagreed():
    """Three engines, one of them wrong, and the query is named."""
    report = run.agreement(
        [
            answer("duckdb", 1, {"v": 1.0}),
            answer("polars", 1, {"v": 1.0}),
            answer("pandas", 1, {"v": 2.0}),
        ]
    )
    assert not report["q1"]["agreed"]
