"""The three ClickBench traps that are enforced by code rather than by a note.

`suites/clickbench/README.md` writes down five ways a port of this suite is
quietly wrong. Two of them are decisions a port makes once and lives with, and the
other three are things a future change could undo without anything going red,
which is what these tests are for: the text columns arriving as text, the empty
string staying an empty string, and the twelve queries whose answers the published
statements do not determine being compared on what the statements do determine
rather than on what they do not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

pytest.importorskip("pyarrow")

import clickbench
import metrics
import pyarrow as pa
import queries as query_registry
import run
import verify


def test_a_text_column_that_arrived_as_bytes_stops_the_load():
    """The first trap, and the reason the check exists at all."""
    kinds = dict.fromkeys(clickbench.TEXT_COLUMNS, True)
    kinds["URL"] = False
    kinds["SearchPhrase"] = False
    with pytest.raises(SystemExit, match="2 of the text columns"):
        clickbench.check_text(kinds, "pandas")


def test_the_check_names_the_engine_whose_load_is_wrong():
    """Three engines call this and the message has to say which one asked."""
    kinds = dict.fromkeys(clickbench.TEXT_COLUMNS, True)
    kinds["Title"] = False
    with pytest.raises(SystemExit, match="polars loaded"):
        clickbench.check_text(kinds, "polars")


def test_the_check_judges_the_columns_that_are_there():
    """A frame with a subset of the columns is checked on the subset.

    The eight row fixture the port tests run on carries 25 of the 105 columns, and
    a check that demanded all 28 text columns would fail on it and then be turned
    off, which is the usual way a check like this stops working.
    """
    clickbench.check_text({"URL": True, "Title": True, "CounterID": False}, "duckdb")


def test_the_check_does_not_pass_an_empty_frame_off_as_a_conversion():
    """Nothing to check is not the same as nothing wrong, and both are quiet here.

    Worth stating: this function cannot tell a loader that converted everything
    from one that loaded nothing. It is not meant to. The loaders it guards have
    already read the file by the time they call it, and the number of columns is
    checked by the dataset manifest.
    """
    clickbench.check_text({}, "duckdb")


def test_null_counts_come_out_of_the_footer(tmp_path):
    """The third trap. The manifest records this so a reader change is visible."""
    table = pa.table(
        {
            "SearchPhrase": pa.array(["", "x", None], pa.string()),
            "CounterID": pa.array([1, 2, 3], pa.int32()),
        }
    )
    path = tmp_path / "hits_0.parquet"
    pytest.importorskip("pyarrow.parquet").write_table(table, path)
    counts = clickbench.null_counts(path)
    assert counts["SearchPhrase"] == 1
    assert counts["CounterID"] == 0


def test_an_empty_string_is_not_counted_as_a_null(tmp_path):
    """The trap the test above is really about.

    This dataset has no nulls and uses the empty string for a missing value. A
    reader that converted one to the other would change what eleven `<> ''`
    filters mean, and this is the count that would move.
    """
    table = pa.table({"URL": pa.array(["", "", ""], pa.string())})
    path = tmp_path / "hits_0.parquet"
    pytest.importorskip("pyarrow.parquet").write_table(table, path)
    assert clickbench.null_counts(path)["URL"] == 0


def test_the_undetermined_queries_are_real_queries():
    """A flag on a name no suite has is a flag that never fires."""
    names = {query.name for query in query_registry.for_suite("clickbench")}
    assert set(query_registry.CLICKBENCH_UNDETERMINED) <= names
    assert len(query_registry.CLICKBENCH_UNDETERMINED) == 12


def test_the_flag_reaches_the_query_objects():
    """The registry is what everything else reads, so the flag has to be on it."""
    flagged = {q.name for q in query_registry.for_suite("clickbench") if q.undetermined}
    assert flagged == set(query_registry.CLICKBENCH_UNDETERMINED)


def test_no_other_suite_has_an_undetermined_query():
    """Every other query in the repository has one right answer and is checked for it."""
    for suite in ("db-benchmark", "tpch", "ingestion"):
        assert not [q.name for q in query_registry.for_suite(suite) if q.undetermined]


def test_the_lookup_is_keyed_by_suite():
    """q17 means something different in each suite and only one of them is undetermined."""
    assert query_registry.undetermined("clickbench", "q17")
    assert query_registry.undetermined("db-benchmark", "q17") == ""
    assert query_registry.undetermined("nonsense", "q17") == ""


def answer(engine, query, rows, sums, hashes=None):
    """Builds a measurement carrying just the answer.

    Args:
        engine: The engine name.
        query: The query name.
        rows: The row count of the answer.
        sums: The per column sums.
        hashes: The per column text digests, if any.

    Returns:
        A successful `Measurement`.
    """
    return metrics.Measurement(
        engine=engine,
        query=query,
        ok=True,
        rows_out=rows,
        sums=dict(sums),
        hashes=dict(hashes or {}),
    )


def test_different_values_on_an_undetermined_query_are_not_a_disagreement():
    """Two engines took a different ten out of a tie and both are right."""
    report = run.agreement(
        [
            answer("duckdb", "q31", 10, {"c": 10.0}, {"WatchID": 1}),
            answer("polars", "q31", 10, {"c": 10.0}, {"WatchID": 2}),
        ],
        "clickbench",
    )
    assert report["q31"]["agreed"]
    assert "nearly unique" in report["q31"]["undetermined"]


def test_the_same_difference_on_a_determined_query_is_a_disagreement():
    """The weaker comparison reaches exactly the queries it was asked to reach."""
    report = run.agreement(
        [
            answer("duckdb", "q0", 1, {"c": 10.0}, {"URL": 1}),
            answer("polars", "q0", 1, {"c": 10.0}, {"URL": 2}),
        ],
        "clickbench",
    )
    assert not report["q0"]["agreed"]


def test_an_undetermined_query_still_has_to_return_the_right_shape():
    """The row count and the column set are determined, so they are still checked."""
    short = run.agreement(
        [
            answer("duckdb", "q31", 10, {"c": 1.0}),
            answer("polars", "q31", 5, {"c": 1.0}),
        ],
        "clickbench",
    )
    assert not short["q31"]["agreed"]
    wide = run.agreement(
        [
            answer("duckdb", "q31", 10, {"c": 1.0}, {"WatchID": 1}),
            answer("polars", "q31", 10, {"c": 1.0}, {}),
        ],
        "clickbench",
    )
    assert not wide["q31"]["agreed"]


def test_the_run_says_which_queries_were_compared_weakly():
    """A weaker check that is not announced is a weaker check nobody knows about."""
    report = run.agreement(
        [answer("duckdb", "q17", 10, {"c": 1.0}), answer("pandas", "q17", 10, {"c": 1.0})],
        "clickbench",
    )
    assert "no ORDER BY" in report["q17"]["undetermined"]
    assert "undetermined" not in report.get("q0", {})


def write(path: Path, table) -> Path:
    """Writes a table where an answer file would be.

    Args:
        path: Where to write it.
        table: The answer.

    Returns:
        The path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with pa.ipc.new_file(path, table.schema) as writer:
        writer.write_table(table)
    return path


