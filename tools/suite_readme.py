#!/usr/bin/env python3
"""Puts the current numbers into the READMEs, generated rather than typed.

Ten result files a week go into a workflow artifact, a step summary and a static
page, and not one of those is what somebody looks at when they arrive. They open
the repository and they read a README. What the suite READMEs said before this
was what the suite is and how the port was done, which is the right thing to say
and is not a number.

So each suite README carries a block of current numbers between two markers, and
the repository README carries one row per suite saying how the comparison is
going. Everything between the markers is written by this script and everything
outside them is written by a person, and the closing marker holds a digest of the
content so an edit inside the block is caught by `--check` without needing the
result files that produced it.

What a block answers, per engine, with the ratio against pandas in the direction
the rest of this repository uses, where above one is better:

Throughput is input rows per second rather than answer rows per second, because a
query returning ten rows out of a hundred million did not do ten rows of work.
Latency is the median with the ninety ninth percentile over it beside it, because
a median with no spread is not a measurement and the tail is what somebody putting
a query behind a service is buying. Resource usage is peak resident set, CPU
seconds, and the cores those two imply, because an engine twice as fast on ten
cores is not the same result as an engine twice as fast on one. Coverage is how
many of the suite's queries the engine actually ran, because an engine missing
from half a table looks fast on the other half.

Nothing is aggregated across a machine, a size or an io mode. Each of those gets
its own table with its own heading, because none of them is comparable to another
and a mean over two of them is a number about neither.

The numbers come from `results/*.json`, which is gitignored, and this script never
fetches anything. A suite with no result file in the directory keeps whatever its
block already said rather than being emptied, so running this on a fresh checkout
does not throw away the last reviewed numbers. That is also why the scheduled run
opens a pull request with the refreshed blocks rather than pushing: a number that
lands in a commit nobody reviewed is a number nobody checked, and between runs the
README is the last state a human looked at and says which run it came from.

Usage:
    pixi run suite-readme --results results
    pixi run suite-readme --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import queries as query_registry
import report as report_tool

ROOT = Path(__file__).resolve().parent.parent

# The engine everything else is divided by, which is the one most people are
# actually running.
BASELINE = "pandas"

# The marker pair. The closing one carries a digest of what is between them, so a
# hand edit inside the block is caught by a checkout with no result files in it.
# It catches an accident rather than a forgery: anybody can edit the block and run
# this script again to reseal it, and that is the intended way to change it.
OPEN = "<!-- suite-readme: generated, do not edit between these markers -->"
CLOSE = "<!-- suite-readme: end, sha256 {digest} -->"

# Which README gets which block. The udf suite is in here with the others even
# though it has no runs yet, because a suite with an empty block says it has not
# run and a suite with no block says nothing at all.
SUITE_READMES = {
    "db-benchmark": ROOT / "suites" / "db-benchmark" / "README.md",
    "tpch": ROOT / "suites" / "tpch" / "README.md",
    "ingestion": ROOT / "suites" / "ingestion" / "README.md",
    "clickbench": ROOT / "suites" / "clickbench" / "README.md",
    "udf": ROOT / "suites" / "udf" / "README.md",
}

SUMMARY_README = ROOT / "README.md"


def shown(path: Path) -> str:
    """Returns the shortest name for a path that a reader can act on.

    Args:
        path: Any path.

    Returns:
        The path relative to the repository when it is inside it, and the whole
        path when it is not, which is what happens under a test.
    """
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def digest_of(lines: list[str]) -> str:
    """Returns the digest the closing marker carries.

    Args:
        lines: The block content, without the markers.

    Returns:
        The first sixteen characters of the SHA-256 of the content, which is
        plenty for catching an edit and short enough to read.

    """
    body = "\n".join(lines).strip() + "\n"
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def split(text: str, path: Path) -> tuple[str, list[str], str, str]:
    """Cuts a document into what is before the block, the block, and what is after.

    Args:
        text: The whole file.
        path: Where it came from, for the error message.

    Returns:
        The text before the opening marker, the block content as lines, the digest
        the closing marker claims, and the text after the closing marker.

    Raises:
        SystemExit: If the markers are missing, out of order, or repeated.
    """
    if text.count(OPEN) != 1:
        raise SystemExit(f"{path} needs exactly one opening suite-readme marker")
    head, rest = text.split(OPEN, 1)
    ends = [line for line in rest.splitlines() if line.startswith("<!-- suite-readme: end")]
    if len(ends) != 1:
        raise SystemExit(f"{path} needs exactly one closing suite-readme marker after the opening")
    closing = ends[0]
    body, tail = rest.split(closing, 1)
    claimed = closing.rsplit("sha256", 1)[-1].strip().rstrip("->").strip()
    return head, body.strip().splitlines(), claimed, tail


def splice(text: str, lines: list[str], path: Path) -> str:
    """Puts a block back into a document and seals it.

    Args:
        text: The whole file as it is now.
        lines: The block content to write.
        path: Where it came from, for the error message.

    Returns:
        The whole file with the block replaced and the digest recomputed.
    """
    head, _, _, tail = split(text, path)
    body = "\n".join(lines).strip()
    return f"{head}{OPEN}\n\n{body}\n\n{CLOSE.format(digest=digest_of(lines))}{tail}"


def load(directory: Path) -> list[tuple[Path, dict]]:
    """Reads every result file in a directory.

    Args:
        directory: Where the result files are.

    Returns:
        The readable ones, paired with their paths, sorted by path so the output
        does not depend on what order the filesystem hands them over in.
    """
    found = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if "results" in document and "suite" in document:
            found.append((path, document))
    return found


def stamp(path: Path) -> str:
    """Returns the date a result file was written.

    The file has no clock in it. The runner puts the date at the front of the name
    and that is where this comes from, which is also where the site gets it.

    Args:
        path: The result file.

    Returns:
        The date, or the file stem when the name is not in the usual shape.
    """
    parts = path.stem.split("-")
    return "-".join(parts[:3]) if len(parts) >= 3 else path.stem


def host_of(document: dict) -> str:
    """Returns the name of the machine a run happened on.

    Args:
        document: The result document.

    Returns:
        The host name, falling back to the CPU model.
    """
    machine = document.get("machine", {})
    return machine.get("host") or machine.get("cpu_model") or "unknown machine"


def dataset_rows(document: dict) -> int:
    """Returns how many rows the whole dataset a run read has.

    Used to order the tables, so the biggest run in a suite is the one the
    repository README quotes. Ordering by the size name would need a table of size
    names per suite and would be wrong the day somebody adds one.

    Args:
        document: The result document.

    Returns:
        The row count, or zero when the manifest does not carry one.
    """
    dataset = document.get("dataset", {})
    if dataset.get("rows"):
        return int(dataset["rows"])
    table_rows = dataset.get("table_rows") or {}
    if table_rows:
        return sum(int(value) for value in table_rows.values())
    total = 0
    for entry in (dataset.get("files") or {}).values():
        total += int((entry.get("parquet") or {}).get("rows", 0))
    return total


def query_rows(document: dict, query) -> int:
    """Returns how many input rows one query reads.

    Throughput per query needs the rows that query touched and not the rows the
    suite has. The ingestion suite's wide file is a tenth of the height of its
    narrow one, and a throughput column that used the suite row count would be
    wrong about it by a factor of ten.

    Args:
        document: The result document.
        query: The query.

    Returns:
        The row count across the tables it reads, falling back to the dataset's own
        row count when the manifest does not name them, which is what ClickBench
        does because its files are partitions rather than tables.
    """
    dataset = document.get("dataset", {})
    table_rows = dataset.get("table_rows") or {}
    files = dataset.get("files") or {}
    total = 0
    for name in query.needs:
        if name in table_rows:
            total += int(table_rows[name])
        elif name in files:
            total += int((files[name].get("parquet") or {}).get("rows", 0))
    return total or int(dataset.get("rows") or 0)


def fmt_seconds(value: float) -> str:
    """Formats a duration the way the report tables do.

    Args:
        value: Seconds.

    Returns:
        The text.
    """
    if value <= 0:
        return "-"
    if value < 0.001:
        return f"{value * 1e6:.0f} us"
    if value < 1:
        return f"{value * 1000:.1f} ms"
    return f"{value:.2f} s"


def fmt_rate(value: float) -> str:
    """Formats a throughput in rows per second.

    Args:
        value: Rows per second.

    Returns:
        The text.
    """
    if value <= 0:
        return "-"
    if value >= 1e9:
        return f"{value / 1e9:.2f} G rows/s"
    if value >= 1e6:
        return f"{value / 1e6:.1f} M rows/s"
    if value >= 1e3:
        return f"{value / 1e3:.0f} K rows/s"
    return f"{value:.0f} rows/s"


def fmt_bytes(value: float) -> str:
    """Formats a byte count.

    Args:
        value: Bytes.

    Returns:
        The text.
    """
    if value <= 0:
        return "-"
    if value >= 1 << 30:
        return f"{value / (1 << 30):.2f} GB"
    return f"{value / (1 << 20):.0f} MB"


def measured(document: dict, engines: list[str], engine: str) -> list[tuple[str, dict]]:
    """Returns the entries for one engine that belong in a summary number.

    A query the engines disagreed on is not a result, so it is not in any of these
    aggregates, the same rule the per query tables in the report follow. A query an
    engine could not run is not in them either, and the coverage column is where
    that shows rather than here, because averaging over a smaller set of queries
    would turn a refusal into a faster mean.

    Args:
        document: The result document.
        engines: Every engine in the run.
        engine: The one being summarized.

    Returns:
        The query name and entry for each usable pairing.
    """
    usable = []
    for query in query_registry.for_suite(document["suite"]):
        if not report_tool.comparable(document, query.name, engines):
            continue
        entry = document["results"].get(f"{query.name}/{engine}")
        if entry and entry.get("ok"):
            usable.append((query.name, entry))
    return usable


def summarize(document: dict, engines: list[str], engine: str) -> dict:
    """Reduces one engine's whole run to the numbers the block prints.

    Every number here is a geometric mean over the same set of queries, including
    the peak resident set. One rule for the whole table is worth more than a column
    that is a maximum, because a reader who has to remember which column is which
    will divide two cells that were never comparable. The largest single peak is a
    real number and it is in the per query tables the report prints.

    Args:
        document: The result document.
        engines: Every engine in the run.
        engine: The one being summarized.

    Returns:
        The numbers, with zeros where the run did not carry the inputs.
    """
    rows = measured(document, engines, engine)
    total = len(query_registry.for_suite(document["suite"]))
    ran = sum(
        1
        for query in query_registry.for_suite(document["suite"])
        if (document["results"].get(f"{query.name}/{engine}") or {}).get("ok")
    )
    medians = [entry["median_s"] for _, entry in rows if entry.get("median_s")]
    tails = [
        entry["p99_s"] / entry["median_s"]
        for _, entry in rows
        if entry.get("p99_s") and entry.get("median_s")
    ]
    rates = []
    for name, entry in rows:
        count = query_rows(document, query_registry.lookup(document["suite"], name))
        if count and entry.get("median_s"):
            rates.append(count / entry["median_s"])
    cpu = [
        float(entry.get("cpu_user_s", 0.0)) + float(entry.get("cpu_sys_s", 0.0))
        for _, entry in rows
        if float(entry.get("cpu_user_s", 0.0)) + float(entry.get("cpu_sys_s", 0.0)) > 0
    ]
    cores = [float(entry["parallelism"]) for _, entry in rows if entry.get("parallelism")]
    peaks = [entry["peak_rss_bytes"] for _, entry in rows if entry.get("peak_rss_bytes")]
    return {
        "ran": ran,
        "total": total,
        "counted": len(rows),
        "median_s": report_tool.geometric_mean(medians),
        "tail": report_tool.geometric_mean(tails),
        "rate": report_tool.geometric_mean(rates),
        "cpu_s": report_tool.geometric_mean(cpu),
        "cores": report_tool.geometric_mean(cores),
        "peak_rss_bytes": report_tool.geometric_mean(peaks),
    }


def against_baseline(document: dict, engines: list[str], engine: str) -> dict:
    """Returns one engine's speed and memory ratios against pandas.

    Taken per query and then averaged, rather than by dividing the two summary
    numbers above. The two differ whenever an engine skipped a query, and the per
    query form is the one that cannot be improved by refusing to run something.

    Args:
        document: The result document.
        engines: Every engine in the run.
        engine: The one being scored.

    Returns:
        The two geometric means, both zero when pandas is not in the run.
    """
    if BASELINE not in engines or engine == BASELINE:
        return {"speed": 0.0, "memory": 0.0}
    scored = report_tool.ratios(document, engines, BASELINE, engine)
    return {"speed": scored["speed_geomean"], "memory": scored["memory_geomean"]}


def ratio(value: float) -> str:
    """Formats a ratio for a cell that already has a number in it.

    Args:
        value: The ratio, or zero when there is not one.

    Returns:
        The parenthesized text, or an empty string.
    """
    return f" ({value:.2f}x)" if value > 0 else ""


def engine_table(document: dict) -> list[str]:
    """Renders one run as a table with a row per engine.

    Args:
        document: The result document.

    Returns:
        The markdown lines.
    """
    engines = list(document.get("engines", {}))
    lines = [
        "| engine | ran | time | p99 over median | throughput | peak memory | CPU |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for engine in engines:
        numbers = summarize(document, engines, engine)
        scored = against_baseline(document, engines, engine)
        version = document["engines"].get(engine) or ""
        name = f"{engine} {version}".strip()
        # pandas is every ratio's denominator, so the row a reader has to find to
        # make sense of the other rows is the one that is marked.
        cells = [f"**{name}**" if engine == BASELINE else name]
        cells.append(f"{numbers['ran']}/{numbers['total']}")
        cells.append(fmt_seconds(numbers["median_s"]) + ratio(scored["speed"]))
        cells.append(f"{numbers['tail']:.2f}x" if numbers["tail"] else "-")
        cells.append(fmt_rate(numbers["rate"]))
        cells.append(fmt_bytes(numbers["peak_rss_bytes"]) + ratio(scored["memory"]))
        cpu = fmt_seconds(numbers["cpu_s"])
        cells.append(f"{cpu} ({numbers['cores']:.1f} cores)" if numbers["cpu_s"] else "-")
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def run_heading(path: Path, document: dict) -> str:
    """The heading over one run's table.

    It names all four of the things nothing is comparable across, because a table
    that does not say which machine, which size and which io mode it is from is a
    table somebody will average with another one.

    Args:
        path: The result file.
        document: The result document.

    Returns:
        The heading line.
    """
    return (
        f"### {document['size']}, {document.get('io', 'memory')} io, "
        f"on {host_of(document)}, {stamp(path)}"
    )


def counted_note(document: dict) -> str:
    """Says how many queries the numbers above are an average over.

    Args:
        document: The result document.

    Returns:
        The sentence.
    """
    engines = list(document.get("engines", {}))
    suite = query_registry.for_suite(document["suite"])
    agreed = sum(1 for query in suite if report_tool.comparable(document, query.name, engines))
    left = len(suite) - agreed
    text = (
        f"Every number in that table is a geometric mean over the same {agreed} of "
        f"{len(suite)} queries, the ones every engine that answered agreed on, at "
        f"{document['runs']} runs per pairing. The ratios are taken per query and then "
        f"averaged rather than by dividing two columns, so an engine cannot improve one "
        f"by skipping the query it is slowest on."
    )
    if left:
        text += (
            f" The other {left} are in none of these numbers, because a query nobody "
            f"answered has nothing to average and a query two engines answered "
            f"differently is not a result whatever the times next to it say. The `ran` "
            f"column counts them, and `pixi run report` names them one by one."
        )
    return text


def pick_runs(documents: list[tuple[Path, dict]], suite: str) -> list[tuple[Path, dict]]:
    """Chooses which runs of a suite get a table.

    One per machine, size and io mode, the newest of each, because those are the
    four things nothing is comparable across and an older run of the same four is
    history rather than news. The history is on the site as a chart.

    Args:
        documents: Every result file that was read.
        suite: Which suite.

    Returns:
        The chosen runs, largest dataset first, then by machine and io mode.
    """
    newest: dict[tuple[str, str, str], tuple[Path, dict]] = {}
    for path, document in documents:
        if document["suite"] != suite:
            continue
        key = (host_of(document), document["size"], document.get("io", "memory"))
        if key not in newest or stamp(path) >= stamp(newest[key][0]):
            newest[key] = (path, document)
    return sorted(
        newest.values(),
        key=lambda pair: (
            -dataset_rows(pair[1]),
            host_of(pair[1]),
            pair[1].get("io", "memory"),
        ),
    )


def suite_block(documents: list[tuple[Path, dict]], suite: str) -> list[str]:
    """Builds the generated block for one suite README.

    Args:
        documents: Every result file that was read.
        suite: Which suite.

    Returns:
        The markdown lines, or an empty list when this suite has no run, which
        means the block already in the file is left alone.
    """
    runs = pick_runs(documents, suite)
    if not runs:
        return []
    lines = [
        "## Where this suite stands",
        "",
        "Generated by `pixi run suite-readme` from the result files of the runs named "
        "below. One table per machine, size and io mode, because nothing is comparable "
        "across any of those. Every ratio is against pandas and above one is better, "
        "which is the direction the report and the compat cost matrix both use.",
    ]
    for path, document in runs:
        lines.append("")
        lines.append(run_heading(path, document))
        lines.append("")
        lines.extend(engine_table(document))
        lines.append("")
        lines.append(counted_note(document))
    return lines


def summary_row(path: Path, document: dict) -> str:
    """Renders one suite's row in the repository README table.

    Args:
        path: The result file the row is from.
        document: The result document.

    Returns:
        The markdown line.
    """
    engines = list(document.get("engines", {}))
    cells = []
    for engine in ("polars", "duckdb", "firepanda"):
        if engine not in engines:
            cells.append("-")
            continue
        scored = against_baseline(document, engines, engine)
        numbers = summarize(document, engines, engine)
        if not scored["speed"]:
            cells.append(f"0 of {numbers['total']}")
            continue
        memory = f"{scored['memory']:.2f}x" if scored["memory"] else "-"
        cells.append(f"{scored['speed']:.2f}x / {memory}")
    suite = document["suite"]
    where = f"{document['size']}, {document.get('io', 'memory')}, {stamp(path)}"
    return f"| [{suite}](suites/{suite}) | {where} | " + " | ".join(cells) + " |"


def summary_block(documents: list[tuple[Path, dict]]) -> list[str]:
    """Builds the generated block for the repository README.

    One row per suite, from the largest run of it, because the front page is a
    place to see how the comparison is going rather than a place to hold four
    variables in your head.

    Args:
        documents: Every result file that was read.

    Returns:
        The markdown lines, or an empty list when there is no run at all.
    """
    rows = []
    for suite in SUITE_READMES:
        runs = pick_runs(documents, suite)
        if runs:
            rows.append(summary_row(*runs[0]))
    if not rows:
        return []
    return [
        "| suite | from | Polars | DuckDB | firepanda |",
        "| --- | --- | ---: | ---: | ---: |",
        *rows,
        "",
        "Speed then peak memory against pandas, geometric means over the queries every "
        "engine agreed on, above one better. One row per suite from the largest run of "
        "it, named in the second column, because a number from one size and io mode is "
        "not comparable to a number from another. A cell saying how many of the suite an "
        "engine answered is an engine that answered none of it. Each suite README has "
        "the same numbers per machine and size with throughput, latency spread and CPU "
        "beside them, and `pixi run report` has them per query.",
    ]


def write(path: Path, lines: list[str], check: bool) -> list[str]:
    """Writes one block, or checks it.

    Args:
        path: The README.
        lines: The block content, empty to leave the content alone and only reseal.
        check: Whether to check rather than write.

    Returns:
        The complaints, empty when there is nothing wrong.
    """
    text = path.read_text()
    _, current, claimed, _ = split(text, path)
    wanted = lines or current
    if check:
        problems = []
        if claimed != digest_of(current):
            problems.append(
                f"{shown(path)} has been edited between the markers, "
                f"which is a block `pixi run suite-readme` writes. Run it again."
            )
        elif lines and current != lines:
            problems.append(
                f"{shown(path)} is stale against the result files in this "
                f"directory. Run `pixi run suite-readme`."
            )
        return problems
    updated = splice(text, wanted, path)
    if updated != text:
        path.write_text(updated)
        print(f"wrote {shown(path)}")
    return []


def main(argv: list[str] | None = None) -> int:
    """Regenerates every block, or checks every block.

    Args:
        argv: The arguments, or None for the real ones.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when a block was edited by hand or is stale against the results",
    )
    args = parser.parse_args(argv)

    documents = load(args.results) if args.results.exists() else []
    problems = []
    for suite, path in SUITE_READMES.items():
        problems += write(path, suite_block(documents, suite), args.check)
    problems += write(SUMMARY_README, summary_block(documents), args.check)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    if args.check:
        count = len(documents)
        print(f"{count} result file(s) under {shown(args.results)} agree with the blocks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
