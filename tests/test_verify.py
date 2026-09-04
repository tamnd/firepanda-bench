#!/usr/bin/env python3
"""Tests for the exact answer check.

What is being tested here is the harness side of it: which files get paired with
which, which tolerance a query is compared under, what the widening does to a type
an engine chose for itself, and what the report says. The comparison itself is
firepanda-compat's and is tested there, so most of these run against a stand in
that records what it was handed and answers whatever the test needs. That is
deliberate rather than lazy. A test that needed the real comparison layer would
need a checkout of a second repository to run, which would mean it did not run,
and the branching it would then not be covering is exactly the branching that
decides whether a difference is reported as a difference or quietly excused.

One test does use the real thing when a checkout happens to be next door, because
a stand in cannot catch this file calling the comparison layer with an argument
that layer no longer takes.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pyarrow as pa
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import verify


class FakeTolerance(Enum):
    """The tolerance classes the real one has, with the values it gives them."""

    EXACT = None
    SINGLE = 1e-12
    ACCUMULATION = 1e-9
    STATISTICAL = 1e-7


@dataclass(frozen=True)
class FakeRules:
    tolerance: FakeTolerance
    relaxations: frozenset = frozenset()
    reason: str = ""


@dataclass
class FakeVerdict:
    equal: bool
    differences: list = field(default_factory=list)
    extra: int = 0
    relaxations_used: frozenset = frozenset()


class FakeCompare:
    """A stand in for `fpcompat.compare` that answers from a script.

    `answers` maps a tolerance class name to whether the comparison is equal under
    it, which is the whole of what the known difference path needs: a difference
    that fails strictly and passes loosely.
    """

    Tolerance = FakeTolerance
    Rules = FakeRules

    def __init__(self, answers: dict[str, bool] | None = None):
        self.answers = answers if answers is not None else {}
        self.calls: list[FakeRules] = []

    def compare(self, left, right, rules):
        self.calls.append(rules)
        equal = self.answers.get(rules.tolerance.name, True)
        if equal:
            return FakeVerdict(equal=True)
        return FakeVerdict(
            equal=False,
            differences=["column revenue: row 0, 1.0 against 2.0"],
            extra=3,
        )


def write(path: Path, table: pa.Table) -> Path:
    """Writes a table where an answer file would be."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pa.ipc.new_file(path, table.schema) as writer:
        writer.write_table(table)
    return path


def test_integer_width_is_not_a_difference():
    table = pa.table({"n": pa.array([1, 2], pa.int32())})
    assert verify.widen(table).column("n").type == pa.int64()


def test_float_width_is_not_a_difference():
    table = pa.table({"v": pa.array([1.5, 2.5], pa.float32())})
    assert verify.widen(table).column("v").type == pa.float64()


def test_a_decimal_with_a_scale_is_money_and_becomes_a_float():
    money = [Decimal("1.25"), Decimal("2.50")]
    table = pa.table({"price": pa.array(money, pa.decimal128(15, 2))})
    widened = verify.widen(table)
    assert widened.column("price").type == pa.float64()
    assert widened.column("price").to_pylist() == [1.25, 2.5]


def test_a_decimal_with_no_scale_is_an_integer_in_a_wider_coat():
    # This is what DuckDB hands back for SUM over an INTEGER column. Turning it into
    # a float would report a dtype difference against every other engine's int64 on
    # four db-benchmark queries, which is what it used to do.
    table = pa.table({"v1": pa.array([Decimal("300000000")], pa.decimal128(38, 0))})
    widened = verify.widen(table)
    assert widened.column("v1").type == pa.int64()
    assert widened.column("v1").to_pylist() == [300000000]


def test_a_scaleless_decimal_too_large_for_int64_falls_back_rather_than_crashing():
    huge = Decimal("1" + "0" * 25)
    table = pa.table({"v1": pa.array([huge], pa.decimal128(38, 0))})
    widened = verify.widen(table)
    assert widened.column("v1").type == pa.float64()
    assert widened.column("v1").to_pylist() == [1e25]