def test_the_exact_check_compares_an_undetermined_query_on_its_shape(tmp_path):
    """Different rows, same shape, and the verdict says why it did not look closer.

    `compare` is None on purpose. An undetermined query must not reach the
    comparison layer at all, so handing it something that would raise if it did is
    the assertion.
    """
    engines = {
        "pandas": write(tmp_path / "q31" / "pandas.arrow", pa.table({"WatchID": [1, 2]})),
        "duckdb": write(tmp_path / "q31" / "duckdb.arrow", pa.table({"WatchID": [3, 4]})),
    }
    result = verify.verify_query(None, "clickbench", "q31", engines)
    assert result["agreed"] is True
    assert result["rows"] == 2
    assert "nearly unique" in result["undetermined"]


def test_a_wrong_shape_on_an_undetermined_query_still_fails(tmp_path):
    """Row count, column names and column types are all still compared."""
    engines = {
        "pandas": write(tmp_path / "q31" / "pandas.arrow", pa.table({"WatchID": [1, 2]})),
        "duckdb": write(tmp_path / "q31" / "duckdb.arrow", pa.table({"WatchID": [3]})),
    }
    short = verify.verify_query(None, "clickbench", "q31", engines)
    assert short["agreed"] is False
    assert short["engines"]["duckdb"]["differences"] == ["1 rows against 2"]

    engines = {
        "pandas": write(tmp_path / "q31" / "pandas.arrow", pa.table({"WatchID": [1, 2]})),
        "polars": write(tmp_path / "q31" / "polars.arrow", pa.table({"UserID": [1, 2]})),
    }
    renamed = verify.verify_query(None, "clickbench", "q31", engines)
    assert renamed["agreed"] is False
    assert "columns" in renamed["engines"]["polars"]["differences"][0]


def test_the_verify_report_names_the_queries_it_did_not_look_inside():
    """The output has to distinguish a weaker pass from an ordinary one."""
    document = {
        "compat": {"revision": "abc123"},
        "known_unused": [],
        "queries": {
            "clickbench/q31": {
                "reference": "pandas",
                "rows": 10,
                "agreed": True,
                "undetermined": "69,354 groups tie at the row the limit cuts on",
                "engines": {
                    "pandas": {"equal": True, "differences": []},
                    "duckdb": {"equal": True, "differences": []},
                },
            }
        },
    }
    text = verify.render(document)
    assert "values not compared" in text
    assert "compared on shape alone" in text
    assert "clickbench/q31" in text
