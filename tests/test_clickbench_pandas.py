"""The pandas ClickBench port, checked against DuckDB running the published SQL.

The interesting tests here run both engines over the same small Parquet file and
require them to produce the same answer for all 43 queries. That is the check the
whole suite exists to make, and doing it in CI needs a table small enough to build
in a test and shaped like the real one.

Small has a specific meaning. The fixture is eight rows, which is fewer than any
`LIMIT` in the suite takes, so every query returns everything that survives its
filter and no query has to choose between rows that tie on the ordering
expression. That matters: thirteen of the 43 do not have a determined answer at a
real size, and against those thirteen a cross engine comparison is measuring which
way each engine broke a tie rather than whether either is right. Eight rows takes
the ties off the table and leaves the part that is actually a contract, which is
the filter, the grouping, the aggregates, the types and the column names.

What it does not cover is the top ten selection itself, and two aggregates hidden
behind a `HAVING COUNT(*) > 100000` that no eight row table can satisfy. Those get
their own tests below, against DuckDB where the answer has to match something.
"""

from __future__ import annotations

import datetime
import inspect
import re
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import engines
from engines import pandas_clickbench

pd = pytest.importorskip("pandas")


def duckdb_engine():
    """Imports the DuckDB engine, or skips.

    Returns:
        The engine module.
    """
    return pytest.importorskip("engines.duckdb_engine")


def pandas_engine():
    """Imports the pandas engine, or skips.

    Returns:
        The engine module.
    """
    return pytest.importorskip("engines.pandas_engine")


# Eight rows in the types the real file has, which is not the types the published
# schema has: the date is a count of days, the timestamps are counts of seconds
# and every text column is bytes with no logical type. Both engines convert on the
# way in and this fixture is written the raw way so that both conversions are
# under test rather than bypassed.
#
# The values are chosen so nothing is trivially empty, and then chosen again so
# that breaking a filter or an aggregate on purpose actually changes an answer.
# Two rows carry the user identifier q19 looks for. Two carry the referer hash q40
# filters on, with one of each of the two traffic source values q40 accepts, so
# dropping one of them is visible. Two carry the URL hash q41 filters on and both
# survive its other three conditions. One URL contains `.google.` so q22's negated
# filter has something to exclude and one referer does not match q28's pattern at
# all. Two rows land in the same group with the refresh flag set on both, so a sum
# over that flag is not the same number as a maximum. Two rows are the same user in
# the same region, so a distinct count is not the same number as a count. Several
# URLs and referers carry Cyrillic, so a byte length is not a character length.
ROWS = 8

