#!/usr/bin/env python3
"""Tests for the ClickBench entries in the query registry.

The registry says what a query is in words, and words cannot be checked by a test.
What can be checked is that the set is the published set: 43 entries, named the way
ClickBench names them, in the order ClickBench publishes them. A registry that is
missing q30 or that calls it q31 produces a result file nobody can read next to a
published one, and that failure is silent everywhere else.

The rest of these pin the handful of facts about the suite that later work depends
on being true, so that a change to the registry which breaks one of them fails here
rather than three ports later.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import queries as query_registry
import run


def test_the_suite_is_the_published_forty_three():
    assert len(query_registry.CLICKBENCH) == 43


def test_the_names_are_q0_through_q42_in_order():
    # Numbered from zero because ClickBench numbers from zero. Anybody reading our
    # q22 next to a published q22 has to be looking at the same query, so this is
    # not a naming preference and it is not ours to change.
    names = [q.name for q in query_registry.CLICKBENCH]
    assert names == [f"q{i}" for i in range(43)]


def test_every_entry_belongs_to_the_suite_and_reads_the_one_table():
    for query in query_registry.CLICKBENCH:
        assert query.suite == "clickbench"
        assert query.group in query_registry.CLICKBENCH_BANDS
        assert query.needs == ("hits",)


def test_every_query_is_in_exactly_one_band():
    # The bands are what the report splits its table into, so a query in two of
    # them is a query counted twice and a query in none of them is one that quietly
    # stops being published.
    banded = [name for names, _ in query_registry.CLICKBENCH_BANDS.values() for name in names]
    assert sorted(banded) == sorted(q.name for q in query_registry.CLICKBENCH)
    assert len(banded) == len(set(banded)) == 43


def test_every_band_is_a_contiguous_range_of_the_published_numbering():
    # They are reading order and not a taxonomy. Almost every query in this suite
    # filters, groups and sorts at once, so a taxonomy would either put most of the
    # suite in one bucket or need a query in three, and a range cannot overlap.
    seen = []
    for names, _ in query_registry.CLICKBENCH_BANDS.values():
        numbers = [int(name[1:]) for name in names]
        assert numbers == list(range(numbers[0], numbers[-1] + 1))
        seen.append(numbers[0])
    assert seen == sorted(seen)


def test_every_band_says_what_is_in_it():
    # The sentence goes under the heading in the report. A heading that is one word
    # is a heading that gets skipped.
    for band, (_, blurb) in query_registry.CLICKBENCH_BANDS.items():
        assert len(blurb) > 120, f"the {band} band has a sentence nobody would read"


def test_every_entry_says_what_it_computes_and_what_it_exposes():
    for query in query_registry.CLICKBENCH:
        assert query.description, f"{query.name} says nothing about what it computes"
        # Long enough to be a reason rather than a restatement of the SQL. The
        # shortest of the 43 is comfortably over this.
        assert len(query.why) > 120, f"{query.name} has a why nobody would read"


def test_the_suite_is_registered_and_its_groups_are_the_bands():
    assert query_registry.SUITES["clickbench"] is query_registry.CLICKBENCH
    assert query_registry.GROUPS["clickbench"] == tuple(query_registry.CLICKBENCH_BANDS)


def test_a_band_name_selects_that_band_and_nothing_else():
    # Rerunning seven queries to look at a change in a Parquet reader beats
    # rerunning 43.
    selected = query_registry.select("derived", "clickbench")
    assert [q.name for q in selected] == ["q27", "q28", "q29"]


def test_all_selects_the_whole_suite():
    assert len(query_registry.select("all", "clickbench")) == 43


def test_a_subset_comes_back_in_registry_order_and_not_in_the_order_asked_for():
    # Same rule as the other suites. A result file whose query order depends on how
    # somebody typed the argument is harder to diff against another run.
    selected = query_registry.select("q29,q4,q0", "clickbench")
    assert [q.name for q in selected] == ["q0", "q4", "q29"]


def test_a_name_from_another_suite_is_refused():
    # db-benchmark has a j1 and ClickBench does not, and picking the wrong suite is
    # an easy mistake to make from the command line.
    with pytest.raises(SystemExit):
        query_registry.select("j1", "clickbench")


def test_q43_does_not_exist():
    # The off by one this suite invites. There are 43 queries and the last is q42.
    with pytest.raises(SystemExit):
        query_registry.lookup("clickbench", "q43")


def test_the_default_size_is_the_one_that_does_not_need_a_twelve_gigabyte_download():
    # Deliberately not the published size. A first run that spends an hour on the
    # network before it prints anything is a run nobody makes twice.
    assert run.DEFAULT_SIZE["clickbench"] == "1M"


def test_the_suite_defaults_to_memory_mode():
    assert run.DEFAULT_IO.get("clickbench", "memory") == "memory"


def test_the_queries_that_group_name_their_keys():
    # Not every query groups, so this is not a blanket rule. What it does check is
    # that the ones whose why talks about a grouping key actually carry one, since
    # the keys field is what the report groups the table by.
    by_name = {q.name: q for q in query_registry.CLICKBENCH}
    for name in ("q7", "q15", "q32", "q33", "q35", "q42"):
        assert by_name[name].keys, f"{name} groups and declares no key"


def test_the_scalar_queries_declare_no_key():
    # q0 through q6 are whole table aggregates with nothing to group by, and q19 and
    # q20 filter without grouping. A key on any of them would be wrong rather than
    # merely unhelpful.
    by_name = {q.name: q for q in query_registry.CLICKBENCH}
    for name in ("q0", "q1", "q2", "q3", "q4", "q5", "q6", "q19", "q20", "q23"):
        assert by_name[name].keys == (), f"{name} declares a grouping key and does not group"


def test_the_four_correlated_keys_are_all_recorded():
    # q35 groups by a column and three expressions over it. The whole point of the
    # query is that the last three add nothing, which only reads that way if all
    # four are written down.
    keys = query_registry.lookup("clickbench", "q35").keys
    assert len(keys) == 4
    assert all("ClientIP" in key for key in keys)
