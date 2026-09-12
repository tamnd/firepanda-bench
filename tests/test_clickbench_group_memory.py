"""Tests for the tool that measures what a group by adds on top of the load.

None of these run a query. What they check is the part that would go wrong
quietly: that the default query is one every engine actually implements, that an
engine asked for a query it does not have says so instead of failing somewhere
inside the measurement, and that the two peaks come back in an order that makes
the subtraction mean what the table says it means.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

pytest.importorskip("pyarrow")

import clickbench_group_memory as tool  # noqa: E402

import engines  # noqa: E402


def test_the_default_query_is_the_one_with_no_filter():
    # q31 is the same group by behind a SearchPhrase filter, which cuts the rows
    # before the key is built, so it is the smaller case and not the one to
    # default to when the question is what a nearly unique key costs.
    assert tool.DEFAULT_QUERY == "q32"


def test_every_engine_implements_the_default_query():
    # A query one engine cannot run would leave the table with three rows in it
    # and nothing saying which comparison went missing. An engine that is not
    # installed in the interpreter running the tests is skipped rather than
    # failed, because that is a property of this checkout and not of the tool.
    checked = 0
    for name in tool.ENGINES:
        if name == "firepanda":
            continue
        try:
            module = engines.load_engine(name)
        except ImportError:
            continue
        assert tool.DEFAULT_QUERY in engines.query_map(module, "clickbench")
        checked += 1
    if checked == 0:
        pytest.skip("none of the Python engines are installed here")


def test_a_query_an_engine_does_not_have_says_so():
    with pytest.raises(SystemExit) as caught:
        tool.measure("pandas", "q999", "ignored")
    assert "q999" in str(caught.value)


def test_a_size_that_was_never_downloaded_says_what_to_run():
    with pytest.raises(SystemExit) as caught:
        tool.pattern_for("100M" if not (ROOT / "data" / "clickbench" / "100M").is_dir() else "10M")
    assert "tools/data.py" in str(caught.value)


def test_the_firepanda_reader_subtracts_the_load_from_the_end(monkeypatch):
    # The driver reports two peaks and the tool reports their difference. A
    # reader that took the wrong one of the two would publish the whole process
    # peak as the cost of the group by, which is mostly the loader.
    payload = {
        "ok": True,
        "load_s": 0.9,
        "rows_out": 10,
        "runs": [{"wall_s": 0.03}],
        "peak_rss_after_load_bytes": 3_000_000_000,
        "peak_rss_bytes": 3_400_000_000,
    }
    monkeypatch.setattr(tool, "_read_driver", lambda query, pattern: payload)
    measured = tool.measure_firepanda("q32", "ignored")
    assert measured["peak_after_load_bytes"] == 3_000_000_000
    assert measured["peak_rss_bytes"] == 3_400_000_000
    assert measured["query_rss_bytes"] == 400_000_000
