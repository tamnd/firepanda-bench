#!/usr/bin/env python3
"""Tests for the query to operation declarations and the link into the cost matrix.

These do not check that a declaration is complete, because nothing can: a query calls
what it calls and only a person reading the implementation can say whether the list
matches. What they check is that every query has one, that every name in one is a name
the compat matrix could in principle carry, and that the two sides of the link do not
drift apart silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import operations
import queries as query_registry


def test_every_query_declares_what_it_is_made_of():
    for suite, entries in query_registry.SUITES.items():
        for query in entries:
            names = operations.declared(suite, query.name)
            assert names, f"{suite}/{query.name} declares nothing"


def test_no_declaration_names_a_query_that_does_not_exist():
    known = {
        (suite, query.name) for suite, entries in query_registry.SUITES.items() for query in entries
    }
    for key in operations.DECLARED:
        assert key in known, f"{key} is declared and is not a query"


def test_the_vendored_matrix_is_the_shape_compat_publishes():
    table = operations.matrix()
    assert table["count"] == len(table["operations"])
    assert 0 < table["chained"] < table["count"]
    for name, entry in table["operations"].items():
        assert entry["covers"], f"{name} covers nothing"
        assert isinstance(entry["chained"], bool)


def test_the_vendored_matrix_carries_no_timings():
    # It is a copy of a file compat commits, and that file has no numbers in it on
    # purpose: a timing belongs to a machine and a committed file belongs to a commit.
    # Asserted by shape rather than by looking for words, because "median" is the
    # name of an operation as well as the name of a statistic.
    table = operations.matrix()
    assert set(table) == {"generator", "count", "chained", "operations"}
    for entry in table["operations"].values():
        assert set(entry) == {"section", "covers", "chained", "needs"}


def test_a_declared_operation_is_a_pandas_name_and_not_a_sentence():
    for names in operations.DECLARED.values():
        for name in names:
            assert " " not in name, f"{name!r} is prose, not a pandas name"
            assert "." in name, f"{name!r} has no namespace, so no matrix row can match it"


def test_a_query_that_groups_links_to_the_group_by_rows():
    rows = operations.rows_for(operations.declared("db-benchmark", "q1"))
    assert any(row.startswith("groupby.") for row in rows)


def test_a_query_that_joins_links_to_the_merge_rows():
    rows = operations.rows_for(operations.declared("db-benchmark", "j1"))
    assert any("merge" in row for row in rows)


def test_an_operation_with_no_matrix_row_is_reported_and_not_dropped():
    # Reading a CSV is the one declared operation with no row, and it is one on
    # purpose, so it is also the case that keeps the gap machinery honest.
    gaps = operations.uncovered(operations.declared("ingestion", "csv_narrow"))
    assert gaps == ["pandas.read_csv"]


def test_an_operation_the_matrix_measures_is_not_reported_as_a_gap():
    gaps = operations.uncovered(operations.declared("db-benchmark", "q1"))
    assert gaps == []


def test_the_reductions_that_keep_their_values_per_group_have_rows_now():
    # q6 is a median and a standard deviation per group, and the matrix measured
    # neither until this link found them. Losing those rows again would be a
    # regression in the thing the link exists to catch.
    gaps = operations.uncovered(operations.declared("db-benchmark", "q6"))
    assert gaps == []


def test_every_operation_with_no_row_is_either_a_known_hole_or_excluded_on_purpose():
    # This used to say the only uncovered name was pandas.read_csv. ClickBench added
    # five real ones, and the point of pinning the list is unchanged: a name arriving
    # here belongs either in the matrix, in the exclusion table with a reason, or in
    # this list with somebody having looked at it.
    #
    # `str.extract` was in this list and came off it when the declarations were read
    # off the pandas port rather than off the published SQL. q28 replaces the whole
    # string with its capture group rather than extracting one, and the name that
    # covers what it does is `str.replace`, which has a row.
    assert operations.coverage()["missing"] == [
        "DataFrame.__len__",
        "DataFrame.iloc",
        "Series.min",
        "Series.nunique",
        "Series.where",
        "dt.minute",
        "pandas.read_csv",
    ]


def test_the_holes_and_the_deliberate_exclusions_are_not_described_the_same_way():
    # A name the matrix has not reached yet and a name it will never carry are
    # different statements, and running them together makes the second look like an
    # excuse for the first.
    text = operations.report()
    assert "pandas.read_csv is a deliberate exclusion rather than a hole." in text
    assert "DataFrame.__len__ is a deliberate exclusion rather than a hole." in text
    assert "should grow next" in text
    for name in ("Series.nunique", "dt.minute", "DataFrame.iloc"):
        assert name in text.split("is a deliberate exclusion")[0]


def test_a_deliberate_exclusion_carries_the_reason_it_is_one():
    for name, reason in operations.EXCLUDED_ON_PURPOSE.items():
        assert name in operations.coverage()["missing"], f"{name} is excluded and is not missing"
        assert len(reason) > 80, f"{name} is excluded without a reason worth reading"


def test_the_coverage_split_accounts_for_every_declared_operation():
    every = {name for names in operations.DECLARED.values() for name in names}
    split = operations.coverage()
    assert set(split["covered"]) | set(split["missing"]) == every
    assert not set(split["covered"]) & set(split["missing"])


def test_the_report_names_the_operations_the_matrix_does_not_measure():
    text = operations.report()
    for name in operations.coverage()["missing"]:
        assert name in text


def test_a_missing_vendored_matrix_does_not_crash_the_report(monkeypatch, tmp_path):
    # A checkout that has not vendored the file still has to render, because a report
    # that refuses to print because a link target is absent is worse than one that
    # prints without the links.
    monkeypatch.setattr(operations, "MATRIX", tmp_path / "nothing.json")
    assert operations.rows_for(("DataFrame.groupby",)) == []
    assert operations.uncovered(("DataFrame.groupby",)) == ["DataFrame.groupby"]


def test_the_command_prints_one_query(capsys):
    assert operations.main(["--query", "tpch/q9"]) == 0
    printed = capsys.readouterr().out
    assert "str.contains" in printed
    assert "matrix row" in printed


def test_the_command_prints_every_suite(capsys):
    assert operations.main([]) == 0
    printed = capsys.readouterr().out
    for suite in query_registry.SUITES:
        assert f"## {suite}" in printed


def test_an_unknown_query_is_an_error_and_not_an_empty_answer():
    with pytest.raises(SystemExit):
        operations.main(["--query", "tpch/q99"])


def test_the_vendored_copy_is_valid_json():
    json.loads(operations.MATRIX.read_text())


def test_a_clickbench_declaration_matches_what_the_pandas_port_calls():
    # The first version of this table was read off the published SQL because the port
    # did not exist. These three are where the two readings disagreed, so they are the
    # ones worth pinning: q1 sums a boolean mask instead of filtering and counting,
    # q28 replaces with a backreference instead of extracting, and q34 is q33 because
    # its constant column goes on the ten rows that survive the limit.
    assert operations.declared("clickbench", "q1") == ("Series.sum",)
    assert "str.replace" in operations.declared("clickbench", "q28")
    assert "str.extract" not in operations.declared("clickbench", "q28")
    assert operations.declared("clickbench", "q34") == operations.declared("clickbench", "q33")


def test_the_limit_is_a_slice_everywhere_except_the_one_query_with_no_ordering():
    # Every other limit in the suite goes through one helper that takes an offset, so
    # it slices whether or not the query has one. q17 is the only query with a limit
    # and no order by, it goes through a different helper, and that helper takes a
    # head. Declaring head on all of them would describe a port nobody wrote.
    for query in query_registry.for_suite("clickbench"):
        names = operations.declared("clickbench", query.name)
        if query.name == "q17":
            assert "DataFrame.head" in names
        else:
            assert "DataFrame.head" not in names


def test_an_operation_measured_by_its_neighbour_is_in_neither_list():
    # Covered, so not a hole. Not measured, so not the same claim as the rest of the
    # covered set. A table that folded these into either side would be saying
    # something it does not know.
    split = operations.coverage()
    for name in operations.MEASURED_NEARBY:
        assert name in split["covered"]
        assert name not in split["missing"]


def test_the_neighbour_rows_say_what_they_measure_instead(capsys):
    assert operations.main([]) == 0
    printed = capsys.readouterr().out
    assert "Rows that measure the neighbour of what a query runs" in printed
    for name, reason in operations.MEASURED_NEARBY.items():
        assert f"{name}: {reason}" in printed
        assert len(reason) > 80, f"{name} is called a neighbour without saying of what"


def test_only_a_query_that_runs_one_reports_a_neighbour():
    assert operations.nearby(operations.declared("clickbench", "q27")) == ["str.len"]
    assert operations.nearby(operations.declared("clickbench", "q28")) == [
        "str.replace",
        "str.len",
    ]
    assert operations.nearby(operations.declared("db-benchmark", "q1")) == []
