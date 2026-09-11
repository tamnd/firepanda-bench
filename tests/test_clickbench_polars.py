"""The Polars ClickBench port, checked against DuckDB running the published SQL.

Same arrangement as the pandas port's tests. Both engines run over the same eight
row Parquet file and have to produce the same answer for all 43 queries, and the
table itself is in `clickbench_fixture.py` so that the two ports are compared on
exactly the same rows.

Polars is the one engine here with two genuinely different paths into this suite.
In memory mode the table arrives as Arrow and the conversions are already done; in
scan mode nothing has been read yet and the conversions go into the plan, where
Polars pushes a three column projection underneath them. That is two
implementations of the same schema, so there is a test requiring them to answer
all 43 identically.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest
from clickbench_fixture import NAMES, NARROWING, ORDERING, TAIL, hits_file, record

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import engines

# Imported through `importorskip` rather than with an import statement, because the
# port imports polars at the top of its own file and a plain import would turn a
# machine without polars into a collection error for the whole module.
pl = pytest.importorskip("polars")
polars_clickbench = pytest.importorskip("engines.polars_clickbench")


def duckdb_engine():
    """Imports the DuckDB engine, or skips.

    Returns:
        The engine module.
    """
    return pytest.importorskip("engines.duckdb_engine")


def polars_engine():
    """Imports the Polars engine, or skips.

    Returns:
        The engine module.
    """
    return pytest.importorskip("engines.polars_engine")


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """Loads the fixture into DuckDB and into Polars both ways.

    Args:
        tmp_path_factory: pytest's temporary directory factory.

    Returns:
        The DuckDB context, the Polars memory context, the Polars scan context and
        the pattern they were all built from.
    """
    duck = duckdb_engine()
    polars = polars_engine()
    pattern = hits_file(tmp_path_factory.mktemp("clickbench"))
    return (
        duck.load({"hits": pattern}, suite="clickbench", io="memory"),
        polars.load({"hits": pattern}, suite="clickbench", io="memory"),
        polars.load({"hits": pattern}, suite="clickbench", io="scan"),
        pattern,
    )


@pytest.mark.parametrize("name", NAMES)
def test_polars_agrees_with_duckdb(name, loaded):
    """Both engines answer every query with the same rows, columns and digest."""
    duck_ctx, polars_ctx, _, _ = loaded
    expected = engines.digest(duckdb_engine().CLICKBENCH_QUERIES[name](duck_ctx))
    actual = engines.digest(polars_engine().CLICKBENCH_QUERIES[name](polars_ctx))
    assert actual[0] == expected[0], f"{name} row count"
    assert actual[1] == expected[1], f"{name} column count"
    assert actual[3] == pytest.approx(expected[3]), f"{name} numeric columns"
    assert actual[4] == expected[4], f"{name} text columns"


@pytest.mark.parametrize("name", NAMES)
def test_scan_and_memory_answer_the_same(name, loaded):
    """The two io modes are two conversions of one file and must not disagree.

    Memory mode converts in Arrow before Polars sees the table. Scan mode converts
    inside the plan, over what the Parquet reader produced. A query that answered
    differently under the two would mean one of the conversions is wrong, and the
    published number would depend on which mode the run happened to use.
    """
    _, memory_ctx, scan_ctx, _ = loaded
    polars = polars_engine()
    expected = engines.digest(polars.CLICKBENCH_QUERIES[name](memory_ctx))
    actual = engines.digest(polars.CLICKBENCH_QUERIES[name](scan_ctx))
    assert actual[0] == expected[0], f"{name} row count"
    assert actual[1] == expected[1], f"{name} column count"
    assert actual[2] == expected[2], f"{name} digest"


@pytest.mark.parametrize("name", NAMES)
def test_column_names_match_duckdb(name, loaded):
    """The port answers with DuckDB's own column names, in DuckDB's order.

    The digest is keyed by column name, so a port that computed the right answer
    under its own names would be reported as an engine disagreement rather than as
    a naming difference. Order matters too, and a rename would not have caught the
    right columns in the wrong places.
    """
    duck_ctx, polars_ctx, _, _ = loaded
    expected = engines.as_arrow(duckdb_engine().CLICKBENCH_QUERIES[name](duck_ctx))
    actual = engines.as_arrow(polars_engine().CLICKBENCH_QUERIES[name](polars_ctx))
    assert actual.column_names == expected.column_names


@pytest.mark.parametrize("name", NAMES)
def test_polars_agrees_with_duckdb_before_the_limit(name, loaded, monkeypatch):
    """The two engines agree on the whole answer, not just on the part a limit keeps."""
    duck_ctx, polars_ctx, _, _ = loaded
    duck = duckdb_engine()
    statement = NARROWING.sub("", duck.CLICKBENCH_SQL[name])
    monkeypatch.setattr(polars_clickbench, "top", lambda frame, by, *a, **k: frame)
    monkeypatch.setattr(polars_clickbench, "first", lambda frame, rows: frame)
    monkeypatch.setattr(polars_clickbench, "having", lambda frame, column, minimum: frame)
    expected = engines.digest(duck.run_sql(duck_ctx, statement))
    actual = engines.digest(polars_engine().CLICKBENCH_QUERIES[name](polars_ctx))
    assert actual[0] == expected[0], f"{name} row count"
    assert actual[3] == pytest.approx(expected[3]), f"{name} numeric columns"
    assert actual[4] == expected[4], f"{name} text columns"


@pytest.mark.parametrize("name", NAMES)
def test_the_limit_and_the_direction_match_the_statement(name, loaded, monkeypatch):
    """Every query narrows by the amount its own statement narrows by, in the same order."""
    _, polars_ctx, _, _ = loaded
    statement = duckdb_engine().CLICKBENCH_SQL[name]
    calls = []
    monkeypatch.setattr(polars_clickbench, "top", record(polars_clickbench.top, calls))
    monkeypatch.setattr(polars_clickbench, "first", record(polars_clickbench.first, calls))
    polars_engine().CLICKBENCH_QUERIES[name](polars_ctx)
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
    assert arguments["descending"] == ("DESC" in ordered), f"{name} direction"


def test_every_query_is_ported():
    """All 43 published queries have a Polars implementation."""
    assert sorted(polars_clickbench.QUERIES, key=lambda n: int(n[1:])) == NAMES


def test_registry_matches_duckdb():
    """The Polars port answers to the same query names DuckDB does."""
    polars = polars_engine()
    assert sorted(polars.CLICKBENCH_QUERIES) == sorted(duckdb_engine().CLICKBENCH_QUERIES)


def test_query_map_finds_the_clickbench_queries():
    """The suite table hands back the ClickBench callables rather than another suite's."""
    polars = polars_engine()
    assert engines.query_map(polars, "clickbench") is polars.CLICKBENCH_QUERIES
    assert engines.query_map(polars, "db-benchmark") is polars.QUERIES


