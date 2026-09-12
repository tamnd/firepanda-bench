"""Tests for the tool that runs the 43 queries by hand and through the planner.

None of these run a query either. What they check is the part that decides
whether the published pair means anything: that a query only counts towards a
total when both routes ran it, that a query the two routes answer differently is
seen rather than timed, and that the statement the planner is handed is the same
file the other three engines are handed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

pytest.importorskip("pyarrow")

import clickbench_planner as tool  # noqa: E402
import validate_clickbench  # noqa: E402


def answer(rows: int, cols: int, sums: dict | None = None, hashes: dict | None = None) -> dict:
    """Builds what the driver's JSON line looks like for one run."""
    return {
        "ok": True,
        "rows_out": rows,
        "cols_out": cols,
        "sums": sums or {},
        "hashes": hashes or {},
        "runs": [{"wall_s": 0.01}, {"wall_s": 0.02}, {"wall_s": 0.05}],
    }


def test_the_planner_is_handed_the_vendored_statements():
    # Not a copy compiled into the driver. The point of the comparison is that
    # both routes answer the same 43 queries, and the file the other three
    # engines read is the only thing that says what those are.
    assert tool.STATEMENTS == ROOT / "suites" / "clickbench" / "queries.sql"
    assert len(validate_clickbench.statements()) == 43


def test_the_reported_time_is_the_median_and_not_the_mean():
    # Three runs of 10, 20 and 50 ms. The mean is 26.7 and the median is 20, and
    # the tail here is a machine doing something else rather than the query.
    assert tool.seconds(answer(1, 1)) == 0.02


def test_two_routes_that_answer_the_same_thing_agree():
    hand = answer(10, 2, {"count_star()": 43.0, "sum(qty)": 12.5})
    planned = answer(10, 2, {"__expr_0": 12.5, "__expr_1": 43.0})
    assert tool.agree(hand, planned)


def test_the_names_are_not_part_of_the_comparison():
    # The hand written route names a count `count_star()` and the SQL route
    # names it after the expression it was written as. Two names for the one
    # number is not a disagreement.
    assert tool.digest(answer(1, 1, {"a": 5.0})) == tool.digest(answer(1, 1, {"b": 5.0}))


def test_a_different_number_under_the_same_name_disagrees():
    hand = answer(1, 1, {"avg(UserID)": 1.9481948778949345e18})
    planned = answer(1, 1, {"__expr_0": -2657217693603.6587})
    assert not tool.agree(hand, planned)


def test_a_different_height_disagrees():
    assert not tool.agree(answer(10, 2, {"a": 1.0}), answer(9, 2, {"a": 1.0}))


def test_a_text_column_is_compared_by_its_hash():
    hand = answer(3, 1, {}, {"SearchPhrase": 11111})
    planned = answer(3, 1, {}, {"__expr_0": 22222})
    assert not tool.agree(hand, planned)


def test_a_refused_query_is_left_out_of_both_totals(monkeypatch):
    # A total over a different set of queries on each side is not a comparison.
    # q1 here is one the SQL layer does not run yet, so neither its hand written
    # time nor its absence reaches the totals.
    def fake(binary, query, pattern, runs, planner):
        if query == "q1" and planner:
            return {"ok": False, "note": "run: LIKE is not lowered yet"}
        return answer(1, 1, {"a": 2.0})

    monkeypatch.setattr(tool, "run_one", fake)
    monkeypatch.setattr(validate_clickbench, "statements", lambda: ["a", "b", "c"])
    measured = tool.measure(Path("driver"), "ignored", 3)
    assert len(measured) == 3
    both = [r for r in measured if "hand_s" in r and "planner_s" in r]
    assert [r["query"] for r in both] == ["q0", "q2"]
    assert "hand_s" in measured[1]
    assert measured[1]["planner_note"].endswith("not lowered yet")
    assert "agree" not in measured[1]


def test_a_disagreement_is_recorded_against_the_query(monkeypatch):
    def fake(binary, query, pattern, runs, planner):
        if query == "q1" and planner:
            return answer(1, 1, {"a": -3.0})
        return answer(1, 1, {"a": 2.0})

    monkeypatch.setattr(tool, "run_one", fake)
    monkeypatch.setattr(validate_clickbench, "statements", lambda: ["a", "b"])
    measured = tool.measure(Path("driver"), "ignored", 3)
    assert measured[0]["agree"] is True
    assert measured[1]["agree"] is False


def test_a_driver_that_exited_reports_the_line_it_ended_on(monkeypatch):
    # The driver writes its refusal to stderr and exits non zero, and the last
    # line of that is the sentence the SQL layer gave. A record with no note in
    # it would leave the table saying a query was refused and not why.
    class Finished:
        returncode = 1
        stdout = ""
        stderr = "loaded 1000000 rows\nrun: strlen is not a function this knows\n"

    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: Finished())
    record = tool.run_one(Path("driver"), "q27", "ignored", 3, True)
    assert record["ok"] is False
    assert record["note"] == "run: strlen is not a function this knows"


def test_the_statements_flag_is_only_passed_on_the_planner_route(monkeypatch):
    seen = []

    class Finished:
        returncode = 0
        stderr = ""
        stdout = '{"ok": true, "rows_out": 1, "cols_out": 1, "runs": []}\n'

    def fake(command, **kwargs):
        seen.append(command)
        return Finished()

    monkeypatch.setattr(tool.subprocess, "run", fake)
    tool.run_one(Path("driver"), "q0", "part-*.parquet", 3, False)
    tool.run_one(Path("driver"), "q0", "part-*.parquet", 3, True)
    assert not any(a.startswith("--statements") for a in seen[0])
    assert f"--statements={tool.STATEMENTS}" in seen[1]