def test_a_dictionary_encoded_key_is_the_same_key():
    keys = pa.array(["a", "b", "a"]).dictionary_encode()
    table = pa.table({"id1": keys})
    widened = verify.widen(table)
    assert widened.column("id1").type == pa.string()
    assert widened.column("id1").to_pylist() == ["a", "b", "a"]


def test_a_date_becomes_a_timestamp_because_pandas_has_no_date():
    table = pa.table({"o_orderdate": pa.array([19000], pa.date32())})
    widened = verify.widen(table)
    assert widened.column("o_orderdate").type == pa.timestamp("us")


def test_widening_leaves_strings_and_booleans_where_they_are():
    table = pa.table({"id1": ["a"], "flag": [True]})
    widened = verify.widen(table)
    assert widened.column("id1").type == pa.string()
    assert widened.column("flag").type == pa.bool_()


def test_snapping_zeroes_cancellation_residue_in_the_declared_column_only():
    table = pa.table({"r2": [1e-35, 0.5], "v1": [1e-35, 0.5]})
    snapped = verify.snap(table, {"r2": 1e-12})
    assert snapped.column("r2").to_pylist() == [0.0, 0.5]
    assert snapped.column("v1").to_pylist() == [1e-35, 0.5]


def test_snapping_with_no_floors_returns_the_answer_untouched():
    table = pa.table({"r2": [1e-35]})
    assert verify.snap(table, {}).column("r2").to_pylist() == [1e-35]


def test_a_floor_on_a_column_that_is_not_a_float_does_nothing():
    table = pa.table({"r2": ["1e-35"]})
    assert verify.snap(table, {"r2": 1e-12}).column("r2").to_pylist() == ["1e-35"]


def test_the_only_near_zero_entry_is_the_one_that_was_argued_for():
    # Adding to this table is deliberately awkward, because every entry is a place
    # the exact check stops being exact. If a second one appears, the paragraph
    # above it in verify.py has to earn it the way the first one did.
    assert set(verify.NEAR_ZERO) == {("db-benchmark", "q9")}
    assert verify.NEAR_ZERO[("db-benchmark", "q9")] == {"r2": 1e-12}


def test_answers_are_grouped_by_query_and_the_suite_comes_off_the_path(tmp_path):
    write(tmp_path / "tpch" / "sf1" / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    write(tmp_path / "tpch" / "sf1" / "q1" / "polars.arrow", pa.table({"a": [1]}))
    write(tmp_path / "tpch" / "sf1" / "q2" / "pandas.arrow", pa.table({"a": [1]}))
    found = verify.collect(tmp_path)
    assert set(found) == {("tpch", "q1"), ("tpch", "q2")}
    assert set(found[("tpch", "q1")]) == {"pandas", "polars"}


def test_a_caller_that_knows_the_suite_beats_the_path(tmp_path):
    # This is the case run.py is in. It points the check at the size directory,
    # which is already inside the suite, so there is nothing above the query to read
    # the suite name off and q9 would be compared under the strict default it cannot
    # meet.
    write(tmp_path / "q9" / "pandas.arrow", pa.table({"a": [1]}))
    write(tmp_path / "q9" / "polars.arrow", pa.table({"a": [1]}))
    assert set(verify.collect(tmp_path, "db-benchmark")) == {("db-benchmark", "q9")}


def test_a_directory_too_shallow_to_name_a_suite_says_so(tmp_path):
    write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    write(tmp_path / "q1" / "polars.arrow", pa.table({"a": [1]}))
    assert set(verify.collect(tmp_path)) == {("unknown", "q1")}


def test_a_stray_file_with_nothing_above_it_is_skipped(tmp_path):
    write(tmp_path / "loose.arrow", pa.table({"a": [1]}))
    assert verify.collect(tmp_path) == {}


def test_pandas_is_the_reference_when_it_ran():
    assert verify.reference_engine({"duckdb": Path("d"), "pandas": Path("p")}) == "pandas"


def test_something_else_stands_in_when_pandas_did_not_run():
    assert verify.reference_engine({"polars": Path("p"), "duckdb": Path("d")}) == "duckdb"


def test_the_default_tolerance_is_accumulation_and_the_correlation_is_looser(tmp_path):
    compare = FakeCompare()
    engines = {
        "pandas": write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]})),
        "polars": write(tmp_path / "q1" / "polars.arrow", pa.table({"a": [1]})),
    }
    plain = verify.verify_query(compare, "db-benchmark", "q1", engines)
    assert plain["tolerance"] == "ACCUMULATION"
    correlation = verify.verify_query(compare, "db-benchmark", "q9", engines)
    assert correlation["tolerance"] == "STATISTICAL"


