#!/usr/bin/env python3
"""Tests for the fourth implementation and for the validator that runs it.

`tools/clickbench_hand.py` exists because this suite publishes no answers, so the
only thing the other three checks can say is that the ports agree. These tests are
what keeps the fourth implementation honest, and there is an obvious circularity to
watch for: a test that checked the hand implementation against one of the ports
would be checking it against the thing it was written to check.

So the queries are checked against DuckDB running the published statement on the
eight row fixture, which is the closest thing to an authority here, and the parts
that are not a query are checked on their own. The fixture is small enough that
every statement returns everything that survives its filter, so this is a test of
the filter, the grouping, the aggregates and the type conversions rather than of
which ten rows come back.

The other half is the arithmetic about boundaries and ties, which is the thing that
decides whether a query is compared on its values or only on its shape. That is
pure and is tested without a table at all, including the two cases the hand written
list got wrong before it was computed: a key that does separate the boundary rows,
and a statement whose OFFSET cut ties wider than its LIMIT cut.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import clickbench_fixture
import clickbench_hand
import pyarrow as pa
import validate_clickbench

# The eight queries the fixture can answer. q27 and q28 are not here because both
# sit behind a HAVING COUNT(*) > 100000 that eight rows cannot reach through, so
# comparing them against DuckDB on this fixture would compare two empty answers.
# What those two are really about is the byte length, and that has its own test.
COMPARABLE = ["q4", "q8", "q17", "q22", "q23", "q25", "q34", "q39"]


def fixture(tmp_path) -> tuple:
    """Writes the eight row table and loads it into DuckDB the way the engine does.

    Args:
        tmp_path: The directory to write into.

    Returns:
        The hand implementation's view of the file and a DuckDB connection with the
        same file loaded through the engine's own conversions, so both sides see
        dates, timestamps and text rather than days, seconds and bytes.
    """
    duckdb_engine = pytest.importorskip("engines.duckdb_engine")
    pattern = clickbench_fixture.hits_file(tmp_path)
    context = duckdb_engine.load({"hits": pattern}, "clickbench", "scan")
    return clickbench_hand.Hits(tmp_path), context["con"]


def answer(result) -> pa.Table:
    """Reads a DuckDB result as a table.

    Args:
        result: What `execute` handed back.

    Returns:
        The answer. Newer DuckDB hands back a stream here and older DuckDB hands
        back a table, and both are the same answer.
    """
    table = result.arrow()
    return table if isinstance(table, pa.Table) else pa.table(table)


def whole(statement: str) -> str:
    """Takes the paging off a statement so it returns its whole answer.

    The hand implementation carries the answer before the limit, because the
    validator has to work out for itself which rows the statement pins down. This
    is the other side of that: DuckDB is asked for the same whole answer rather
    than for the ten rows it would choose out of a tie.

    Args:
        statement: The published statement.

    Returns:
        The statement with its LIMIT and OFFSET removed.
    """
    return validate_clickbench.LIMIT_ONLY.sub("", statement)


@pytest.mark.parametrize("name", COMPARABLE)
def test_the_hand_implementation_agrees_with_duckdb_on_the_fixture(name, tmp_path):
    pytest.importorskip("duckdb")
    hits, connection = fixture(tmp_path)
    statement = validate_clickbench.statements()[int(name[1:])]
    produced = validate_clickbench.rows_of(answer(connection.execute(whole(statement))))
    hand = clickbench_hand.HAND[name](hits)
    computed = [tuple(validate_clickbench.cell(value) for value in row) for row in hand.rows]
    assert sorted(computed, key=repr) == sorted(produced, key=repr)


def test_the_hand_implementation_produces_the_columns_the_statement_names(tmp_path):
    # Names rather than values. The comparison above is positional on purpose, so
    # a query whose columns were in the wrong order would still pass it if the
    # values happened to be interchangeable, and this is what catches that.
    pytest.importorskip("duckdb")
    hits, connection = fixture(tmp_path)
    for name in COMPARABLE:
        statement = validate_clickbench.statements()[int(name[1:])]
        published = answer(connection.execute(whole(statement))).schema.names
        assert list(clickbench_hand.HAND[name](hits).columns) == list(published), name


def test_a_length_is_counted_in_bytes_and_not_in_characters(tmp_path):
    # The second trap, in the one place the hand implementation has to make the
    # same decision the ports make. The fixture carries Cyrillic in several URLs
    # so the two counts are different numbers, and the assertion that they differ
    # is there so the one above it cannot quietly become a tautology.
    pytest.importorskip("duckdb")
    hits, connection = fixture(tmp_path)
    counted = sum(len(url) for url in hits.bytes_("URL"))
    characters = sum(len(url) for url in hits.text("URL"))
    assert counted != characters
    assert connection.execute("SELECT SUM(STRLEN(URL)) FROM hits").fetchone()[0] == counted


def test_the_having_is_not_dropped_to_make_a_small_table_answer(tmp_path):
    # Eight rows cannot reach through `COUNT(*) > 100000` and the right answer is
    # nothing. A hand implementation that relaxed the threshold so its tests had
    # something to compare would be checking a different query than the one the
    # ports run.
    hits = clickbench_hand.Hits(Path(clickbench_fixture.hits_file(tmp_path)).parent)
    assert clickbench_hand.HAND["q27"](hits).rows == []
    assert clickbench_hand.HAND["q28"](hits).rows == []


def test_a_referer_the_pattern_does_not_match_comes_back_unchanged(tmp_path):
    # The pattern is anchored at both ends, so `мусор` is not an http URL with a
    # path and has to group under itself. An implementation that returned the empty
    # string for a non match gets a different set of groups, and on the real data
    # it gets one enormous wrong group.
    hits = clickbench_hand.Hits(Path(clickbench_fixture.hits_file(tmp_path)).parent)
    referers = [r for r in hits.text("Referer") if r]
    pattern = clickbench_hand.re.compile(r"^https?://(?:www\.)?([^/]+)/.*$")
    keys = {pattern.sub(r"\1", referer) for referer in referers}
    assert "мусор" in keys
    assert "www.example.ru" not in keys
    assert "example.ru" in keys


def hand(rows, key=(), offset=0, limit=None):
    """Builds an answer with the shape a boundary test needs.

    Args:
        rows: The rows, already in the statement's order.
        key: The positions the ORDER BY sorts on.
        offset: The OFFSET.
        limit: The LIMIT.

    Returns:
        The answer.
    """
    columns = tuple(f"c{i}" for i in range(len(rows[0])))
    return clickbench_hand.Hand(columns, list(rows), key=key, offset=offset, limit=limit)


def test_the_answer_is_the_rows_after_the_offset_and_inside_the_limit():
    rows = [(n,) for n in range(10)]
    assert hand(rows, key=(0,), offset=3, limit=2).answer() == [(3,), (4,)]
    assert hand(rows, key=(0,), limit=2).answer() == [(0,), (1,)]
    assert hand(rows, key=(0,), offset=8).answer() == [(8,), (9,)]


def test_a_statement_with_an_offset_cuts_twice():
    rows = [(n,) for n in range(100)]
    assert hand(rows, key=(0,), offset=10, limit=5).boundaries() == [10, 15]
    assert hand(rows, key=(0,), limit=5).boundaries() == [5]


def test_a_cut_past_the_end_of_the_answer_is_not_a_boundary():
    # Five queries page in past row one thousand and at a small size there is
    # nothing there to cut. A tie needs a row on both sides of the cut.
    rows = [(n,) for n in range(3)]
    assert hand(rows, key=(0,), offset=1000, limit=10).boundaries() == []
    assert hand(rows, key=(0,), limit=10).boundaries() == []


def test_a_tie_at_the_offset_counts_as_much_as_a_tie_at_the_limit():
    # q40's shape, and the reason the tie set was recomputed rather than trusted.
    # It ties 13 rows at its offset boundary and 10 at its limit boundary, so a
    # check that looked only at the limit would have found the smaller of the two.
    rows = [(1,)] * 6 + [(2,)] * 4 + [(3,)] * 6
    ties = hand(rows, key=(0,), offset=4, limit=4).ties()
    assert ties == {4: 6, 8: 4}


def test_a_key_that_separates_the_boundary_rows_is_determined():
    # q26's shape, and the one the hand written list got wrong in the other
    # direction. Its ordering key is two columns, the first of them repeats across
    # the cut and the pair does not, so the answer is pinned down and comparing it
    # on its shape alone was a lost check on ten rows.
    rows = [(7, "a"), (7, "b"), (7, "c"), (7, "d")]
    assert hand(rows, key=(0,), limit=2).ties() == {2: 4}
    assert hand(rows, key=(0, 1), limit=2).ties() == {}
    assert hand(rows, key=(0, 1), limit=2).determined()


def test_a_statement_with_no_order_by_ties_with_the_whole_table():
    # q17, and only q17. There is no ordering expression to separate anything, so
    # every group in the table is a candidate for every row of the answer.
    rows = [(n, "x", 1) for n in range(40)]
    assert hand(rows, limit=10).ties() == {10: 40}
    assert not hand(rows, limit=10).determined()


def test_a_row_that_fell_outside_the_limit_is_still_a_row_of_the_answer():
    # What is left when the answer is not determined: an engine may return a
    # different ten out of a tie and may not return a row that is not there at all.
    rows = [(n,) for n in range(40)]
    answer = hand(rows, key=(0,), limit=10)
    assert answer.contains((39,))
    assert not answer.contains((40,))


def test_a_query_with_no_limit_has_nothing_to_tie_at():
    rows = [(1,), (1,), (1,)]
    assert hand(rows, key=(0,)).ties() == {}
    assert hand(rows, key=(0,)).determined()


def test_a_value_is_stored_the_same_way_whichever_side_produced_it():
    # Both sides go through this, which is the point. An engine that hands back a
    # DECIMAL where another hands back a float, or bytes where another hands back
    # text, is not a disagreement about the answer.
    assert validate_clickbench.cell(Decimal("1.25")) == 1.25
    assert validate_clickbench.cell(b"\xd1\x82\xd1\x83\xd1\x80") == "тур"
    assert validate_clickbench.cell(date(2013, 7, 15)) == "2013-07-15"
    assert validate_clickbench.cell(datetime(2013, 7, 15)) == "2013-07-15"
    assert validate_clickbench.cell(datetime(2013, 7, 15, 12, 30)) == "2013-07-15 12:30:00"
    assert validate_clickbench.cell(None) is None
    assert validate_clickbench.cell(7) == 7


def test_two_answers_compare_as_multisets_rather_than_by_position():
    rows = [("b", 2), ("a", 1)]
    assert validate_clickbench.project(rows, ("k", "n"), ["k", "n"]) == [["a", 1], ["b", 2]]
    assert validate_clickbench.project(rows, ("k", "n"), ["n"]) == [[1], [2]]


def test_a_float_is_compared_with_a_tolerance_and_nothing_else_is():
    assert validate_clickbench.same(1.0, 1.0 + 1e-13)
    assert not validate_clickbench.same(1.0, 1.0 + 1e-6)
    assert validate_clickbench.same("a", "a")
    assert not validate_clickbench.same("a", "b")
    assert validate_clickbench.same(None, None)


def test_a_difference_says_where_it_is_rather_than_that_there_is_one():
    assert validate_clickbench.differs([[1]], [[1]]) == ""
    assert validate_clickbench.differs([[1]], [[1], [2]]) == "1 rows against 2"
    message = validate_clickbench.differs([[1, "a"]], [[1, "b"]])
    assert "row 1 column 2" in message


def stored():
    """Reads every expected file.

    Returns:
        The documents, keyed by query name.
    """
    return {name: validate_clickbench.load_expected(name) for name in clickbench_hand.HAND}


def test_there_is_an_expected_file_for_every_query_that_has_a_hand_implementation():
    assert all(document is not None for document in stored().values())


def test_an_expected_file_is_about_the_statement_it_names():
    # The reason this can go wrong quietly: the files are regenerated by a command
    # and reviewed as a diff, so a file that held the right answer to the wrong
    # query would look like a normal answer.
    published = validate_clickbench.statements()
    for name, document in stored().items():
        assert document["query"] == name
        assert document["statement"] == published[int(name[1:])], name


def test_an_expected_file_checks_every_column_when_the_statement_determines_one():
    for name, document in stored().items():
        if not document["determined"]:
            continue
        assert document["checked"] == document["columns"], name
        assert document["ties"] == {}, name
        assert len(document["answer"]) == document["rows_out"], name


# What each undetermined file pins down, which is its ordering key and nothing
# else. Written out rather than derived, because deriving it from the same function
# that wrote the files would assert that the code agrees with itself.
KEY_ONLY = {"q17": [], "q22": ["c"], "q25": ["SearchPhrase"], "q39": ["PageViews"]}


def test_an_undetermined_expected_file_checks_the_ordering_key_and_says_so():
    # The values in the ordering key are determined even when which rows carry them
    # is not, so these files pin that and nothing else, and the note in the file
    # says which it is without anybody having to find the page that explains it.
    undetermined = {n: d for n, d in stored().items() if not d["determined"]}
    assert set(undetermined) == set(KEY_ONLY)
    for name, document in undetermined.items():
        assert document["ties"] or name == "q17", name
        assert document["checked"] == KEY_ONLY[name], name
        assert document["note"], name


def test_the_query_with_no_ordering_at_all_pins_nothing_and_admits_it():
    document = validate_clickbench.load_expected("q17")
    assert document["checked"] == []
    assert document["answer"] == []
    assert "no ORDER BY" in document["note"]


def test_the_note_names_the_columns_a_file_does_pin_down():
    determined = {
        "determined": True,
        "columns": ["a", "b"],
        "rows_out": 4,
        "checked": ["a", "b"],
        "ties": {},
    }
    assert "all 2 columns over 4 rows" in validate_clickbench.note_for(determined)
    partial = {
        "determined": False,
        "columns": ["a", "b"],
        "rows_out": 10,
        "checked": ["b"],
        "ties": {"10": 19},
    }
    note = validate_clickbench.note_for(partial)
    assert "The b column only" in note
    assert "19 rows tie across the cut at 10" in note
