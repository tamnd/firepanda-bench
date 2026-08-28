"""Tests for the checks that stand between a result and a publication.

Each of these corresponds to a way a benchmark has actually been wrong here, not
to a way one could be.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_sweep

TRACEBACK = (
    'Traceback (most recent call last): |   File "tools/worker.py", line 214, in run '
    "|     samples, answer = metrics.measure(lambda: runner(context), runs) |       "
    '| _duckdb.ParserException: Parser Error: syntax error at or near "l" |  '
    "| LINE 1: ... FROM left l JOIN right_small r USING (id1) |            ^"
)


def _document(results: dict, agreement: dict | None = None) -> dict:
    """Builds a result document around a set of pairings.

    Args:
        results: The per pairing entries.
        agreement: The agreement block, defaulting to everything agreeing.

    Returns:
        The document.
    """
    queries = sorted({key.split("/")[0] for key in results})
    return {
        "suite": "db-benchmark",
        "engines": {"firepanda": "0.6.3", "duckdb": "1.5.5", "pandas": "3.0.5"},
        "results": results,
        "agreement": agreement if agreement is not None else {q: {"agreed": True} for q in queries},
    }


def test_the_exception_is_pulled_out_of_a_flattened_traceback():
    """The last segment is the caret, not the message.

    DuckDB's exception is `_duckdb.ParserException`, whose leading underscore is
    not a reason to skip it.
    """
    assert check_sweep.exception_line(TRACEBACK).startswith("_duckdb.ParserException")


def test_a_crashing_engine_is_flagged():
    """The regression.

    Every join in db-benchmark failed on DuckDB because the tables are called
    `left` and `right_small` and LEFT is a reserved word. The report said so on
    five rows and nobody read them.
    """
    document = _document(
        {
            "j1/duckdb": {"ok": False, "note": TRACEBACK},
            "j1/pandas": {"ok": True, "median_s": 1.0, "peak_rss_bytes": 100},
        }
    )
    concerns = check_sweep.review(document)
    assert any("duckdb raised on 1" in c for c in concerns)


def test_a_stated_reason_is_not_a_crash():
    """ "firepanda has no Parquet reader" is a fact about the engine, not a bug."""
    document = _document(
        {
            "q1/firepanda": {"ok": False, "note": "groups by a string column"},
            "q1/pandas": {"ok": True, "median_s": 1.0, "peak_rss_bytes": 100},
        }
    )
    assert not any("raised" in c for c in check_sweep.review(document))


def test_an_implausible_speedup_is_flagged():
    """Two hundred times faster is a query that read less data."""
    document = _document(
        {
            "q1/firepanda": {"ok": True, "median_s": 0.01, "peak_rss_bytes": 100},
            "q1/pandas": {"ok": True, "median_s": 5.0, "peak_rss_bytes": 100},
        }
    )
    assert any("past what a layout" in c for c in check_sweep.review(document))


def test_a_disagreed_query_is_not_scored_as_a_win():
    """It is not a fast answer, it is a different one."""
    document = _document(
        {
            "q1/firepanda": {"ok": True, "median_s": 0.01, "peak_rss_bytes": 100},
            "q1/pandas": {"ok": True, "median_s": 5.0, "peak_rss_bytes": 100},
        },
        agreement={"q1": {"agreed": False}},
    )
    assert not any("past what a layout" in c for c in check_sweep.review(document))


BINDER_TRACEBACK = (
    'Traceback (most recent call last): |   File "tools/worker.py", line 210, in run '
    "|     context = module.load(paths, suite=suite, io=io) |       "
    "| _duckdb.BinderException: Binder Error: Unexpected prepared parameter. "
    "This type of statement can't be prepared! |            ^"
)


def test_the_crash_gate_ignores_everything_that_is_a_judgement_call():
    """CI fails on crashes and only annotates the rest.

    Winning every query might be true and low coverage is stated in the report.
    An engine that raised is a hole in the table every time, which is why it is
    the one thing the pipeline is allowed to stop on.
    """
    results = {}
    for index in range(1, 4):
        results[f"q{index}/firepanda"] = {"ok": True, "median_s": 0.01, "peak_rss_bytes": 100}
        results[f"q{index}/pandas"] = {"ok": True, "median_s": 5.0, "peak_rss_bytes": 100}
    document = _document(results)
    assert check_sweep.review(document)
    assert check_sweep.crashes(document) == []


def test_the_crash_gate_catches_a_broken_scan_mode():
    """The second regression of exactly this shape.

    DuckDB will not prepare a DDL statement carrying a parameter, so every
    Parquet view failed to create and scan mode had no DuckDB column at all. In
    the report that read as an engine that skipped the suite.
    """
    results = {f"q{i}/duckdb": {"ok": False, "note": BINDER_TRACEBACK} for i in range(1, 16)}
    results["q1/polars"] = {"ok": True, "median_s": 1.0, "peak_rss_bytes": 100}
    found = check_sweep.crashes(_document(results))
    assert len(found) == 1
    assert "duckdb raised on 15" in found[0]
    assert "BinderException" in found[0]


def test_low_coverage_is_flagged():
    """Eight of fifteen queries is a result about eight queries."""
    results = {}
    for index in range(1, 4):
        results[f"q{index}/firepanda"] = {
            "ok": True,
            "median_s": 1.0,
            "peak_rss_bytes": 100,
        }
        results[f"q{index}/pandas"] = {"ok": True, "median_s": 1.0, "peak_rss_bytes": 100}
    assert any("ran 3 of 15" in c for c in check_sweep.review(_document(results)))