@pytest.mark.parametrize("mode", ["memory", "scan"])
def test_load_converts_the_dates_and_the_text(mode, loaded):
    """Both io modes produce the published schema's types, not the file's.

    The file stores the date as a count of days, the three timestamps as counts of
    seconds and every text column as bytes. An engine that skipped any of that
    would still run most of the suite and would answer several queries with the
    wrong type, which the digest cannot see because it hashes bytes and text the
    same way.
    """
    _, memory_ctx, scan_ctx, _ = loaded
    hits = (memory_ctx if mode == "memory" else scan_ctx)["hits"]
    schema = hits.collect_schema()
    assert schema["EventDate"] == pl.Date
    assert schema["EventTime"] == pl.Datetime("us")
    assert schema["ClientEventTime"] == pl.Datetime("us")
    assert schema["LocalEventTime"] == pl.Datetime("us")
    assert schema["URL"] == pl.String
    assert hits.select("EventDate").collect().item(2, 0) == datetime.date(2013, 7, 15)


def test_byte_length_matches_duckdb_strlen(loaded):
    """`STRLEN` is bytes and the port counts bytes.

    This is behind a `HAVING COUNT(*) > 100000` in both queries that use it, so no
    fixture small enough for CI can reach it through the query. It is checked here
    directly, against the function DuckDB runs, because getting it wrong moves the
    average by more than two percent on the real data and the two queries would
    still look like they worked.
    """
    duck_ctx, polars_ctx, _, _ = loaded
    expected = duck_ctx["con"].execute("SELECT STRLEN(URL) FROM hits").fetchall()
    actual = polars_ctx["hits"].select(pl.col("URL").str.len_bytes()).collect()
    assert [value for (value,) in expected] == actual.to_series().to_list()