FIXTURE = {
    "WatchID": pa.array([9123146090114127052, 2, 3, 9123146090114127052, 5, 6, 7, 8], pa.int64()),
    "Title": [
        "Google поиск",
        "Google",
        "Магазин",
        "Google новости",
        "Google карта",
        "",
        "Магазин",
        "Новости",
    ],
    # Seconds since the epoch, each one inside the day its own EventDate names.
    # Rows three and seven are the two rows q42 keeps that land in the same hour and
    # in different minutes, so truncating to the hour instead of to the minute puts
    # them in one group and the row count of the answer changes.
    "EventTime": pa.array(
        [
            1372636800,
            1373763660,
            1373846430,
            1375236000,
            1375236060,
            1373846490,
            1373846500,
            1373328200,
        ],
        pa.int64(),
    ),
    "ClientEventTime": pa.array([1372636801] * ROWS, pa.int64()),
    "LocalEventTime": pa.array([1372636802] * ROWS, pa.int64()),
    # 15887 is 2013-07-01, 15900 is 2013-07-14, 15901 is 2013-07-15 and 15917 is
    # 2013-07-31, which are the four boundaries the last seven queries filter on.
    "EventDate": pa.array([15887, 15900, 15901, 15917, 15917, 15901, 15901, 15895], pa.uint16()),
    "CounterID": pa.array([62, 62, 62, 62, 62, 7, 62, 62], pa.int32()),
    "ClientIP": pa.array([1000, -2000, 1000, 1000, 4000, 1000, 5000, -2000], pa.int32()),
    "RegionID": pa.array([1, 2, 1, 3, 2, 1, 4, 2], pa.int32()),
    # Three users appear on more than one row, which is what makes a distinct count
    # a different number from a count. They repeat inside a region for q8, inside a
    # phone model for q10 and q11 and inside a search phrase for q13, so all four of
    # those would count the same user twice if they counted rows instead of users.
    "UserID": pa.array(
        [435090932899640449, 100, 200, 435090932899640449, 300, 200, 200, 300], pa.int64()
    ),
    "URL": [
        "http://example.ru/google/страница",
        "http://www.google.com/search",
        "http://shop.ru/тур",
        "",
        "http://x.ru/.google./a",
        "http://example.ru/google/другая",
        "http://shop.ru/тур",
        "http://other.ru/page",
    ],
    "Referer": [
        "http://www.example.ru/a/b",
        "https://go.mail.ru/search?q=x",
        "",
        "http://example.ru/x",
        "мусор",
        "",
        "https://www.other.org/страница",
        "http://example.ru/y",
    ],
    "IsRefresh": pa.array([1, 0, 0, 1, 0, 0, 0, 0], pa.int16()),
    "ResolutionWidth": pa.array([1024, 1280, 1920, 800, 1024, 1366, 1920, 1280], pa.int16()),
    "MobilePhone": pa.array([0, 1, 0, 2, 1, 0, 3, 1], pa.int16()),
    "MobilePhoneModel": ["", "iPhone", "", "Galaxy", "iPhone", "", "Nokia", "iPhone"],
    "TraficSourceID": pa.array([-1, 6, 6, 2, 0, 6, -1, 6], pa.int16()),
    "SearchEngineID": pa.array([0, 3, 0, 0, 0, 0, 0, 2], pa.int16()),
    "SearchPhrase": [
        "тур в турцию",
        "",
        "купить",
        "тур в турцию",
        "карта",
        "тур в турцию",
        "купить",
        "новости",
    ],
    "AdvEngineID": pa.array([0, 2, 0, 0, 3, 0, 0, 1], pa.int16()),
    "WindowClientWidth": pa.array([1000, 1200, 1900, 780, 1000, 1300, 1900, 1200], pa.int16()),
    "WindowClientHeight": pa.array([700, 800, 1000, 500, 700, 900, 1000, 800], pa.int16()),
    "IsLink": pa.array([1, 0, 1, 0, 0, 0, 1, 0], pa.int16()),
    "IsDownload": pa.array([0, 0, 0, 1, 0, 0, 0, 0], pa.int16()),
    "DontCountHits": pa.array([0, 0, 0, 0, 1, 0, 0, 0], pa.int16()),
    "RefererHash": pa.array(
        [10, 11, 12, 13, 14, 15, 3594120000172545465, 3594120000172545465], pa.int64()
    ),
    "URLHash": pa.array(
        [20, 2868770270353813622, 22, 23, 24, 25, 2868770270353813622, 27], pa.int64()
    ),
}


def hits_file(directory: Path) -> str:
    """Writes the fixture as a partition of the hits table.

    Named `hits_0.parquet` and handed back as a glob, because that is the shape
    both engines are given for this suite: the real dataset is a hundred files and
    nothing downstream has a path to a single one.

    Args:
        directory: Where to write it.

    Returns:
        The glob matching the partition.
    """
    columns = {}
    for name, values in FIXTURE.items():
        if isinstance(values, list):
            columns[name] = pa.array([v.encode() for v in values], pa.binary())
        else:
            columns[name] = values
    pq.write_table(pa.table(columns), directory / "hits_0.parquet")
    return str(directory / "hits_*.parquet")


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """Loads the fixture into both engines, through each engine's own loader.

    Args:
        tmp_path_factory: pytest's temporary directory factory.

    Returns:
        The DuckDB context and the pandas context.
    """
    duck = duckdb_engine()
    pandas = pandas_engine()
    pattern = hits_file(tmp_path_factory.mktemp("clickbench"))
    return (
        duck.load({"hits": pattern}, suite="clickbench", io="memory"),
        pandas.load({"hits": pattern}, suite="clickbench", io="memory"),
        pattern,
    )


