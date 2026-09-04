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


def test_the_only_operation_with_no_row_is_the_one_excluded_on_purpose():
    # Everything else the 37 queries touch is measured. When that stops being true
    # the new name belongs either in the matrix or in a paragraph saying why not.
    assert operations.coverage()["missing"] == ["pandas.read_csv"]


def test_the_deliberate_exclusion_is_not_described_as_a_hole():
    text = operations.report()
    assert "deliberate exclusion rather than a hole" in text
    assert "should grow next" not in text


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