def test_the_comparison_is_told_to_sort_and_told_why(tmp_path):
    compare = FakeCompare()
    engines = {
        "pandas": write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]})),
        "polars": write(tmp_path / "q1" / "polars.arrow", pa.table({"a": [1]})),
    }
    verify.verify_query(compare, "tpch", "q1", engines)
    rules = compare.calls[0]
    assert rules.relaxations == frozenset({"grouped_order"})
    assert "sort=False" in rules.reason


def test_the_reference_is_reported_as_equal_to_itself_without_being_compared(tmp_path):
    compare = FakeCompare()
    engines = {"pandas": write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]}))}
    result = verify.verify_query(compare, "tpch", "q1", engines)
    assert result["engines"]["pandas"] == {"equal": True, "differences": []}
    assert compare.calls == []


def test_a_disagreement_is_a_disagreement(tmp_path):
    compare = FakeCompare({"ACCUMULATION": False, "STATISTICAL": False})
    engines = {
        "pandas": write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]})),
        "duckdb": write(tmp_path / "q1" / "duckdb.arrow", pa.table({"a": [2]})),
    }
    result = verify.verify_query(compare, "tpch", "q1", engines)
    assert result["agreed"] is False
    assert result["engines"]["duckdb"]["differences"] == ["column revenue: row 0, 1.0 against 2.0"]


def test_a_registered_difference_is_compared_again_under_the_class_it_claims(tmp_path):
    # tpch against polars is in the registry with STATISTICAL, so a failure at
    # ACCUMULATION is retried there, and passing means it is the difference the
    # registry describes rather than a new one.
    compare = FakeCompare({"ACCUMULATION": False, "STATISTICAL": True})
    engines = {
        "pandas": write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]})),
        "polars": write(tmp_path / "q1" / "polars.arrow", pa.table({"a": [1]})),
    }
    result = verify.verify_query(compare, "tpch", "q1", engines)
    assert result["agreed"] is True
    entry = result["engines"]["polars"]
    assert entry["equal"] is True
    assert entry["tolerance"] == "STATISTICAL"
    assert "decimal product" in entry["known"]
    # The strict differences are kept, so a reader can see what was excused.
    assert entry["differences"]


def test_a_difference_larger_than_the_registered_one_is_still_a_disagreement(tmp_path):
    compare = FakeCompare({"ACCUMULATION": False, "STATISTICAL": False})
    engines = {
        "pandas": write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]})),
        "polars": write(tmp_path / "q1" / "polars.arrow", pa.table({"a": [1]})),
    }
    result = verify.verify_query(compare, "tpch", "q1", engines)
    assert result["agreed"] is False
    assert "known" not in result["engines"]["polars"]


def test_a_registry_entry_only_covers_the_suite_it_names(tmp_path):
    # The same engine, a different suite, and no excuse available.
    compare = FakeCompare({"ACCUMULATION": False, "STATISTICAL": True})
    engines = {
        "pandas": write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]})),
        "polars": write(tmp_path / "q1" / "polars.arrow", pa.table({"a": [1]})),
    }
    result = verify.verify_query(compare, "db-benchmark", "q1", engines)
    assert result["agreed"] is False


