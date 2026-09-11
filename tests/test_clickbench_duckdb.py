#!/usr/bin/env python3
"""Tests for DuckDB on ClickBench, which is mostly a test that we did not retype it.

The value of this engine on this suite comes from one property: the statements it
runs are the published ones and nobody here chose them. That property is invisible
at a glance, since a transcribed query and a vendored query look identical in a
diff, so it is asserted rather than trusted. The digest pins the file and a second
test pins that every statement handed to DuckDB is a line of that file.

The rest cover the setup, which is where this suite is easy to get quietly wrong.
The file stores days, seconds and bytes where the published schema says dates,
timestamps and text, and an engine that reads the file as it stands answers six of
the queries with an error and nine of them with the wrong types. The error is
self-reporting. The wrong types are not, because the cross engine digest hashes
bytes and text through the same function and cannot tell them apart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import clickbench  # noqa: E402
import queries as query_registry  # noqa: E402
import worker  # noqa: E402

import engines  # noqa: E402

# The vendored file, reached without going through the engine. The two tests that
# matter most here are about a file on disk rather than about DuckDB, and asking
# for the engine module to get at them would make them skip on a machine with no
# DuckDB installed, which is the machine most likely to have got the copy wrong.
VENDORED = Path(__file__).resolve().parent.parent / "suites" / "clickbench" / "queries.sql"

# What ClickHouse/ClickBench commits at `duckdb/queries.sql`, as of b5b34de. The
# file has not changed since November 2022. This is here so that an edit to the
# vendored copy fails loudly instead of quietly changing what we publish a number
# for, which is the entire reason the copy exists rather than a transcription.
PUBLISHED_DIGEST = "274ffe1c4f83baad2fc177bbb6773bbdab0db779faeb2970de8cc531292a5dc6"


def engine():
    """Imports the DuckDB engine, or skips the test that asked for it.

    Per test rather than for the module, because most of what is checked here is
    the vendored file and the type conversions, neither of which needs DuckDB
    installed to be wrong.

    Returns:
        The engine module.
    """
    return pytest.importorskip("engines.duckdb_engine")


def published_lines() -> list[str]:
    """Reads the vendored file the way the engine reads it.

    Returns:
        The 43 statements, semicolons included.
    """
    return [line.strip() for line in VENDORED.read_text().splitlines() if line.strip()]


def test_the_vendored_file_is_the_published_one():
    import hashlib

    assert hashlib.sha256(VENDORED.read_bytes()).hexdigest() == PUBLISHED_DIGEST


def test_the_file_holds_the_published_forty_three():
    assert len(published_lines()) == 43


def test_the_statements_are_lines_of_that_file_and_not_something_retyped():
    # The whole claim of this engine on this suite. Every statement DuckDB is given
    # is a line of the vendored file with its semicolon removed and nothing else
    # done to it, so there is no room for a transcription to have drifted.
    duckdb_engine = engine()
    assert duckdb_engine.CLICKBENCH_SQL_PATH == VENDORED
    for index, line in enumerate(published_lines()):
        assert duckdb_engine.CLICKBENCH_SQL[f"q{index}"] + ";" == line


def test_the_semicolon_comes_off_because_the_statement_gets_wrapped():
    # `run_sql` puts the text inside a `CREATE OR REPLACE TABLE ans AS`, and a
    # semicolon in the middle of that is a syntax error rather than a stray
    # character. This is why the file cannot be used byte for byte.
    duckdb_engine = engine()
    for sql in duckdb_engine.CLICKBENCH_SQL.values():
        assert not sql.endswith(";")


def test_every_registered_query_has_a_statement_and_nothing_extra_does():
    duckdb_engine = engine()
    registered = {query.name for query in query_registry.CLICKBENCH}
    assert set(duckdb_engine.CLICKBENCH_QUERIES) == registered


def test_a_stale_vendored_file_is_refused_rather_than_run_short(tmp_path, monkeypatch):
    # A copy that lost a line would otherwise renumber every query after the gap,
    # and the run would look fine: 42 queries, all green, each one measuring the
    # query after the one its name says.
    duckdb_engine = engine()
    short = tmp_path / "queries.sql"
    short.write_text("SELECT COUNT(*) FROM hits;\nSELECT 1;\n")
    monkeypatch.setattr(duckdb_engine, "CLICKBENCH_SQL_PATH", short)
    with pytest.raises(SystemExit) as caught:
        duckdb_engine.clickbench_sql()
    assert "43" in str(caught.value)


def test_the_suite_gets_its_own_callables_rather_than_db_benchmarks():
    duckdb_engine = engine()
    assert engines.query_map(duckdb_engine, "clickbench") is duckdb_engine.CLICKBENCH_QUERIES


def test_a_suite_nobody_registered_is_an_error_and_not_a_db_benchmark_run():
    # This used to fall through to `QUERIES`, so a new suite whose wiring was
    # forgotten would have been handed the db-benchmark callables and reported an
    # engine that ran ten completely different queries under ClickBench's names.
    duckdb_engine = engine()
    with pytest.raises(SystemExit):
        engines.query_map(duckdb_engine, "a-suite-that-does-not-exist")


def test_the_scan_projection_converts_all_four_integer_columns():
    duckdb_engine = engine()
    projection = duckdb_engine.CLICKBENCH_PROJECTION
    assert "make_date(EventDate)" in projection
    for column in clickbench.TIMESTAMP_COLUMNS:
        assert f"epoch_ms({column} * 1000)" in projection


def hits_like() -> pa.Table:
    """Builds a few rows shaped like the real table's awkward columns.

    Not the whole 105. What matters is one column of each kind that needs
    converting and one that does not, which is enough to pin the conversion
    without a hundred and twenty megabyte download.

    Returns:
        A table in the types the Parquet file actually uses.
    """
    return pa.table(
        {
            "URL": pa.array([b"http://google.com/x", b""], pa.binary()),
            "EventDate": pa.array([15901, 15902], pa.uint16()),
            "EventTime": pa.array([1373846400, 1373846461], pa.int64()),
            "ClientEventTime": pa.array([1373846400, 1373846461], pa.int64()),
            "LocalEventTime": pa.array([1373846400, 1373846461], pa.int64()),
            "CounterID": pa.array([62, 62], pa.int32()),
        }
    )


def test_the_text_columns_arrive_as_text_and_not_as_bytes():
    # The failure this prevents does not raise and does not change a row count. It
    # changes `'http://google.com/x'` into `b'http://google.com/x'`, which the
    # digest hashes identically, so nothing downstream would have caught it.
    converted = clickbench.retype(hits_like())
    assert pa.types.is_string(converted.schema.field("URL").type)
    assert converted.column("URL").to_pylist() == ["http://google.com/x", ""]


def test_the_day_count_becomes_a_date_and_the_second_counts_become_timestamps():
    converted = clickbench.retype(hits_like())
    assert pa.types.is_date(converted.schema.field(clickbench.DATE_COLUMN).type)
    for column in clickbench.TIMESTAMP_COLUMNS:
        kind = converted.schema.field(column).type
        assert pa.types.is_timestamp(kind)
        # Microseconds, because that is what DuckDB's TIMESTAMP is. An engine
        # answering in seconds would be reported as disagreeing about the answer.
        assert kind.unit == "us"


def test_the_conversion_reads_the_dates_the_way_clickbench_does():
    # 15901 days after the epoch is 2013-07-15, which is inside the window the
    # last seven queries filter on. A conversion that was off by the epoch would
    # make those queries answer with nothing and still look like they ran.
    import datetime

    converted = clickbench.retype(hits_like())
    assert converted.column("EventDate").to_pylist()[0] == datetime.date(2013, 7, 15)
    assert converted.column("EventTime").to_pylist()[0] == datetime.datetime(2013, 7, 15, 0, 0)


def test_a_column_that_needs_nothing_done_to_it_is_left_alone():
    before = hits_like()
    after = clickbench.retype(before)
    assert after.schema.field("CounterID").type == before.schema.field("CounterID").type
    assert after.num_rows == before.num_rows
    assert after.column_names == before.column_names


def test_the_partitions_are_ordered_by_number_and_not_by_name(tmp_path):
    # `sorted` puts hits_10 before hits_2. No query in the suite has an answer that
    # depends on row order, so the wrong order would never show up as a wrong
    # answer. It would show up as two engines disagreeing on a query where both of
    # them are right.
    for index in (0, 1, 2, 10, 11):
        (tmp_path / f"hits_{index}.parquet").write_bytes(b"")
    found = clickbench.partitions(str(tmp_path / "hits_*.parquet"))
    assert [Path(path).name for path in found] == [
        "hits_0.parquet",
        "hits_1.parquet",
        "hits_2.parquet",
        "hits_10.parquet",
        "hits_11.parquet",
    ]


def test_a_pattern_that_matches_nothing_says_to_download_the_data(tmp_path):
    with pytest.raises(SystemExit) as caught:
        clickbench.partitions(str(tmp_path / "hits_*.parquet"))
    assert "tools/data.py" in str(caught.value)


def test_the_worker_hands_over_a_pattern_because_the_table_is_many_files(tmp_path):
    # Every other manifest here has one file per table and this one has a hundred,
    # so the usual lookup by table name finds nothing at all.
    manifest = {
        "suite": "clickbench",
        "files": {f"hits_{i}": {"path": f"hits_{i}.parquet"} for i in range(3)},
    }
    paths = worker.table_paths(manifest, tmp_path, ("hits",))
    assert paths == {"hits": str(tmp_path / "hits_*.parquet")}


def test_the_other_suites_still_get_one_file_each(tmp_path):
    manifest = {"suite": "db-benchmark", "files": {"groupby": {"parquet": {"bytes": 1}}}}
    paths = worker.table_paths(manifest, tmp_path, ("groupby",))
    assert paths == {"groupby": str(tmp_path / "groupby.parquet")}


def test_a_hash_column_is_summed_rather_than_refused():
    # `WatchID`, `UserID`, `URLHash` and `RefererHash` are full width sixty four
    # bit hashes and eleven of the queries answer with one. Arrow's safe cast to
    # float64 refuses anything past 2^53, which turned q18 and q23 into engine
    # failures. The sum is rounded to nine significant figures before anything
    # compares it, so the precision the cast loses was never being looked at.
    table = pa.table({"WatchID": pa.array([9123146090114127052, 1], pa.int64())})
    sums = engines.column_sums(table)
    assert sums["WatchID"] == pytest.approx(9.123146090114127e18, rel=1e-9)


def test_duckdb_runs_the_published_statements_against_a_real_file(tmp_path):
    # The end to end check, on a handful of rows rather than on a million. It runs
    # the three queries that between them touch every part of the setup: a date
    # comparison, a string pattern and a timestamp truncation. Against the file as
    # it is stored, without the conversions, all three are wrong or refused.
    duckdb_engine = engine()
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "hits_0.parquet"
    pq.write_table(hits_like(), path)

    context = duckdb_engine.load({"hits": str(tmp_path / "hits_*.parquet")}, "clickbench", "scan")
    connection = context["con"]
    assert connection.execute("SELECT typeof(URL) FROM hits LIMIT 1").fetchone()[0] == "VARCHAR"
    assert connection.execute("SELECT COUNT(*) FROM hits WHERE URL LIKE '%google%'").fetchone() == (
        1,
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM hits WHERE EventDate >= '2013-07-01' AND EventDate <= '2013-07-31'"
    ).fetchone() == (2,)
    assert connection.execute(
        "SELECT COUNT(DISTINCT DATE_TRUNC('minute', EventTime)) FROM hits"
    ).fetchone() == (2,)
    assert duckdb.__version__


def test_both_io_modes_hand_the_queries_the_same_types(tmp_path):
    # The two modes get there by different routes, a projection in SQL and a cast
    # in Arrow, and the point of having both is that the query cannot tell which
    # one it got.
    duckdb_engine = engine()
    pytest.importorskip("duckdb")
    pq.write_table(hits_like(), tmp_path / "hits_0.parquet")
    pattern = str(tmp_path / "hits_*.parquet")
    seen = []
    for io in ("memory", "scan"):
        context = duckdb_engine.load({"hits": pattern}, "clickbench", io)
        seen.append(
            context["con"]
            .execute(
                "SELECT typeof(URL), typeof(EventDate), typeof(EventTime), "
                "typeof(ClientEventTime), typeof(LocalEventTime) FROM hits LIMIT 1"
            )
            .fetchone()
        )
    assert seen[0] == seen[1]
    assert seen[0] == ("VARCHAR", "DATE", "TIMESTAMP", "TIMESTAMP", "TIMESTAMP")


def test_the_manifest_the_downloader_writes_is_the_one_the_worker_reads(tmp_path):
    # These two were written in different pull requests against different ideas of
    # what a manifest holds, and nothing else checks that they agree.
    manifest = {
        "suite": "clickbench",
        "size": "1M",
        "files": {
            "hits_0": {"path": str(tmp_path / "hits_0.parquet"), "bytes": 1, "rows": 1},
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    pq.write_table(hits_like(), tmp_path / "hits_0.parquet")
    paths = worker.table_paths(
        json.loads((tmp_path / "manifest.json").read_text()), tmp_path, ("hits",)
    )
    assert clickbench.partitions(paths["hits"]) == [str(tmp_path / "hits_0.parquet")]