def test_byte_length_is_not_character_length(loaded):
    """The trap that makes the test above worth having.

    `str.len_chars` is the neighbouring method and it is a character count. On any
    URL with a non ASCII byte in it the two disagree, and the fixture has several.
    """
    _, polars_ctx, _, _ = loaded
    lengths = polars_ctx["hits"].select(
        pl.col("URL").str.len_bytes().sum().alias("bytes"),
        pl.col("URL").str.len_chars().sum().alias("chars"),
    )
    row = lengths.collect().row(0)
    assert row[0] > row[1]


def test_the_port_counts_distinct_exactly_and_measures_text_in_bytes():
    """Neither of the two faster wrong answers is anywhere in the port.

    `approx_n_unique` and `str.len_chars` are both the neighbouring method of one
    the port uses, both are faster, and both answer a different question than the
    published SQL asks. Neither shows up as a failure on an eight row fixture,
    since the approximation is exact at that size and the fixture would have to be
    read to notice the character count, so this reads the source. It looks for
    calls rather than for the names, because the module docstring names both of
    them in the course of saying why they are not used.
    """
    source = Path(polars_clickbench.__file__).read_text()
    assert "approx_n_unique(" not in source
    assert "len_chars(" not in source
    assert source.count("n_unique()") == 8
    assert source.count("len_bytes()") == 2


def test_regex_matches_duckdb(loaded):
    """The capture group in q28 pulls out the same host DuckDB pulls out.

    Also behind a `HAVING`, so it needs its own check. DuckDB spells the group
    reference `\\1` and the regex crate spells it `$1`, which is the sort of
    difference that produces a column of literal backslash ones rather than an
    error. The fixture includes a referer that does not match the pattern at all,
    which both engines have to leave alone rather than turn into an empty string.
    """
    duck_ctx, polars_ctx, _, _ = loaded
    pattern = polars_clickbench.REFERER_PATTERN
    expected = (
        duck_ctx["con"]
        .execute(f"SELECT REGEXP_REPLACE(Referer, '{pattern}', '\\1') FROM hits")
        .fetchall()
    )
    actual = (
        polars_ctx["hits"]
        .select(pl.col("Referer").str.replace(pattern, polars_clickbench.REFERER_REPLACEMENT))
        .collect()
        .to_series()
        .to_list()
    )
    assert [value for (value,) in expected] == actual
    assert "мусор" in actual


def test_top_takes_the_rows_a_limit_and_offset_take():
    """The helper every ordered query ends with takes the right slice.

    The eight row fixture is deliberately smaller than any limit in the suite, so
    nothing above this exercises the selection itself.
    """
    frame = pl.DataFrame({"k": list(range(20)), "c": list(range(20))}).lazy()
    assert polars_clickbench.top(frame, "c", 3).collect()["c"].to_list() == [19, 18, 17]
    assert polars_clickbench.top(frame, "c", 3, offset=5).collect()["c"].to_list() == [14, 13, 12]
    ascending = polars_clickbench.top(frame, "c", 3, descending=False)
    assert ascending.collect()["c"].to_list() == [0, 1, 2]
    assert polars_clickbench.top(frame, "c", 3, offset=100).collect().is_empty()


def test_top_is_stable():
    """Ties come back in the same order on every run over the same frame.

    That does not make Polars agree with DuckDB on a query whose answer the SQL
    does not determine, and it does not make the grouped queries deterministic
    either, since the group by above them is left unordered. It settles the four
    that sort raw rows.
    """
    frame = pl.DataFrame({"k": list(range(20)), "c": [1] * 20}).lazy()
    first = polars_clickbench.top(frame, "c", 5).collect()["k"].to_list()
    assert first == polars_clickbench.top(frame, "c", 5).collect()["k"].to_list()
    assert first == list(range(5))
