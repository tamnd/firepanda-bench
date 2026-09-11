#!/usr/bin/env python3
"""Turns a result file into the table that gets published.

The project's stated goal is ten times the speed of pandas and a tenth of the
memory. This is the script that says whether that happened, and it is written so
that it cannot say yes by accident.

Three rules it enforces:

A query where the engines disagreed is not a result. If two engines produced
different row counts or different column sums they did not run the same query,
and the faster one is not faster, it is wrong. Those rows are moved out of the
table and into a section of their own.

A query an engine could not run is shown, with the reason. Dropping it would turn
a partial implementation into a clean sweep, which is the single easiest way to
publish a dishonest benchmark.

The summary is a geometric mean, not an arithmetic one. Speedups are ratios, and
an arithmetic mean of ratios is dominated by whichever query happened to go
best. A single hundred times win and nine ties average to eleven times under an
arithmetic mean and to one and a half under a geometric one, and the second
number is the one that describes the engine.

Usage:
    python tools/report.py results/2026-08-28-tpch-sf1-memory.json --out REPORT.md
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import operations
import queries as query_registry

ROOT = Path(__file__).resolve().parent.parent

# The engine every other engine is measured against, because it is the one most
# people are actually running.
BASELINE = "pandas"

# The engine this repository exists to measure.
SUBJECT = "firepanda"


def load_result(path: Path) -> dict:
    """Reads a result file.

    Args:
        path: The file.

    Returns:
        The parsed document.

    Raises:
        SystemExit: If the file is not a result file.
    """
    document = json.loads(path.read_text())
    if "results" not in document or "suite" not in document:
        raise SystemExit(f"{path} is not a result file")
    return document


def cell(entry: dict | None) -> str:
    """Formats one engine's time for the table.

    Args:
        entry: The result entry, or None if the pairing was never run.

    Returns:
        The cell text.
    """
    if entry is None:
        return "-"
    if not entry.get("ok"):
        return "n/a"
    seconds = entry["median_s"]
    if seconds < 0.001:
        return f"{seconds * 1e6:.0f} us"
    if seconds < 1:
        return f"{seconds * 1000:.1f} ms"
    return f"{seconds:.2f} s"


def tail_cell(entry: dict | None) -> str:
    """Formats one engine's ninety ninth percentile against its median.

    The ratio is the useful half. A p99 on its own says how slow the worst warm
    run was and says nothing about whether that is noise or a real tail, and the
    median is already in the table above.

    Args:
        entry: The result entry, or None.

    Returns:
        The cell text.
    """
    if entry is None or not entry.get("ok") or not entry.get("p99_s"):
        return "-"
    tail = entry["p99_s"]
    median = entry.get("median_s") or 0.0
    ratio = f" ({tail / median:.2f}x)" if median else ""
    if tail < 0.001:
        return f"{tail * 1e6:.0f} us{ratio}"
    if tail < 1:
        return f"{tail * 1000:.1f} ms{ratio}"
    return f"{tail:.2f} s{ratio}"


def cpu_cell(entry: dict | None) -> str:
    """Formats one engine's CPU seconds and how many cores it kept busy.

    Args:
        entry: The result entry, or None.

    Returns:
        The cell text.
    """
    if entry is None or not entry.get("ok"):
        return "-"
    cpu = float(entry.get("cpu_user_s", 0.0)) + float(entry.get("cpu_sys_s", 0.0))
    parallelism = float(entry.get("parallelism", 0.0))
    if cpu <= 0:
        return "-"
    return f"{cpu * 1000:.0f} ms ({parallelism:.1f}x)"


def cold_cell(entry: dict | None) -> str:
    """Formats the first run against the warm median.

    Args:
        entry: The result entry, or None.

    Returns:
        The cell text.
    """
    if entry is None or not entry.get("ok") or not entry.get("cold_s"):
        return "-"
    cold = entry["cold_s"]
    median = entry.get("median_s") or 0.0
    ratio = f" ({cold / median:.2f}x)" if median else ""
    if cold < 1:
        return f"{cold * 1000:.1f} ms{ratio}"
    return f"{cold:.2f} s{ratio}"


def rate_cell(entry: dict | None, size_bytes: int) -> str:
    """Formats a read as megabytes of file per second.

    Seconds are the wrong unit for comparing a reader across four files of
    different shapes: the wide file has a tenth of the rows of the narrow one and
    about the same number of bytes, and a table in seconds makes that look like a
    tenfold regression rather than the per field cost it is.

    Args:
        entry: The result entry, or None.
        size_bytes: How large the file being read is.

    Returns:
        The cell text.
    """
    if entry is None or not entry.get("ok") or not entry.get("median_s") or not size_bytes:
        return "-"
    return f"{size_bytes / entry['median_s'] / 1e6:,.0f}"


def file_bytes(document: dict, query) -> int:
    """Returns the size of the file an ingestion query reads.

    Args:
        document: The result document.
        query: The query.

    Returns:
        The byte count, or zero if the manifest does not have one.
    """
    if not query.needs:
        return 0
    entry = document.get("dataset", {}).get("files", {}).get(query.needs[0], {})
    return int(entry.get("csv", {}).get("bytes", 0))


def bytes_cell(entry: dict | None, baseline: dict | None = None) -> str:
    """Formats one engine's peak resident memory, against the baseline's.

    The bytes on their own are the honest raw number and they are also the one a
    reader cannot use, because whether 118 MB is good depends entirely on what the
    other engine did on the same query on the same machine. That is the thing the
    table already knows and used to not say, so working out whether firepanda used a
    third of what pandas used meant dividing two cells by hand.

    Above one means less memory used, which is the same direction as the speed
    ratios here and the same direction the compat cost matrix uses, so a reader does
    not have to work out which way round each table goes.

    Args:
        entry: The result entry, or None.
        baseline: The baseline engine's entry for the same query, or None when this
            cell is the baseline itself or the baseline did not run.

    Returns:
        The cell text.
    """
    if entry is None or not entry.get("ok") or not entry.get("peak_rss_bytes"):
        return "-"
    value = entry["peak_rss_bytes"]
    text = f"{value / (1 << 30):.2f} GB" if value >= 1 << 30 else f"{value / (1 << 20):.0f} MB"
    if baseline and baseline is not entry and baseline.get("ok") and baseline.get("peak_rss_bytes"):
        text += f" ({baseline['peak_rss_bytes'] / value:.2f}x)"
    return text


def per_query_table(document: dict, engines: list[str], suite: str, formatter) -> list[str]:
    """Renders one metric as a table with a row per query and a column per engine.

    Args:
        document: The result document.
        engines: The engines in the run.
        suite: Which suite is being reported.
        formatter: Takes the row's entries by engine and one engine name, and
            returns the cell. It gets the whole row rather than one entry because
            the memory cell is a ratio against the baseline in the same row.

    Returns:
        The markdown lines.
    """
    lines = [
        "| query | " + " | ".join(engines) + " |",
        "| --- | " + " | ".join("---:" for _ in engines) + " |",
    ]
    for query in query_registry.for_suite(suite):
        keys = {e: document["results"].get(f"{query.name}/{e}") for e in engines}
        if not any(keys.values()) or not comparable(document, query.name, engines):
            continue
        lines.append(f"| {query.name} | " + " | ".join(formatter(keys, e) for e in engines) + " |")
    return lines


def geometric_mean(values: list[float]) -> float:
    """Returns the geometric mean of a list of ratios.

    Args:
        values: The ratios, which must be positive.

    Returns:
        The mean, or zero if there is nothing to average.
    """
    usable = [v for v in values if v > 0]
    if not usable:
        return 0.0
    return math.exp(sum(math.log(v) for v in usable) / len(usable))


def comparable(document: dict, query: str, engines: list[str]) -> bool:
    """Says whether a query's results may be compared across engines.

    Args:
        document: The result document.
        query: The query name.
        engines: The engines in the run.

    Returns:
        Whether every engine that produced an answer produced the same one.
    """
    verdict = document.get("agreement", {}).get(query)
    return bool(verdict and verdict.get("agreed"))


def ratios(document: dict, engines: list[str], against: str, subject: str) -> dict:
    """Computes the per query and overall ratios between two engines.

    Args:
        document: The result document.
        engines: The engines in the run.
        against: The baseline engine name.
        subject: The engine being scored.

    Returns:
        A mapping with the per query speed and memory ratios and their geometric
        means, over the queries where both engines ran and every engine agreed.
    """
    speed = {}
    memory = {}
    for name in sorted({key.split("/")[0] for key in document["results"]}):
        if not comparable(document, name, engines):
            continue
        base = document["results"].get(f"{name}/{against}")
        mine = document["results"].get(f"{name}/{subject}")
        if not (base and mine and base.get("ok") and mine.get("ok")):
            continue
        if mine["median_s"] > 0:
            speed[name] = base["median_s"] / mine["median_s"]
        if mine.get("peak_rss_bytes"):
            memory[name] = base["peak_rss_bytes"] / mine["peak_rss_bytes"]
    return {
        "speed": speed,
        "memory": memory,
        "speed_geomean": geometric_mean(list(speed.values())),
        "memory_geomean": geometric_mean(list(memory.values())),
    }


def headline(document: dict, engines: list[str]) -> str:
    """The one line summary of a suite, as a pair rather than as a time.

    The claim this repository exists to check is ten times the speed on a tenth of
    the memory. Leading with the speed and putting the memory four tables down
    answers half of it and lets a reader assume the other half, which is the half
    that is harder.

    Args:
        document: The result document.
        engines: The engines in the run.

    Returns:
        The sentence, or an empty string when the subject did not run here.
    """
    if SUBJECT not in engines or BASELINE not in engines:
        return ""
    scored = ratios(document, engines, BASELINE, SUBJECT)
    if not scored["speed"]:
        return ""
    return (
        f"**{SUBJECT} against {BASELINE}: {scored['speed_geomean']:.2f}x on time and "
        f"{scored['memory_geomean']:.2f}x on peak memory**, geometric means over the "
        f"{len(scored['speed'])} queries both engines ran and every engine agreed on. "
        f"The goal is ten and ten, both numbers are published every run whatever they "
        f"say, and the per query tables below are where a mean this shape comes apart."
    )


# The full ClickBench size, and the only one a published number is comparable to.
# The other two exist because a suite that can only be run on a machine with a
# spare hundred gigabytes gets run four times a year.
CLICKBENCH_SIZE = "100M"


def clickbench_methodology(document: dict) -> list[str]:
    """The five ways our ClickBench numbers differ from the published table.

    Somebody is going to put a number from this report next to a number from
    ClickHouse's published table, and the difference between the two is partly the
    engine and partly this list. A reader who cannot see the list cannot do that
    comparison correctly, so it goes where the numbers are rather than in a commit
    message or a README nobody opened.

    Args:
        document: The result document.

    Returns:
        The markdown lines.
    """
    lines = ["### How this differs from published ClickBench", ""]
    lines.append(
        "These are not published ClickBench results and they are not submitted to "
        "the ClickBench table. They are the published 43 statements, run by this "
        "harness, under this harness's rules, and those rules differ from "
        "ClickBench's in five ways. Every one of the five is deliberate and none of "
        "them makes a number here better than it is."
    )
    lines.append("")
    runs = document["runs"]
    lines.append(
        f"- **The statistic.** ClickBench reports the minimum of three runs. This "
        f"reports the median of {runs} run{'' if runs == 1 else 's'} with the "
        f"interquartile range in the result file. A minimum is the cleanest run the "
        f"machine gave you and a median with its spread is a measurement. They are "
        f"not the same number."
    )
    lines.append(
        "- **The cache.** ClickBench runs each query three times and publishes cold, "
        "warm and hot separately. This runs warm. Cold cache behaviour is measured "
        "by the ingestion suite, with the page cache dropped between runs, because "
        "mixing it into a query suite makes every number in it partly a measurement "
        "of the filesystem."
    )
    lines.append(
        "- **Load time and data size are missing.** Both are real parts of the "
        "published table and this harness has never measured either, for any suite. "
        "The column is absent rather than empty, so nobody reads it as instant."
    )
    lines.append(
        "- **The machine.** The published table is one machine type, c6a.4xlarge. "
        "This is not that machine, and the machine that produced this file is named "
        "at the top of it. Only the ratio between two engines in the same table "
        "transfers between machines."
    )
    if document["size"] != CLICKBENCH_SIZE:
        lines.append(
            f"- **The size.** This file is {document['size']} and ClickBench is "
            f"{CLICKBENCH_SIZE}, the full hits table of 99,997,497 rows. A partial "
            f"size exists here for CI and for machines that cannot hold the whole "
            f"table, and a number taken on one is not comparable to a published one."
        )
    else:
        lines.append(
            f"- **The size.** This file is {CLICKBENCH_SIZE}, which is the full "
            f"table. The 1M and 10M sizes this harness also runs are not ClickBench "
            f"and are labelled where they appear."
        )
    lines.append("")
    lines.append(
        "One more difference is not about the harness at all. ClickHouse's published "
        "entry answers `COUNT(DISTINCT UserID)` with `uniq`, a HyperLogLog estimate. "
        "Every engine here counts exactly, on q3, q4, q7, q8, q9, q10, q12 and q22, "
        "and none of them reaches for the approximate spelling its library also "
        "offers. An exact count is more work than an estimate, so this makes our "
        "numbers on those eight worse rather than better, and it is the difference "
        "most likely to be misread."
    )
    lines.append("")
    lines.append(
        "Submitting a firepanda entry upstream is the right end state and it needs a "
        "firepanda that reads Parquet on its own and runs all 43. Not yet."
    )
    lines.append("")
    return lines


# Suites whose per query table is split into blocks rather than rendered whole.
# Forty three rows in one table is a wall, and this is the only suite with that
# many. The bands come from the query registry, because which queries belong
# together is a fact about the queries and not about the report.
BANDED = {"clickbench"}


def band_note(checks: bool) -> list[str]:
    """The paragraph above the banded tables, and the legend for the check column.

    The three agreement states are the point of it. A query here can agree, or
    disagree, or be one the statement does not determine an answer for, and the
    third one looks like the second to anybody who has not read the suite README.
    The first two say what happened to the answers. The third says the question
    did not have one answer, which is not a fault of any engine in the table.

    Args:
        checks: Whether the check column is in the tables.

    Returns:
        The markdown lines, ending in a blank one.
    """
    lines = [
        "The same measurement as any other suite, in blocks rather than in one "
        "table of 43 rows. The bands are ranges of ClickBench's own numbering cut "
        "where the workload changes, and a query is in exactly one of them.",
        "",
    ]
    if checks:
        lines.extend(
            [
                "The check column is which comparison is behind the row, and it has "
                "two values here because the third state is not in these tables at "
                "all. `values` means every engine returned the same answer, "
                "compared cell by cell. `shape only` means the statement does not "
                "determine which rows come back, so two engines returning different "
                "rows are both right and what was compared is the row count and the "
                "column set. A query the engines genuinely disagreed on is in its "
                "own section below and in none of the tables, because a different "
                "answer is not a faster or slower answer.",
                "",
            ]
        )
    return lines


def made_of(suite: str) -> list[str]:
    """The operations inside each query, and which of them the cost matrix measures.

    Every table above this one is a number per query, and a query is five or six
    operations wrapped into one. A reader who sees a row where firepanda loses wants
    to know which operation inside it lost, and that answer is in the compat cost
    matrix rather than here. This is the index into it.

    It is rendered once per suite at the end rather than inside each result section,
    because what a query is made of is a property of the query and not of the machine
    it ran on, and the same table ten times is a table nobody reads.

    Args:
        suite: Which suite is being reported.

    Returns:
        The markdown lines, or nothing when no query in the suite declares anything.
    """
    table = operations.matrix()
    rows: list[tuple[str, tuple[str, ...], list[str]]] = []
    for query in query_registry.for_suite(suite):
        names = operations.declared(suite, query.name)
        if names:
            rows.append((query.name, names, operations.uncovered(names, table)))
    if not rows:
        return []

    lines = ["", f"## What each {suite} query is made of", ""]
    lines.append(
        "This repository answers how fast on published workloads. "
        f"[firepanda-compat]({operations.MATRIX_URL}) answers how fast per operation, "
        "one row per pandas operation on a one million row corpus, which is the "
        "question a reader asks the moment a query in the tables above loses. These "
        "are the operations the pandas implementation of each query calls, so a slow "
        "query can be looked up there by the thing inside it rather than by its name."
    )
    lines.append("")
    lines.append("| query | operations | not in the matrix |")
    lines.append("| --- | --- | --- |")
    for name, names, gaps in rows:
        lines.append(
            f"| {name} | {', '.join(f'`{n}`' for n in names)} | "
            f"{', '.join(f'`{n}`' for n in gaps) if gaps else 'none'} |"
        )

    every = {gap for _, _, gaps in rows for gap in gaps}
    if every:
        count = f"{len(every)} operation{'' if len(every) == 1 else 's'}"
        lines.append("")
        lines.append(
            "The last column is a hole in the cost matrix with a query attached to "
            "it, and it is published rather than quietly dropped for the same reason "
            f"every other loss in this report is. {count} this suite runs "
            + ("has" if len(every) == 1 else "have")
            + " no row over there: "
            + ", ".join(f"`{name}`" for name in sorted(every))
            + "."
        )
        if every == {"pandas.read_csv"}:
            lines.append("")
            lines.append(
                "That one is not really a hole. Reading a CSV is what this whole "
                "suite measures, on five file shapes and against four engines, and "
                "the compat corpus is Arrow on disk rather than text. A row over "
                "there would be a worse version of the table above."
            )
    return lines


def render(document: dict, path: Path) -> str:
    """Renders one result file as markdown.

    Args:
        document: The result document.
        path: Where it came from, so the report can name its own source.

    Returns:
        The markdown.
    """
    engines = sorted(document.get("engines", {}))
    suite = document["suite"]
    machine = document.get("machine", {})
    lines: list[str] = []

    # The size goes in the heading rather than in a note under the table, because a
    # partial ClickBench size is a different workload from ClickBench and a reader
    # who scrolls to the numbers and no further has to see that.
    partial = suite == "clickbench" and document["size"] != CLICKBENCH_SIZE
    label = ", a partial size and not ClickBench" if partial else ""
    lines.append(f"## {suite} at {document['size']}{label}, io mode {document.get('io', 'memory')}")
    lines.append("")
    cores = machine.get("physical_cores") or machine.get("logical_cores") or "?"
    lines.append(
        f"{machine.get('cpu_model', machine.get('processor', 'unknown CPU'))}, "
        f"{cores} physical cores, "
        f"{machine.get('ram_bytes', 0) / (1 << 30):.0f} GB of memory. "
        f"{document['runs']} runs per query, the median reported and the "
        f"interquartile range in the result file."
    )
    lines.append("")
    versions = ", ".join(
        f"{name} {value or 'unknown'}" for name, value in sorted(document["engines"].items())
    )
    lines.append(f"Versions: {versions}.")
    lines.append("")

    lead = headline(document, engines)
    if lead:
        lines.append(lead)
        lines.append("")

    if suite == "ingestion":
        lines.append(
            "This suite measures the CSV reader itself, so the timed region is the "
            "read and nothing else. The frame each engine returns is its own, not "
            "an Arrow table: converting it would be a second and quite separate "
            "piece of work, it is not the same size for every engine, and on the "
            "wide file it cost Polars seventy times what the read did. The harness "
            "converts afterwards, outside the timing, to check the four engines "
            "read the same file."
        )
        lines.append("")
    elif document.get("io") == "scan":
        lines.append(
            "In scan mode Polars and DuckDB read the Parquet inside the timed "
            "region and push the projection into the file, so they never touch "
            "the columns the query does not name. pandas has no lazy scan and "
            "reads every column either way. A scan number and a memory number "
            "for the same query are not the same measurement."
        )
        lines.append("")

    if suite == "clickbench":
        lines.extend(clickbench_methodology(document))

    if partial:
        lines.append(
            f"Every row below is {document['size']} of the hits table. ClickBench is "
            f"the whole {CLICKBENCH_SIZE}, and these numbers are not comparable to a "
            f"published one."
        )
        lines.append("")

    # One table of 43 rows is a table nobody reads, and ClickBench is one flat
    # list of 43 with no groups of its own. The bands in the registry are ranges of
    # the published numbering cut where the workload changes, so each block here is
    # between three and twelve rows with a sentence saying what is in it.
    banded = suite in BANDED
    # The check column exists wherever a query in the suite can be legitimately
    # unanswerable, which today is ClickBench and nothing else. A reader cannot be
    # expected to hold twelve query names from a section further down in their head
    # while reading a timing, so the row says which check is behind it.
    checks = any(query.undetermined for query in query_registry.for_suite(suite))
    header = (
        "| query | what it does | " + ("check | " if checks else "") + " | ".join(engines) + " |"
    )
    rule = (
        "| --- | --- | " + ("--- | " if checks else "") + " | ".join("---:" for _ in engines) + " |"
    )

    disagreed: list[str] = []
    missing: list[tuple[str, str, str]] = []
    asides: list[tuple[str, str, str]] = []
    rows: dict[str, list[str]] = {}
    for query in query_registry.for_suite(suite):
        keys = {e: document["results"].get(f"{query.name}/{e}") for e in engines}
        if not any(keys.values()):
            continue
        if not comparable(document, query.name, engines):
            disagreed.append(query.name)
            continue
        for engine, entry in keys.items():
            if entry is None:
                continue
            if not entry.get("ok"):
                missing.append((query.name, engine, entry.get("note", "")))
            elif entry.get("note"):
                asides.append((query.name, engine, entry["note"]))
        row = " | ".join(cell(keys[e]) for e in engines)
        check = f"{'shape only' if query.undetermined else 'values'} | " if checks else ""
        band = query.group if banded else ""
        rows.setdefault(band, []).append(f"| {query.name} | {query.description} | {check}{row} |")

    if banded:
        lines.append("### Wall clock, by band")
        lines.append("")
        lines.extend(band_note(checks))
        for band, (_, blurb) in query_registry.CLICKBENCH_BANDS.items():
            if band not in rows:
                continue
            lines.append(f"#### {band}")
            lines.append("")
            lines.append(blurb)
            lines.append("")
            lines.extend([header, rule, *rows[band]])
            lines.append("")
    else:
        lines.extend([header, rule, *rows.get("", [])])

    if suite == "ingestion":
        lines.append("")
        lines.append(
            "The same warm runs as megabytes of file per second, which is the "
            "number that compares across the four files. They are deliberately "
            "different shapes and the wide one has a tenth of the rows, so seconds "
            "compare a reader against itself and bytes per second compare it "
            "against the file."
        )
        lines.append("")
        lines.append("| query | file | " + " | ".join(engines) + " |")
        lines.append("| --- | ---: | " + " | ".join("---:" for _ in engines) + " |")
        for query in query_registry.for_suite(suite):
            keys = {e: document["results"].get(f"{query.name}/{e}") for e in engines}
            if not any(keys.values()) or not comparable(document, query.name, engines):
                continue
            size = file_bytes(document, query)
            row = " | ".join(rate_cell(keys[e], size) for e in engines)
            lines.append(f"| {query.name} | {size / 1e6:,.0f} MB | {row} |")

        lines.append("")
        lines.append(
            "The first run, taken after the file was dropped from the page cache, "
            "and what it came to as a multiple of the warm median. This is the "
            "number a reader gets on a file they have not touched before, which is "
            "the usual case for a file being read once. That the eviction took is "
            "checked rather than assumed: the block reads on the cold sample in "
            "the result file come to the size of the file, and a machine where the "
            "drop was refused says so against every measurement below."
        )
        lines.append("")
        lines.append("| query | " + " | ".join(engines) + " |")
        lines.append("| --- | " + " | ".join("---:" for _ in engines) + " |")
        for query in query_registry.for_suite(suite):
            keys = {e: document["results"].get(f"{query.name}/{e}") for e in engines}
            if not any(keys.values()) or not comparable(document, query.name, engines):
                continue
            row = " | ".join(cold_cell(keys[e]) for e in engines)
            lines.append(f"| {query.name} | {row} |")

    lines.append("")
    if banded:
        lines.append("### Peak memory")
        lines.append("")
    lines.append(
        f"Peak resident memory, which is the whole process and includes the data, "
        f"and what that is as a multiple of {BASELINE}. Above one is less memory "
        f"used. This is half of the stated goal rather than a tiebreak between "
        f"engines that are close on time: a user running a five gigabyte frame on a "
        f"sixteen gigabyte laptop cares about this number first, because that "
        f"failure mode ends with a process that was killed rather than with a query "
        f"that took longer."
    )
    lines.append("")
    lines.extend(
        per_query_table(
            document, engines, suite, lambda keys, e: bytes_cell(keys[e], keys.get(BASELINE))
        )
    )

    lines.append("")
    if banded:
        lines.append("### The tail")
        lines.append("")
    lines.append(
        "The ninety ninth percentile of the warm runs, and what it is as a "
        "multiple of the median. A number close to one is a query that costs the "
        "same every time it is asked, and that is worth as much as the median to "
        "anyone who has to run it behind something."
    )
    lines.append("")
    lines.extend(per_query_table(document, engines, suite, lambda keys, e: tail_cell(keys[e])))

    lines.append("")
    if banded:
        lines.append("### CPU")
        lines.append("")
    lines.append(
        "CPU seconds per run, user and system together, and how many cores that "
        "came to while the query was in flight. An engine that is four times "
        "faster on sixteen cores and one that is four times faster on one are "
        "not the same result, and the wall clock table cannot tell them apart."
    )
    lines.append("")
    lines.extend(per_query_table(document, engines, suite, lambda keys, e: cpu_cell(keys[e])))

    lines.append("")
    lines.append("### Scorecard")
    lines.append("")
    lines.append(
        "Geometric means over the queries both engines ran and every engine "
        "agreed on. Above one means the first engine is ahead."
    )
    lines.append("")
    lines.append("| against | queries compared | speed | peak memory |")
    lines.append("| --- | ---: | ---: | ---: |")
    for other in engines:
        if other == SUBJECT or SUBJECT not in engines:
            continue
        scored = ratios(document, engines, other, SUBJECT)
        if not scored["speed"]:
            continue
        lines.append(
            f"| {SUBJECT} vs {other} | {len(scored['speed'])} | "
            f"{scored['speed_geomean']:.2f}x | {scored['memory_geomean']:.2f}x |"
        )
    for other in engines:
        if other in (BASELINE, SUBJECT):
            continue
        scored = ratios(document, engines, BASELINE, other)
        if not scored["speed"]:
            continue
        lines.append(
            f"| {other} vs {BASELINE} | {len(scored['speed'])} | "
            f"{scored['speed_geomean']:.2f}x | {scored['memory_geomean']:.2f}x |"
        )

    if missing:
        lines.append("")
        lines.append("### What did not run, and why")
        lines.append("")
        lines.append("| query | engine | reason |")
        lines.append("| --- | --- | --- |")
        for query, engine, note in missing:
            lines.append(f"| {query} | {engine} | {note[:180]} |")

    if asides:
        lines.append("")
        lines.append("### What the numbers above were taken under")
        lines.append("")
        lines.append(
            "A measurement that needed a different configuration to happen at all "
            "is still a measurement, but it is not the same one as its neighbours "
            "in the row, so what changed is written down here rather than left in "
            "the result file for nobody to find."
        )
        lines.append("")
        # Grouped by the note rather than listed per measurement, because the one
        # note every ingestion result carries is the page cache one and fifteen
        # copies of it would bury the note that is actually about one engine.
        grouped: dict[str, list[str]] = {}
        for query, engine, note in asides:
            grouped.setdefault(note, []).append(f"{query}/{engine}")
        for note, where in grouped.items():
            if len(where) == len(asides):
                lines.append(f"- every measurement above: {note}")
            else:
                lines.append(f"- {', '.join(where)}: {note}")

    weak = [
        (query.name, query.undetermined)
        for query in query_registry.for_suite(suite)
        if query.undetermined and document["results"].get(f"{query.name}/{engines[0]}")
    ]
    if weak:
        lines.append("")
        lines.append("### Rows where the check behind the number is weaker")
        lines.append("")
        lines.append(
            "These queries do not determine which rows come back, so two engines "
            "returning different rows are both right and the answers are compared "
            "on their row count and their columns rather than on their values. "
            "They are the rows marked `shape only` above. The timings are as good "
            "as any other row in the table. The agreement behind them is not, and "
            "a reader should know which rows those are rather than assume the "
            "whole suite is checked the same way."
        )
        lines.append("")
        for name, reason in weak:
            lines.append(f"- {name}: {reason}")
        # Which rows tie at row ten is a function of how many rows there are, so
        # this list belongs to a size and this file may not be that size. Carrying
        # it over is the only thing to do with it and it is not the same as having
        # checked it, and a reader looking at a 10M table should not be told a
        # number computed at 1M as though it were about the file in front of them.
        computed_at = query_registry.CLICKBENCH_UNDETERMINED_SIZE
        if suite == "clickbench" and document["size"] != computed_at:
            lines.append("")
            lines.append(
                f"That list was computed at {computed_at} and this file is "
                f"{document['size']}. Which rows tie at the row a limit cuts on "
                f"depends on how many rows there are, so at this size the list is "
                f"carried over rather than checked: a query that ties here and not "
                f"at {computed_at} shows up as a disagreement below rather than "
                f"as a weaker check here. "
                f"`pixi run validate-clickbench --size {document['size']}` is what "
                f"computes the list for this size."
            )

    if disagreed:
        lines.append("")
        lines.append("### Queries the engines did not agree on")
        lines.append("")
        lines.append(
            "These are excluded from every table above. A different answer is "
            "not a slower or faster answer, it is a different query, and until "
            "the disagreement is explained neither timing means anything. Each "
            "line is the row count and the answer digest per engine."
        )
        lines.append("")
        for query in disagreed:
            seen = document["agreement"][query]["by_engine"]
            reason = query_registry.undetermined(suite, query)
            # A query compared on its shape alone that still disagreed is the worse
            # of the two cases and reads like the milder one, since its name is also
            # in the weaker section above. The values were never compared here, so
            # what differed is the row count or the column set, and neither of those
            # is something the statement left open.
            tail = (
                " The values on this one are not compared at all, since "
                f"{reason}, so this is a difference in the row count or the columns."
                if reason
                else ""
            )
            lines.append(
                f"- {query}: " + ", ".join(f"{e} {v}" for e, v in sorted(seen.items())) + tail
            )

    lines.append("")
    lines.append(f"Source: `{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}`.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Renders one or more result files.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="*", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    paths = args.results or sorted((ROOT / "results").glob("*.json"))
    if not paths:
        raise SystemExit("no result files. Run: pixi run bench")

    sections = ["# Results", ""]
    sections.append(
        "Every table here comes from one invocation on one machine. The harness "
        "will not merge runs from different machines, because a normalized cross "
        "machine comparison is a model and this file is a measurement."
    )
    sections.append("")
    documents = [(path, load_result(path)) for path in paths]
    for path, document in documents:
        sections.append(render(document, path))

    # Once per suite, after the results, because what a query is made of does not
    # change with the machine it ran on and the same table under every result section
    # is a table nobody reads.
    for suite in dict.fromkeys(document["suite"] for _, document in documents):
        sections.append("\n".join(made_of(suite)))

    text = "\n".join(sections)
    if args.out:
        args.out.write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