def test_an_unreadable_answer_is_a_disagreement_rather_than_a_crash(tmp_path):
    compare = FakeCompare()
    good = write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    bad = tmp_path / "q1" / "duckdb.arrow"
    bad.write_bytes(b"not an arrow file")
    result = verify.verify_query(compare, "tpch", "q1", {"pandas": good, "duckdb": bad})
    assert result["agreed"] is False
    assert "unreadable" in result["engines"]["duckdb"]["differences"][0]


def test_an_unreadable_reference_stops_the_query_there(tmp_path):
    compare = FakeCompare()
    bad = tmp_path / "q1" / "pandas.arrow"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not an arrow file")
    other = write(tmp_path / "q1" / "duckdb.arrow", pa.table({"a": [1]}))
    result = verify.verify_query(compare, "tpch", "q1", {"pandas": bad, "duckdb": other})
    assert result["agreed"] is False
    assert set(result["engines"]) == {"pandas"}


def test_replacing_the_tolerance_keeps_everything_else():
    compare = FakeCompare()
    rules = FakeRules(FakeTolerance.ACCUMULATION, frozenset({"grouped_order"}), "because")
    looser = verify.replace_tolerance(compare, rules, "STATISTICAL")
    assert looser.tolerance is FakeTolerance.STATISTICAL
    assert looser.relaxations == rules.relaxations
    assert looser.reason == rules.reason


