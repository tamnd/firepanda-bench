"""Tests for the generated blocks in the READMEs.

A generated block has two ways to go wrong and neither of them is loud. Somebody
edits it by hand and the next run silently reverts them, or the script touches
something outside the markers and a paragraph a person wrote disappears into a
commit titled "refresh the numbers". Both are checked here.

The rest is about what the numbers mean. The ratios have to be taken per query
and averaged rather than by dividing two summary numbers, because the second form
gets better when an engine refuses to run the query it is slowest on, and that is
exactly the behaviour this repository exists to not reward.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import queries  # noqa: E402
import suite_readme  # noqa: E402


def _entry(median: float, peak: int, **overrides) -> dict:
    entry = {
        "ok": True,
        "median_s": median,
        "p99_s": median * 1.2,
        "peak_rss_bytes": peak,
        "cpu_user_s": median,
        "cpu_sys_s": 0.0,
        "parallelism": 1.0,
        "note": "",
    }
    entry.update(overrides)
    return entry


def _document(**overrides) -> dict:
    """A db-benchmark run where pandas and Polars answered q1 and q2."""
    document = {
        "suite": "db-benchmark",
        "size": "0.5GB",
        "io": "memory",
        "runs": 10,
        "engines": {"pandas": "3.0.5", "polars": "1.44.1"},
        "machine": {"host": "gamingpc"},
        "dataset": {"rows": 10_000_000, "table_rows": {"groupby": 10_000_000}},
        "agreement": {
            "q1": {"agreed": True},
            "q2": {"agreed": True},
        },
        "results": {
            "q1/pandas": _entry(2.0, 1 << 30),
            "q1/polars": _entry(0.5, 1 << 29),
            "q2/pandas": _entry(4.0, 1 << 30),
            "q2/polars": _entry(1.0, 1 << 29),
        },
    }
    document.update(overrides)
    return document


def _readme(tmp_path: Path, body: str = "nothing yet") -> Path:
    path = tmp_path / "README.md"
    path.write_text(
        "# A suite\n\nWritten by a person.\n\n"
        f"{suite_readme.OPEN}\n\n{body}\n\n"
        f"{suite_readme.CLOSE.format(digest=suite_readme.digest_of([body]))}\n\n"
        "## Also written by a person\n\nStill here.\n"
    )
    return path


def test_writing_a_block_leaves_everything_outside_the_markers_alone(tmp_path):
    path = _readme(tmp_path)
    suite_readme.write(path, ["one", "two"], check=False)
    text = path.read_text()
    assert text.startswith("# A suite\n\nWritten by a person.\n")
    assert text.endswith("## Also written by a person\n\nStill here.\n")
    assert "one\ntwo" in text


def test_an_edit_between_the_markers_is_caught_with_no_result_files_at_all(tmp_path):
    path = _readme(tmp_path)
    path.write_text(path.read_text().replace("nothing yet", "nothing yet, and we are winning"))
    problems = suite_readme.write(path, [], check=True)
    assert len(problems) == 1
    assert "edited between the markers" in problems[0]


def test_a_block_that_matches_its_digest_passes_the_check(tmp_path):
    assert suite_readme.write(_readme(tmp_path), [], check=True) == []


def test_a_block_that_is_stale_against_the_results_is_caught(tmp_path):
    path = _readme(tmp_path)
    problems = suite_readme.write(path, ["the current numbers"], check=True)
    assert len(problems) == 1
    assert "stale" in problems[0]


def test_resealing_makes_a_hand_edited_block_pass_again(tmp_path):
    path = _readme(tmp_path)
    path.write_text(path.read_text().replace("nothing yet", "said differently"))
    suite_readme.write(path, [], check=False)
    assert suite_readme.write(path, [], check=True) == []


def test_a_missing_marker_is_an_error_rather_than_a_silent_skip(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("# A suite\n\nNo markers here.\n")
    with pytest.raises(SystemExit):
        suite_readme.write(path, ["one"], check=True)


def test_a_second_pair_of_markers_is_an_error(tmp_path):
    path = _readme(tmp_path)
    path.write_text(path.read_text() + f"\n{suite_readme.OPEN}\n\nagain\n\n")
    with pytest.raises(SystemExit):
        suite_readme.write(path, ["one"], check=True)


def test_a_suite_with_no_result_file_keeps_the_block_it_has(tmp_path):
    # Running the script on a fresh checkout must not empty the READMEs, because a
    # checkout has no result files in it and `results/*.json` is gitignored.
    assert suite_readme.suite_block([], "tpch") == []
    path = _readme(tmp_path, body="the numbers from the last run")
    suite_readme.write(path, [], check=False)
    assert "the numbers from the last run" in path.read_text()


def test_the_speed_ratio_is_taken_per_query_rather_than_between_two_means():
    # Polars is four times faster on both queries, so both forms agree here.
    document = _document()
    scored = suite_readme.against_baseline(document, ["pandas", "polars"], "polars")
    assert scored["speed"] == pytest.approx(4.0)

    # Now Polars refuses the query it is worst on. Dividing the two summary means
    # would hand it a better number for having run less. Taken per query it keeps
    # the one query it answered, and the coverage column is where the refusal shows.
    document["results"]["q1/polars"] = {"ok": False, "note": "not implemented"}
    scored = suite_readme.against_baseline(document, ["pandas", "polars"], "polars")
    assert scored["speed"] == pytest.approx(4.0)
    numbers = suite_readme.summarize(document, ["pandas", "polars"], "polars")
    assert numbers["ran"] == 1
    assert numbers["total"] == len(queries.for_suite("db-benchmark"))


def test_a_query_the_engines_disagreed_on_is_in_none_of_the_numbers():
    document = _document()
    document["agreement"]["q2"] = {"agreed": False}
    numbers = suite_readme.summarize(document, ["pandas", "polars"], "pandas")
    assert numbers["counted"] == 1
    # It still ran, so the coverage column still counts it. A disagreement is a
    # reason not to quote a timing and not a reason to pretend nobody answered.
    assert numbers["ran"] == 2


def test_throughput_is_input_rows_and_not_answer_rows():
    document = _document()
    numbers = suite_readme.summarize(document, ["pandas", "polars"], "pandas")
    # Ten million rows over two and four seconds, geometric mean of the two rates.
    assert numbers["rate"] == pytest.approx((5e6 * 2.5e6) ** 0.5)


def test_throughput_uses_the_rows_the_query_read_rather_than_the_suite_total():
    # The ingestion suite's wide file is a tenth of the height of its narrow one, so
    # a throughput taken from the suite row count is wrong about it by ten times.
    document = {
        "suite": "ingestion",
        "dataset": {"rows": 10_000_000, "table_rows": {"wide": 1_000_000}},
    }
    wide = next(q for q in queries.for_suite("ingestion") if q.needs == ("wide",))
    assert suite_readme.query_rows(document, wide) == 1_000_000


def test_a_table_names_the_four_things_nothing_is_comparable_across(tmp_path):
    path = tmp_path / "2026-08-28-gamingpc-db-benchmark-0.5GB-memory.json"
    path.write_text(json.dumps(_document()))
    heading = suite_readme.run_heading(path, _document())
    assert "0.5GB" in heading
    assert "memory io" in heading
    assert "gamingpc" in heading
    assert "2026-08-28" in heading


def test_one_table_per_machine_size_and_io_mode_and_the_newest_of_each(tmp_path):
    old = tmp_path / "2026-08-01-gamingpc-db-benchmark-0.5GB-memory.json"
    new = tmp_path / "2026-08-28-gamingpc-db-benchmark-0.5GB-memory.json"
    other = tmp_path / "2026-08-28-gamingpc-db-benchmark-0.5GB-scan.json"
    for path in (old, new):
        path.write_text(json.dumps(_document()))
    other.write_text(json.dumps(_document(io="scan")))
    runs = suite_readme.pick_runs(suite_readme.load(tmp_path), "db-benchmark")
    assert [path.name for path, _ in runs] == [new.name, other.name]


def test_the_biggest_run_is_the_one_the_front_page_quotes(tmp_path):
    small = tmp_path / "2026-08-28-gamingpc-db-benchmark-0.5GB-memory.json"
    big = tmp_path / "2026-08-28-gamingpc-db-benchmark-5GB-memory.json"
    small.write_text(json.dumps(_document()))
    big.write_text(
        json.dumps(
            _document(size="5GB", dataset={"rows": 100_000_000, "table_rows": {"groupby": 1}})
        )
    )
    runs = suite_readme.pick_runs(suite_readme.load(tmp_path), "db-benchmark")
    assert runs[0][1]["size"] == "5GB"


def test_the_repository_block_says_which_run_each_row_is_from(tmp_path):
    path = tmp_path / "2026-08-28-gamingpc-db-benchmark-0.5GB-memory.json"
    path.write_text(json.dumps(_document()))
    lines = suite_readme.summary_block(suite_readme.load(tmp_path))
    row = next(line for line in lines if line.startswith("| [db-benchmark]"))
    assert "0.5GB, memory, 2026-08-28" in row
    assert "4.00x / 2.00x" in row
    # DuckDB and firepanda were not in the run at all, which is not the same thing
    # as having run none of it, so they are a dash rather than a zero.
    assert row.endswith("| - | - |")


def test_an_engine_in_the_run_that_answered_nothing_says_so_rather_than_showing_a_dash(tmp_path):
    document = _document()
    document["engines"]["duckdb"] = "1.5.5"
    document["results"]["q1/duckdb"] = {"ok": False, "note": "cannot read this"}
    document["results"]["q2/duckdb"] = {"ok": False, "note": "cannot read this"}
    path = tmp_path / "2026-08-28-gamingpc-db-benchmark-0.5GB-memory.json"
    path.write_text(json.dumps(document))
    lines = suite_readme.summary_block(suite_readme.load(tmp_path))
    row = next(line for line in lines if line.startswith("| [db-benchmark]"))
    assert "0 of " in row


def test_every_readme_this_script_owns_has_a_well_formed_block():
    # The real files, so a README that loses its markers in a rebase fails here
    # rather than in the scheduled run three days later.
    for path in [*suite_readme.SUITE_READMES.values(), suite_readme.SUMMARY_README]:
        assert suite_readme.write(path, [], check=True) == []
