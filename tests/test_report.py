"""Tests for the arithmetic and the exclusions in the report.

The report is where a number becomes a claim, so the two things worth pinning are
that the summary is a geometric mean and that a query the engines disagreed on
never reaches a table.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import queries
import report


def test_geometric_mean_is_not_dominated_by_one_win():
    """One hundred times win and nine ties is not an eleven times engine.

    The arithmetic mean of those ten ratios is 10.9. The geometric mean is 1.58,
    and the second number is the one that describes the engine.
    """
    ratios = [100.0] + [1.0] * 9
    assert report.geometric_mean(ratios) == pytest.approx(1.5849, rel=1e-3)


def test_geometric_mean_of_nothing_is_zero():
    """No comparable queries is not a score of one."""
    assert report.geometric_mean([]) == 0.0


def test_geometric_mean_ignores_non_positive_ratios():
    """A zero median is a measurement failure, not a ratio."""
    assert report.geometric_mean([4.0, 0.0, 1.0]) == pytest.approx(2.0)


def _document(agreed: bool) -> dict:
    """Builds a minimal result document with one query and two engines.

    Args:
        agreed: Whether the engines are recorded as having agreed.

    Returns:
        The document.
    """
    return {
        "suite": "db-benchmark",
        "size": "0.5GB",
        "io": "memory",
        "runs": 5,
        "engines": {"pandas": "3.0.5", "polars": "1.44.1"},
        "machine": {},
        "results": {
            "q1/pandas": {"ok": True, "median_s": 2.0, "peak_rss_bytes": 200},
            "q1/polars": {"ok": True, "median_s": 1.0, "peak_rss_bytes": 100},
        },
        "agreement": {"q1": {"agreed": agreed, "by_engine": {}}},
    }


def test_a_disagreed_query_is_not_scored():
    """A different answer is not a faster answer."""
    scores = report.ratios(_document(False), ["pandas", "polars"], "pandas", "polars")
    assert scores["speed"] == {}
    assert scores["speed_geomean"] == 0.0


def test_an_agreed_query_is_scored():
    """The same document with agreement produces the ratio."""
    scores = report.ratios(_document(True), ["pandas", "polars"], "pandas", "polars")
    assert scores["speed"]["q1"] == pytest.approx(2.0)
    assert scores["memory"]["q1"] == pytest.approx(2.0)


def test_a_disagreed_query_is_kept_out_of_the_table():
    """It goes in a section of its own rather than in the timings."""
    text = report.render(_document(False), Path("x.json"))
    assert "| q1 |" not in text
    assert "did not agree" in text


def test_a_pairing_that_did_not_run_is_named_with_its_reason():
    """Dropping it would turn a partial implementation into a clean sweep."""
    document = _document(True)
    document["engines"]["firepanda"] = "0.6.3"
    document["results"]["q1/firepanda"] = {
        "ok": False,
        "note": "groups by a string column",
    }
    text = report.render(document, Path("x.json"))
    assert "groups by a string column" in text


def test_tail_cell_reports_the_ratio_to_the_median():
    """A p99 on its own says nothing about whether the tail is real."""
    assert report.tail_cell({"ok": True, "p99_s": 0.25, "median_s": 0.2}) == "250.0 ms (1.25x)"


def test_tail_cell_says_nothing_when_the_percentile_is_missing():
    """An older result file has no p99, and a dash is the honest cell."""
    assert report.tail_cell({"ok": True, "median_s": 0.2}) == "-"


def test_cpu_cell_carries_the_core_count():
    """Four times faster on sixteen cores is not four times faster on one."""
    entry = {"ok": True, "cpu_user_s": 0.8, "cpu_sys_s": 0.2, "parallelism": 7.5}
    assert report.cpu_cell(entry) == "1000 ms (7.5x)"


def test_bytes_cell_carries_the_ratio_to_the_baseline():
    """Whether 100 MB is good depends entirely on what pandas did on the same query
    on the same machine, which is the thing the table knows and used to not say."""
    mine = {"ok": True, "peak_rss_bytes": 100 << 20}
    base = {"ok": True, "peak_rss_bytes": 400 << 20}
    assert report.bytes_cell(mine, base) == "100 MB (4.00x)"


def test_bytes_cell_does_not_compare_the_baseline_to_itself():
    """A 1.00x against pandas in the pandas column is noise in every row."""
    base = {"ok": True, "peak_rss_bytes": 400 << 20}
    assert report.bytes_cell(base, base) == "400 MB"


def test_bytes_cell_prints_the_bytes_when_the_baseline_did_not_run():
    """The raw number is still the honest one. Only the comparison is missing."""
    mine = {"ok": True, "peak_rss_bytes": 100 << 20}
    assert report.bytes_cell(mine, None) == "100 MB"
    assert report.bytes_cell(mine, {"ok": False}) == "100 MB"


def test_bytes_cell_says_nothing_when_there_is_no_sample():
    assert report.bytes_cell({"ok": True}) == "-"
    assert report.bytes_cell(None) == "-"


def test_a_row_using_more_memory_than_pandas_reads_below_one():
    """The rows we lose are printed in the same table and in the same units. A join
    output larger than either input is larger in every engine, and reading 0.50x is
    the truth about that row rather than a reason to leave it out."""
    mine = {"ok": True, "peak_rss_bytes": 800 << 20}
    base = {"ok": True, "peak_rss_bytes": 400 << 20}
    assert report.bytes_cell(mine, base) == "800 MB (0.50x)"


def _with_subject() -> dict:
    """The two engine document plus firepanda, twice as fast on half the memory."""
    document = _document(True)
    document["engines"]["firepanda"] = "0.6.3"
    document["results"]["q1/firepanda"] = {"ok": True, "median_s": 1.0, "peak_rss_bytes": 100}
    return document


def test_the_headline_is_a_pair_and_not_a_time():
    """The claim is ten times on a tenth of the memory. Leading with the speed and
    putting the memory four tables down answers half of it."""
    line = report.headline(_with_subject(), ["firepanda", "pandas", "polars"])
    assert "2.00x on time and 2.00x on peak memory" in line


def test_the_headline_says_nothing_when_the_subject_did_not_run():
    """A suite pandas and Polars ran without firepanda has no pair to lead with."""
    assert report.headline(_document(True), ["pandas", "polars"]) == ""


def test_the_headline_is_at_the_top_of_the_section():
    """Above the tables rather than below them, which is where a reader stops."""
    text = report.render(_with_subject(), Path("x.json"))
    body = text[: text.index("| query |")]
    assert "on peak memory" in body


def test_the_memory_table_says_which_way_round_it_reads():
    """Above one is less memory used, the same direction as the speed ratios and the
    same direction the compat cost matrix uses."""
    text = report.render(_with_subject(), Path("x.json"))
    assert "Above one is less memory used" in text


def _clickbench(
    size: str, names: tuple[str, ...] = ("q0",), disagreed: tuple[str, ...] = ()
) -> dict:
    """Builds a ClickBench result document with two engines.

    Args:
        size: The dataset size, which is what decides whether the numbers are
            comparable to a published ClickBench one.
        names: Which queries ran. The default is the one query the older tests
            here were written against.
        disagreed: Which of them the engines did not agree on.

    Returns:
        The document.
    """
    results = {}
    agreement = {}
    for name in names:
        results[f"{name}/pandas"] = {"ok": True, "median_s": 2.0, "peak_rss_bytes": 200}
        results[f"{name}/duckdb"] = {"ok": True, "median_s": 1.0, "peak_rss_bytes": 100}
        agreement[name] = {
            "agreed": name not in disagreed,
            "by_engine": {"pandas": "10:aaaa", "duckdb": "10:bbbb"},
        }
    return {
        "suite": "clickbench",
        "size": size,
        "io": "memory",
        "runs": 10,
        "engines": {"pandas": "3.0.5", "duckdb": "1.1.3"},
        "machine": {},
        "results": results,
        "agreement": agreement,
    }


def test_the_report_says_these_are_not_published_clickbench_results():
    """The sentence a reader needs before putting one of these next to a public one."""
    text = report.render(_clickbench("100M"), Path("x.json"))
    assert "not published ClickBench results" in text
    assert "not submitted to the ClickBench table" in text


def test_all_five_differences_are_in_the_report_and_not_only_in_a_readme():
    """A report is read on its own, by somebody who never opened this repository."""
    text = report.render(_clickbench("100M"), Path("x.json"))
    for phrase in ("minimum of three runs", "runs warm", "Load time", "c6a.4xlarge", "size"):
        assert phrase in text


def test_the_report_says_our_distinct_counts_are_exact_and_theirs_are_not():
    """The difference most likely to be misread, and the one that costs us."""
    text = report.render(_clickbench("100M"), Path("x.json"))
    assert "HyperLogLog" in text
    assert "worse rather than better" in text


def test_a_partial_size_is_labelled_where_the_numbers_are():
    """In the heading and again above the table, not in a footnote under it."""
    text = report.render(_clickbench("1M"), Path("x.json"))
    heading = text.splitlines()[0]
    assert "a partial size and not ClickBench" in heading
    above = text[: text.index("| query |")]
    assert "not comparable to a published one" in above


def test_the_full_size_is_not_labelled_as_partial():
    """100M is the hits table, so saying it is not ClickBench would be wrong."""
    text = report.render(_clickbench("100M"), Path("x.json"))
    assert "partial size and not ClickBench" not in text
    assert "which is the full" in text


def test_the_methodology_block_names_the_number_of_runs_the_file_actually_took():
    """A block that said ten while the file says three would be the wrong claim."""
    document = _clickbench("100M")
    document["runs"] = 3
    assert "the median of 3 runs" in report.render(document, Path("x.json"))
    document["runs"] = 1
    assert "the median of 1 run " in report.render(document, Path("x.json"))


def test_another_suite_does_not_get_the_clickbench_block():
    """It is a statement about one published table and it is wrong anywhere else."""
    assert "c6a.4xlarge" not in report.render(_document(True), Path("x.json"))


def test_the_clickbench_table_is_split_into_bands():
    """Forty three rows in one table is a wall. Six blocks with a sentence each is
    the same numbers where a reader can find them."""
    text = report.render(_clickbench("1M", ("q0", "q7", "q27")), Path("x.json"))
    assert "#### scan" in text
    assert "#### groupby" in text
    assert "#### derived" in text
    # The bands a run has no queries in are not empty headings.
    assert "#### window" not in text


def test_a_band_carries_the_sentence_saying_what_is_in_it():
    """A heading that is one word is a heading that gets skipped."""
    text = report.render(_clickbench("1M", ("q27",)), Path("x.json"))
    blurb = queries.CLICKBENCH_BANDS["derived"][1]
    assert blurb in text


def test_a_query_is_in_its_own_band_and_no_other():
    """The blocks are a reading order, and the same query twice would make the
    scorecard above them read as though the suite were longer than it is."""
    text = report.render(_clickbench("1M", ("q0", "q7")), Path("x.json"))
    scan = text[text.index("#### scan") : text.index("#### groupby")]
    assert "| q0 |" in scan
    assert "| q7 |" not in scan


def test_the_check_column_says_which_comparison_is_behind_the_row():
    """The second of the three states, in the row rather than in a section further
    down that a reader has to hold in their head."""
    text = report.render(_clickbench("1M", ("q0", "q11")), Path("x.json"))
    # The wall clock tables only. Every table below them has a row per query too.
    wall = text[: text.index("### Peak memory")].splitlines()
    rows = {line.split("|")[1].strip(): line for line in wall if line.startswith("| q")}
    assert "values" in rows["q0"]
    assert "shape only" in rows["q11"]


def test_a_suite_where_no_query_can_be_undetermined_has_no_check_column():
    """Every row of it would say the same word, which is a column that costs width
    and carries nothing."""
    assert "| check |" not in report.render(_document(True), Path("x.json"))


def test_the_three_agreement_states_are_told_apart():
    """Agrees, does not agree, and was never fully determined. A reader who sees a
    query missing from a table assumes the worst of the three, so the third one is
    in the table with a word on it rather than absent."""
    text = report.render(_clickbench("1M", ("q0", "q11", "q7"), disagreed=("q7",)), Path("x.json"))
    table = text[: text.index("### Peak memory")]
    assert "| q0 |" in table and "values" in table
    assert "| q11 |" in table and "shape only" in table
    assert "| q7 |" not in table
    assert "q7:" in text[text.index("### Queries the engines did not agree on") :]


def test_a_disagreement_on_a_query_nobody_checked_the_values_of_is_the_loud_one():
    """Its name is also in the weaker section above, which reads like the milder
    case. What differed here is the row count or the columns, and the statement
    left neither of those open."""
    text = report.render(_clickbench("1M", ("q11",), disagreed=("q11",)), Path("x.json"))
    assert "not compared at all" in text
    assert "a difference in the row count or the columns" in text


def test_the_weaker_list_says_which_size_it_was_computed_at_when_it_is_another():
    """Which rows tie at the row a limit cuts on depends on how many rows there
    are, so at 10M the list is carried over rather than checked."""
    text = report.render(_clickbench("10M", ("q11",)), Path("x.json"))
    assert "computed at 1M and this file is 10M" in text
    assert "validate-clickbench --size 10M" in text


def test_the_weaker_list_says_nothing_about_the_size_when_it_is_the_right_one():
    """At the size it was computed at there is nothing to warn about."""
    assert "carried over rather than checked" not in report.render(
        _clickbench("1M", ("q11",)), Path("x.json")
    )


def test_the_memory_table_covers_every_query_the_banded_table_does():
    """The bands split the wall clock table and not the suite. A query that is in
    one and not the other is a query somebody will conclude something about."""
    text = report.render(_clickbench("1M", ("q0", "q7", "q27")), Path("x.json"))
    memory = text[text.index("### Peak memory") : text.index("### The tail")]
    for name in ("q0", "q7", "q27"):
        assert f"| {name} |" in memory