def test_a_registry_entry_for_a_suite_that_ran_and_was_not_needed_is_reported(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare())
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    write(tmp_path / "tpch" / "sf1" / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    write(tmp_path / "tpch" / "sf1" / "q1" / "polars.arrow", pa.table({"a": [1]}))
    document = verify.verify(tmp_path, Path("/nowhere"))
    assert document["known_unused"] == ["tpch/polars"]
    assert document["agreed"] is True


def test_a_registry_entry_for_a_suite_that_did_not_run_is_not_called_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare())
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    write(tmp_path / "db-benchmark" / "0.5GB" / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    write(tmp_path / "db-benchmark" / "0.5GB" / "q1" / "polars.arrow", pa.table({"a": [1]}))
    document = verify.verify(tmp_path, Path("/nowhere"))
    assert document["known_unused"] == []


def test_the_verdict_records_which_comparison_layer_said_so(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare())
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    write(tmp_path / "tpch" / "sf1" / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    document = verify.verify(tmp_path, Path("/somewhere"))
    assert document["compat"] == {"path": "/somewhere", "revision": "abc1234"}
    assert document["check"] == "exact"


def test_a_missing_compat_checkout_says_all_three_ways_to_fix_it(tmp_path, monkeypatch):
    monkeypatch.delenv(verify.COMPAT_ENV, raising=False)
    with pytest.raises(SystemExit) as raised:
        verify.compat_root(str(tmp_path))
    message = str(raised.value)
    assert "--compat" in message
    assert verify.COMPAT_ENV in message
    assert "git clone" in message


def test_the_environment_variable_is_used_when_nothing_is_passed(tmp_path, monkeypatch):
    (tmp_path / "fpcompat").mkdir()
    (tmp_path / "fpcompat" / "compare.py").write_text("")
    monkeypatch.setenv(verify.COMPAT_ENV, str(tmp_path))
    assert verify.compat_root(None) == tmp_path.resolve()


def test_an_explicit_path_beats_the_environment_variable(tmp_path, monkeypatch):
    for name in ("given", "environment"):
        (tmp_path / name / "fpcompat").mkdir(parents=True)
        (tmp_path / name / "fpcompat" / "compare.py").write_text("")
    monkeypatch.setenv(verify.COMPAT_ENV, str(tmp_path / "environment"))
    assert verify.compat_root(str(tmp_path / "given")) == (tmp_path / "given").resolve()


def test_the_report_names_the_column_and_the_row_that_differ(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare({"ACCUMULATION": False}))
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    write(tmp_path / "db-benchmark" / "0.5GB" / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    write(tmp_path / "db-benchmark" / "0.5GB" / "q1" / "duckdb.arrow", pa.table({"a": [2]}))
    text = verify.render(verify.verify(tmp_path, Path("/nowhere")))
    assert "DISAGREE" in text
    assert "column revenue: row 0, 1.0 against 2.0" in text
    assert "and 3 more" in text
    assert "1 of 1 queries disagree" in text


def test_the_report_says_so_plainly_when_everything_agrees(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare())
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    for query in ("q1", "q2"):
        write(tmp_path / "db-benchmark" / "0.5GB" / query / "pandas.arrow", pa.table({"a": [1]}))
        write(tmp_path / "db-benchmark" / "0.5GB" / query / "duckdb.arrow", pa.table({"a": [1]}))
    text = verify.render(verify.verify(tmp_path, Path("/nowhere")))
    assert "all 2 queries agree" in text
    assert "DISAGREE" not in text


def test_the_report_prints_a_known_difference_once_and_not_per_query(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify,
        "load_compare",
        lambda root: FakeCompare({"ACCUMULATION": False, "STATISTICAL": True}),
    )
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    for query in ("q1", "q10"):
        write(tmp_path / "tpch" / "sf1" / query / "pandas.arrow", pa.table({"a": [1]}))
        write(tmp_path / "tpch" / "sf1" / query / "polars.arrow", pa.table({"a": [1]}))
    text = verify.render(verify.verify(tmp_path, Path("/nowhere")))
    assert text.count("known difference, tpch against polars") == 1
    assert "under a known difference" in text


def test_a_query_only_one_engine_ran_is_reported_and_not_counted_as_agreement(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare())
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    write(tmp_path / "tpch" / "sf1" / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    text = verify.render(verify.verify(tmp_path, Path("/nowhere")))
    assert "only pandas ran, nothing to compare" in text


def test_nothing_to_verify_says_nothing_to_verify():
    assert "nothing was verified" in verify.render({"queries": {}})


def test_the_exit_code_is_the_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(verify, "compat_root", lambda given: Path("/nowhere"))
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    write(tmp_path / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    write(tmp_path / "q1" / "duckdb.arrow", pa.table({"a": [1]}))

    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare())
    assert verify.main(["--answers", str(tmp_path), "--suite", "tpch"]) == 0

    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare({"ACCUMULATION": False}))
    assert verify.main(["--answers", str(tmp_path), "--suite", "db-benchmark"]) == 1
    capsys.readouterr()


def test_the_json_verdict_is_written_where_it_was_asked_for(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(verify, "compat_root", lambda given: Path("/nowhere"))
    monkeypatch.setattr(verify, "load_compare", lambda root: FakeCompare())
    monkeypatch.setattr(verify, "compat_revision", lambda root: "abc1234")
    write(tmp_path / "answers" / "q1" / "pandas.arrow", pa.table({"a": [1]}))
    out = tmp_path / "deep" / "verdicts.json"
    verify.main(["--answers", str(tmp_path / "answers"), "--json", str(out)])
    document = json.loads(out.read_text())
    assert document["check"] == "exact"
    assert "q1" in document["queries"]
    capsys.readouterr()


def test_a_directory_with_no_answers_in_it_explains_why_there_are_none(tmp_path):
    with pytest.raises(SystemExit) as raised:
        verify.main(["--answers", str(tmp_path / "gone")])
    assert "--verify exact" in str(raised.value)


def test_the_real_comparison_layer_takes_the_arguments_this_file_gives_it(tmp_path):
    # The one test that is not against a stand in. Everything above checks this
    # file's own decisions, and none of it would notice `Rules` growing a required
    # argument or `grouped_order` being renamed, which are the changes most likely
    # to arrive from the other repository. Skipped when there is no checkout, so it
    # covers the machines that have one and does not block the ones that do not.
    root = Path(verify.DEFAULT_COMPAT)
    if not (root / "fpcompat" / "compare.py").is_file():
        pytest.skip("no firepanda-compat checkout next door")
    compare = verify.load_compare(root)

    same = pa.table({"id1": ["a", "b"], "v1": [1.0, 2.0]})
    shuffled = pa.table({"id1": ["b", "a"], "v1": [2.0, 1.0]})
    engines = {
        "pandas": write(tmp_path / "q1" / "pandas.arrow", same),
        "duckdb": write(tmp_path / "q1" / "duckdb.arrow", shuffled),
    }
    result = verify.verify_query(compare, "db-benchmark", "q1", engines)
    assert result["agreed"] is True, result["engines"]

    # And the case the fingerprint cannot see: the same multiset in every column,
    # paired up the wrong way round.
    crossed = pa.table({"id1": ["a", "b"], "v1": [2.0, 1.0]})
    engines["duckdb"] = write(tmp_path / "q1" / "duckdb.arrow", crossed)
    result = verify.verify_query(compare, "db-benchmark", "q1", engines)
    assert result["agreed"] is False
    assert result["engines"]["duckdb"]["differences"]


# What `run.py` does around the check: it shells out, records the verdict in the
# result file, and clears up after itself. These run the real subprocess against a
# real compat checkout when there is one, because the point of them is the wiring.


def answers_tree(root: Path, suite: str, size: str, tables: dict[str, dict[str, pa.Table]]):
    """Lays out answer files the way a run with --verify exact leaves them."""
    base = root / "results" / "answers" / suite / size
    for query, engines in tables.items():
        for engine, table in engines.items():
            write(base / query / f"{engine}.arrow", table)
    return base


def test_a_run_records_the_verdict_and_clears_up_after_itself(tmp_path):
    if not (Path(verify.DEFAULT_COMPAT) / "fpcompat" / "compare.py").is_file():
        pytest.skip("no firepanda-compat checkout next door")
    import run

    same = pa.table({"id1": ["a"], "v1": [1.0]})
    base = answers_tree(
        tmp_path, "db-benchmark", "0.05GB", {"q1": {"pandas": same, "duckdb": same}}
    )

    document = run.run_exact_check(base, "db-benchmark", keep=False)

    assert document["ran"] is True
    assert document["agreed"] is True
    assert "db-benchmark/q1" in document["queries"]
    # An ingestion answer file is the size of the dataset, so the default is to
    # delete them, and the empty directories they were in go too.
    assert not (tmp_path / "results" / "answers").exists()
    assert (tmp_path / "results").is_dir()


def test_a_run_asked_to_keep_the_answers_keeps_them(tmp_path):
    if not (Path(verify.DEFAULT_COMPAT) / "fpcompat" / "compare.py").is_file():
        pytest.skip("no firepanda-compat checkout next door")
    import run

    same = pa.table({"id1": ["a"], "v1": [1.0]})
    base = answers_tree(
        tmp_path, "db-benchmark", "0.05GB", {"q1": {"pandas": same, "duckdb": same}}
    )

    run.run_exact_check(base, "db-benchmark", keep=True)

    assert (base / "q1" / "pandas.arrow").is_file()
    assert (base / "verdicts.json").is_file()


def test_a_check_that_could_not_run_does_not_lose_the_timings(tmp_path, monkeypatch):
    # A missing compat checkout is a fact about the machine, and the run that just
    # finished measuring three engines should not throw the numbers away over it.
    import run

    monkeypatch.setenv(verify.COMPAT_ENV, str(tmp_path / "nowhere"))
    base = answers_tree(
        tmp_path,
        "db-benchmark",
        "0.05GB",
        {"q1": {"pandas": pa.table({"a": [1]}), "duckdb": pa.table({"a": [1]})}},
    )

    document = run.run_exact_check(base, "db-benchmark", keep=True)

    assert document["ran"] is False
    assert document["note"]
    assert document["agreed"] is True
