"""Tests for the cross engine fingerprint.

This is the piece the whole comparison rests on. If it says two engines agreed
when they did not, the report ranks two different queries against each other; if
it says they disagreed when they did, real results get thrown away. It has been
wrong in the first direction once already, by dropping decimal columns, so the
cases below are mostly regressions rather than hypotheticals.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

# Before the import below, because `engines` imports pyarrow at module scope and
# these tests are meant to skip rather than error where it is not installed.
pa = pytest.importorskip("pyarrow")

import engines  # noqa: E402


def test_agrees_across_int_and_float_spellings():
    """An engine answering in int64 agrees with one answering in float64."""
    left = pa.table({"n": pa.array([1, 2, 3], pa.int64())})
    right = pa.table({"n": pa.array([1.0, 2.0, 3.0], pa.float64())})
    assert engines.fingerprint(3, engines.column_sums(left)) == engines.fingerprint(
        3, engines.column_sums(right)
    )


def test_decimal_counts_as_numeric():
    """The regression. TPC-H money is DECIMAL(15,2) and it has to be summed.

    Before this, `is_floating` and `is_integer` were both false for a decimal, so
    every money column in the suite was dropped from the fingerprint.
    """
    table = pa.table(
        {"revenue": pa.array([Decimal("1.50"), Decimal("2.25")], pa.decimal128(15, 2))}
    )
    sums = engines.column_sums(table)
    assert sums == {"revenue": pytest.approx(3.75)}


def test_two_one_column_decimal_answers_do_not_collide():
    """The reason the regression was dangerous rather than merely wrong.

    q6 and q19 both return one row of one decimal revenue column. With decimals
    dropped they fingerprinted identically, so an engine returning q6's answer for
    q19 would have been recorded as agreeing.
    """
    q6 = pa.table({"revenue": pa.array([Decimal("123456.78")], pa.decimal128(15, 2))})
    q19 = pa.table({"revenue": pa.array([Decimal("987654.32")], pa.decimal128(15, 2))})
    assert engines.fingerprint(1, engines.column_sums(q6)) != engines.fingerprint(
        1, engines.column_sums(q19)
    )


def test_a_numberless_answer_is_hashed_rather_than_refused():
    """TPC-H q20 answers with two strings and no numbers, and that is legal.

    The guard used to fire on it and every engine failed the query identically, so
    the suite reported twenty one of twenty two queries and called it a run. The
    guard is still there, it just covers text now.
    """
    table = pa.table({"name": pa.array(["a", "b"])})
    rows, cols, checksum, sums, hashes = engines.digest(table)
    assert (rows, cols) == (2, 1)
    assert sums == {}
    assert set(hashes) == {"name"}
    assert checksum


def test_an_answer_with_nothing_to_fingerprint_is_refused():
    """A fingerprint over no content is a row count wearing a hash."""
    table = pa.table({"when": pa.array([None, None], pa.null())})
    with pytest.raises(ValueError):
        engines.digest(table)


def test_an_empty_answer_is_allowed():
    """Zero columns is a legitimate answer and must not raise."""
    assert engines.column_sums(pa.table({})) == {}
    assert engines.digest(pa.table({}))[3] == {}


def test_text_columns_are_part_of_the_fingerprint():
    """db-benchmark q1 groups by a string key, and the key has to be checked.

    Two engines that grouped by different keys and happened to land on the same
    number of groups and the same total were recorded as agreeing before this,
    because the only string column in the answer was invisible to the digest.
    """
    mine = pa.table({"id1": pa.array(["id001", "id002"]), "v1": pa.array([3.0, 4.0])})
    theirs = pa.table({"id1": pa.array(["id003", "id004"]), "v1": pa.array([3.0, 4.0])})
    assert engines.column_sums(mine) == engines.column_sums(theirs)
    assert engines.column_hashes(mine) != engines.column_hashes(theirs)


def test_text_digest_ignores_row_order():
    """Two engines are free to return the same groups in a different order."""
    ascending = pa.table({"k": pa.array(["a", "b", "c"])})
    descending = pa.table({"k": pa.array(["c", "b", "a"])})
    assert engines.column_hashes(ascending) == engines.column_hashes(descending)


def test_text_digest_is_the_published_fnv1a():
    """Pinned, because the Mojo driver has to compute the same number."""
    assert engines.fnv1a("a") == 0xAF63DC4C8601EC8C
    assert engines.fnv1a("foobar") == 0x85944171F73967E8


def test_a_null_string_is_not_an_empty_string():
    """An engine that dropped a null and one that kept it have to disagree."""
    kept = pa.table({"k": pa.array(["a", None])})
    emptied = pa.table({"k": pa.array(["a", ""])})
    assert engines.column_hashes(kept) != engines.column_hashes(emptied)


def test_booleans_and_dates_are_summed():
    """Neither is a number to Arrow and both are an answer a query can return."""
    table = pa.table(
        {
            "flag": pa.array([True, True, False]),
            "day": pa.array([1, 2], pa.date32()).take([0, 1, 1]),
        }
    )
    sums = engines.column_sums(table)
    assert sums["flag"] == pytest.approx(2.0)
    assert sums["day"] == pytest.approx((1 + 2 + 2) * 86_400 * 1_000_000)


def test_a_date_agrees_with_the_same_day_as_a_timestamp():
    """One engine's date32 and another's timestamp are the same answer."""
    day = pa.table({"d": pa.array([1, 2], pa.date32())})
    stamp = pa.table({"d": pa.array([1, 2], pa.date32()).cast(pa.timestamp("ms"))})
    assert engines.column_sums(day) == engines.column_sums(stamp)


def test_column_names_are_part_of_the_fingerprint():
    """Two engines with the same numbers under different names disagree.

    This is what caught Q18, where two ports had aliased the column `sum_qty` and
    DuckDB, running the specification's own text, called it `sum(l_quantity)`.
    """
    a = engines.fingerprint(1, {"sum_qty": 10.0})
    b = engines.fingerprint(1, {"sum(l_quantity)": 10.0})
    assert a != b


def test_row_count_is_part_of_the_fingerprint():
    """Same sums over a different number of rows is a different answer."""
    assert engines.fingerprint(1, {"v": 6.0}) != engines.fingerprint(3, {"v": 6.0})


def test_agreement_survives_float_noise_below_nine_figures():
    """Six million values summed in a different order must still agree."""
    exact = 1234567.89
    drifted = exact * (1 + 1e-12)
    assert engines.fingerprint(1, {"v": exact}) == engines.fingerprint(1, {"v": drifted})


def test_disagreement_shows_up_above_nine_figures():
    """A real difference is not rounded away."""
    assert engines.fingerprint(1, {"v": 1234567.89}) != engines.fingerprint(1, {"v": 1234567.90})