NAMES = [f"q{index}" for index in range(43)]


@pytest.mark.parametrize("name", NAMES)
def test_pandas_agrees_with_duckdb(name, loaded):
    """Both engines answer every query with the same rows, columns and digest."""
    duck_ctx, pandas_ctx, _ = loaded
    expected = engines.digest(duckdb_engine().CLICKBENCH_QUERIES[name](duck_ctx))
    actual = engines.digest(pandas_engine().CLICKBENCH_QUERIES[name](pandas_ctx))
    assert actual[0] == expected[0], f"{name} row count"
    assert actual[1] == expected[1], f"{name} column count"
    assert actual[3] == pytest.approx(expected[3]), f"{name} numeric columns"
    assert actual[4] == expected[4], f"{name} text columns"


@pytest.mark.parametrize("name", NAMES)
def test_column_names_match_duckdb(name, loaded):
    """The port answers with DuckDB's own column names, in DuckDB's order.

    The digest is keyed by column name, so a port that computed the right answer
    under its own names would be reported as an engine disagreement rather than as
    a naming difference. Order matters too, and a rename would not have caught the
    right columns in the wrong places.
    """
    duck_ctx, pandas_ctx, _ = loaded
    expected = engines.as_arrow(duckdb_engine().CLICKBENCH_QUERIES[name](duck_ctx))
    actual = engines.as_arrow(pandas_engine().CLICKBENCH_QUERIES[name](pandas_ctx))
    assert actual.column_names == expected.column_names


# What the published statements do to an answer after the grouping is finished.
# Five queries page in past row one thousand or row ten thousand and two drop every
# group with fewer than a hundred thousand rows, so on an eight row fixture those
# seven return nothing at all and the test above is only checking their column
# names. Taking the narrowing off both sides puts the filter, the grouping and the
# aggregates back under test.
#
# The SQL is not being rewritten to make the port look better. It is the same
# statement with its last clause removed, on both sides, and the pandas side is
# narrowed by exactly three functions so removing it there is removing the same
# thing.
NARROWING = re.compile(r"\s+HAVING COUNT\(\*\) > \d+|\s+LIMIT \d+(\s+OFFSET \d+)?$")


@pytest.mark.parametrize("name", NAMES)
def test_pandas_agrees_with_duckdb_before_the_limit(name, loaded, monkeypatch):
    """The two engines agree on the whole answer, not just on the part a limit keeps."""
    duck_ctx, pandas_ctx, _ = loaded
    duck = duckdb_engine()
    statement = NARROWING.sub("", duck.CLICKBENCH_SQL[name])
    monkeypatch.setattr(pandas_clickbench, "top", lambda frame, by, *a, **k: frame)
    monkeypatch.setattr(pandas_clickbench, "first", lambda frame, rows: frame)
    monkeypatch.setattr(pandas_clickbench, "having", lambda frame, column, minimum: frame)
    expected = engines.digest(duck.run_sql(duck_ctx, statement))
    actual = engines.digest(pandas_engine().CLICKBENCH_QUERIES[name](pandas_ctx))
    assert actual[0] == expected[0], f"{name} row count"
    assert actual[3] == pytest.approx(expected[3]), f"{name} numeric columns"
    assert actual[4] == expected[4], f"{name} text columns"


def test_the_narrowing_is_taken_off_every_query_that_has_one():
    """The regular expression above really does strip what it claims to strip.

    A pattern that quietly matched nothing would turn the test above into a second
    copy of the one before it, passing for the wrong reason.
    """
    statements = duckdb_engine().CLICKBENCH_SQL
    stripped = [n for n in NAMES if NARROWING.search(statements[n])]
    assert len(stripped) == 32
    for name in NAMES:
        assert "LIMIT" not in NARROWING.sub("", statements[name])
        assert "HAVING" not in NARROWING.sub("", statements[name])


