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

    lines.append(f"## {suite} at {document['size']}, io mode {document.get('io', 'memory')}")
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

    header = "| query | what it does | " + " | ".join(engines) + " |"
    lines.append(header)
    lines.append("| --- | --- | " + " | ".join("---:" for _ in engines) + " |")

    disagreed: list[str] = []
    missing: list[tuple[str, str, str]] = []
    asides: list[tuple[str, str, str]] = []
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
        lines.append(f"| {query.name} | {query.description} | {row} |")

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
    lines.append("| query | " + " | ".join(engines) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in engines) + " |")
    for query in query_registry.for_suite(suite):
        keys = {e: document["results"].get(f"{query.name}/{e}") for e in engines}
        if not any(keys.values()) or not comparable(document, query.name, engines):
            continue
        row = " | ".join(bytes_cell(keys[e], keys.get(BASELINE)) for e in engines)
        lines.append(f"| {query.name} | {row} |")

    lines.append("")
    lines.append(
        "The ninety ninth percentile of the warm runs, and what it is as a "
        "multiple of the median. A number close to one is a query that costs the "
        "same every time it is asked, and that is worth as much as the median to "
        "anyone who has to run it behind something."
    )
    lines.append("")
    lines.append("| query | " + " | ".join(engines) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in engines) + " |")
    for query in query_registry.for_suite(suite):
        keys = {e: document["results"].get(f"{query.name}/{e}") for e in engines}
        if not any(keys.values()) or not comparable(document, query.name, engines):
            continue
        row = " | ".join(tail_cell(keys[e]) for e in engines)
        lines.append(f"| {query.name} | {row} |")

    lines.append("")
    lines.append(
        "CPU seconds per run, user and system together, and how many cores that "
        "came to while the query was in flight. An engine that is four times "
        "faster on sixteen cores and one that is four times faster on one are "
        "not the same result, and the wall clock table cannot tell them apart."
    )
    lines.append("")
    lines.append("| query | " + " | ".join(engines) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in engines) + " |")
    for query in query_registry.for_suite(suite):
        keys = {e: document["results"].get(f"{query.name}/{e}") for e in engines}
        if not any(keys.values()) or not comparable(document, query.name, engines):
            continue
        row = " | ".join(cpu_cell(keys[e]) for e in engines)
        lines.append(f"| {query.name} | {row} |")

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

    if disagreed:
        lines.append("")
        lines.append("### Queries the engines did not agree on")
        lines.append("")
        lines.append(
            "These are excluded from every table above. A different answer is "
            "not a slower or faster answer, it is a different query, and until "
            "the disagreement is explained neither timing means anything."
        )
        lines.append("")
        for query in disagreed:
            seen = document["agreement"][query]["by_engine"]
            lines.append(f"- {query}: " + ", ".join(f"{e} {v}" for e, v in sorted(seen.items())))

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
    for path in paths:
        sections.append(render(load_result(path), path))

    text = "\n".join(sections)
    if args.out:
        args.out.write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
