"""The pandas ClickBench port, checked against DuckDB running the published SQL.

The interesting tests here run both engines over the same small Parquet file and
require them to produce the same answer for all 43 queries. That is the check the
whole suite exists to make, and doing it in CI needs a table small enough to build
in a test and shaped like the real one.

The table itself is in `clickbench_fixture.py`, shared with the Polars port's
tests, along with the patterns those tests use to take a published statement's
last clause back off. Everything here is about pandas.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest
from clickbench_fixture import NAMES, NARROWING, ORDERING, TAIL, hits_file, record

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import engines

# The port is imported the same way the engines below are, rather than with an
# import statement, because it imports pandas at the top of its own file. A plain
# import would turn a machine without pandas into a collection error for the whole
# module instead of a skip. CI installs pandas, so this skips on a contributor's
# machine and never in the checks.
pd = pytest.importorskip("pandas")
pandas_clickbench = pytest.importorskip("engines.pandas_clickbench")


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