# The other half of the last clause, which the comparison above cannot see. An
# eight row fixture returns everything, so a query that took the wrong number of
# rows, paged from the wrong place or sorted the wrong way round still produces the
# same answer, and `engines.digest` does not depend on row order anyway. These two
# patterns pull the number, the offset and the direction out of the published
# statement and check them against the arguments the port actually passes.
TAIL = re.compile(r"\s+LIMIT (\d+)(?:\s+OFFSET (\d+))?;?$")
ORDERING = re.compile(r"\bORDER BY (.+?)\s+LIMIT")


def record(real, calls):
    """Wraps a helper so a test can see what it was called with.

    Args:
        real: The helper being wrapped.
        calls: The list to append to, one entry per call.

    Returns:
        A stand in that records the call by argument name and then does the real
        work, so the query it is wrapped into still returns a real answer.
    """
    signature = inspect.signature(real)

    def wrapper(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        calls.append((real.__name__, bound.arguments))
        return real(*args, **kwargs)

    return wrapper


@pytest.mark.parametrize("name", NAMES)
def test_the_limit_and_the_direction_match_the_statement(name, loaded, monkeypatch):
    """Every query narrows by the amount its own statement narrows by, in the same order."""
    _, pandas_ctx, _ = loaded
    statement = duckdb_engine().CLICKBENCH_SQL[name]
    calls = []
    monkeypatch.setattr(pandas_clickbench, "top", record(pandas_clickbench.top, calls))
    monkeypatch.setattr(pandas_clickbench, "first", record(pandas_clickbench.first, calls))
    pandas_engine().CLICKBENCH_QUERIES[name](pandas_ctx)
    tail = TAIL.search(statement)
    if tail is None:
        assert calls == [], f"{name} has no limit and should not be taking a top"
        return
    assert len(calls) == 1, f"{name} narrows once"
    helper, arguments = calls[0]
    assert arguments["rows"] == int(tail.group(1)), f"{name} limit"
    if helper == "first":
        assert "ORDER BY" not in statement, f"{name} is ordered and needs a sort"
        return
    assert arguments["offset"] == int(tail.group(2) or 0), f"{name} offset"
    ordered = ORDERING.search(statement).group(1)
    assert arguments["ascending"] == ("DESC" not in ordered), f"{name} direction"


def test_every_query_is_ported():
    """All 43 published queries have a pandas implementation."""
    assert sorted(pandas_clickbench.QUERIES, key=lambda n: int(n[1:])) == NAMES


def test_registry_matches_duckdb():
    """The pandas port answers to the same query names DuckDB does."""
    pandas = pandas_engine()
    assert sorted(pandas.CLICKBENCH_QUERIES) == sorted(duckdb_engine().CLICKBENCH_QUERIES)


def test_query_map_finds_the_clickbench_queries():
    """The suite table hands back the ClickBench callables rather than another suite's."""
    pandas = pandas_engine()
    assert engines.query_map(pandas, "clickbench") is pandas.CLICKBENCH_QUERIES
    assert engines.query_map(pandas, "db-benchmark") is pandas.QUERIES


def test_load_converts_the_dates_and_the_text(loaded):
    """The loaded frame has the published schema's types, not the file's.

    The file stores the date as a count of days, the three timestamps as counts of
    seconds and every text column as bytes. An engine that skipped any of that
    would still run most of the suite and would answer several queries with the
    wrong type, which the digest cannot see because it hashes bytes and text the
    same way.
    """
    _, pandas_ctx, _ = loaded
    hits = pandas_ctx["hits"]
    assert str(hits["EventDate"].dtype) == "date32[day][pyarrow]"
    assert str(hits["EventTime"].dtype) == "timestamp[us][pyarrow]"
    assert str(hits["ClientEventTime"].dtype) == "timestamp[us][pyarrow]"
    assert str(hits["LocalEventTime"].dtype) == "timestamp[us][pyarrow]"
    assert str(hits["URL"].dtype) == "string[pyarrow]"
    assert hits["EventDate"].iloc[2] == datetime.date(2013, 7, 15)


def test_byte_length_matches_duckdb_strlen(loaded):
    """`STRLEN` is bytes and the port counts bytes.

    This is behind a `HAVING COUNT(*) > 100000` in both queries that use it, so no
    fixture small enough for CI can reach it through the query. It is checked here
    directly, against the function DuckDB runs, because getting it wrong moves the
    average by more than two percent on the real data and the two queries would
    still look like they worked.
    """
    duck_ctx, pandas_ctx, _ = loaded
    expected = duck_ctx["con"].execute("SELECT STRLEN(URL) FROM hits").fetchall()
    actual = pandas_clickbench.byte_length(pandas_ctx["hits"]["URL"])
    assert [value for (value,) in expected] == actual.tolist()


def test_byte_length_is_not_character_length(loaded):
    """The trap that makes the test above worth having.

    `Series.str.len` is the obvious thing to reach for and it is a character count.
    On any URL with a non ASCII byte in it the two disagree, and the fixture has
    several.
    """
    _, pandas_ctx, _ = loaded
    urls = pandas_ctx["hits"]["URL"]
    assert pandas_clickbench.byte_length(urls).sum() > urls.str.len().sum()


def test_regex_matches_duckdb(loaded):
    """The capture group in q28 pulls out the same host DuckDB pulls out.

    Also behind a `HAVING`, so it needs its own check. The fixture includes a
    referer that does not match the pattern at all, which both engines have to
    leave alone rather than turn into an empty string.
    """
    duck_ctx, pandas_ctx, _ = loaded
    pattern = pandas_clickbench.REFERER_PATTERN
    expected = (
        duck_ctx["con"]
        .execute(f"SELECT REGEXP_REPLACE(Referer, '{pattern}', '\\1') FROM hits")
        .fetchall()
    )
    actual = pandas_ctx["hits"]["Referer"].str.replace(pattern, r"\1", regex=True)
    assert [value for (value,) in expected] == actual.tolist()
    assert "мусор" in actual.tolist()


def test_top_takes_the_rows_a_limit_and_offset_take():
    """The helper every ordered query ends with takes the right slice.

    The eight row fixture is deliberately smaller than any limit in the suite, so
    nothing above this exercises the selection itself.
    """
    frame = pd.DataFrame({"k": list(range(20)), "c": list(range(20))})
    assert pandas_clickbench.top(frame, "c", 3)["c"].tolist() == [19, 18, 17]
    assert pandas_clickbench.top(frame, "c", 3, offset=5)["c"].tolist() == [14, 13, 12]
    assert pandas_clickbench.top(frame, "c", 3, ascending=True)["c"].tolist() == [0, 1, 2]
    assert pandas_clickbench.top(frame, "c", 3, offset=100).empty


def test_top_is_stable():
    """Ties come back in the same order on every run over the same frame.

    That does not make pandas agree with DuckDB on a query whose answer the SQL
    does not determine. It makes the pandas answer to such a query the same every
    time, which is what a reference has to be before anything can be compared
    against it.
    """
    frame = pd.DataFrame({"k": list(range(20)), "c": [1] * 20})
    first = pandas_clickbench.top(frame, "c", 5)["k"].tolist()
    assert first == pandas_clickbench.top(frame, "c", 5)["k"].tolist()
    assert first == list(range(5))


def test_named_refuses_a_shape_it_was_not_given_names_for():
    """A port that dropped or gained a column fails loudly rather than renaming."""
    frame = pd.DataFrame({"a": [1], "b": [2]})
    with pytest.raises(ValueError, match="2 columns and 3 names"):
        pandas_clickbench.named(frame, ("x", "y", "z"))
