#!/usr/bin/env python3
"""Refuses to let a clean sweep go out without somebody having looked at it.

A benchmark written by the authors of one of the engines in it, in which that
engine wins every row, is the shape of every dishonest comparison ever
published. Sometimes it is also the shape of a genuinely better engine. The
difference is not visible from inside the harness, so this does not try to judge
it. It looks for the pattern and stops, and a human decides.

What counts as suspicious here:

Winning every comparable query. Real engines lose somewhere. An engine that has
never lost a single query has usually not implemented the ones it would lose.

Winning while running a small fraction of the suite. Eight of fifteen queries
with the other seven marked unsupported is a real result about eight queries and
not a result about the suite, and a scorecard that averages over only the eight
says something the reader will not hear.

A margin nobody should believe. Two hundred times faster than DuckDB on a group
by is not an optimization, it is a query that read less data, and the answer
digest agreeing does not rule that out when both engines were asked for a single
row.

A competitor that crashed. A pairing that failed is recorded with the reason
rather than dropped, which is right, but "DuckDB raised a parser error" and
"firepanda has no Parquet reader" read the same in a table and only one of them
is a fact about the engine. Every join in this suite failed on DuckDB for a week
because the tables are called `left` and `right_small` and LEFT is a reserved
word, and the report said so on five rows that nobody read. A crash is now loud.

Usage:
    python tools/check_sweep.py results/2026-08-28-db-benchmark-0.5GB-memory.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import queries as query_registry

SUBJECT = "firepanda"

# A speedup past this is reported for a human to explain. It is set well above
# anything a better memory layout and a wider vector explain on their own.
IMPLAUSIBLE = 50.0

# Running less than this fraction of a suite makes a scorecard a statement about
# the queries that ran rather than about the suite.
MINIMUM_COVERAGE = 0.6


def exception_line(note: str) -> str:
    """Pulls the exception out of a traceback the worker flattened onto one line.

    The worker joins a traceback with pipes so it fits in a JSON string, and the
    last segment is usually the caret that points at the syntax error rather than
    the message. This looks for the segment that names an exception.

    Args:
        note: The flattened traceback.

    Returns:
        The exception and its message, or the whole note if none is recognisable.
    """
    segments = [s.strip() for s in note.split("|") if s.strip()]
    for segment in reversed(segments):
        # The qualified name, because DuckDB's is `_duckdb.ParserException` and a
        # leading underscore is not a reason to skip it.
        head = segment.split(":", 1)[0].split(".")[-1]
        if head.endswith("Error") or head.endswith("Exception"):
            return segment
    return segments[-1] if segments else note.strip()


def crashes(document: dict) -> list[str]:
    """Returns one line per engine that raised, for any engine in the file.

    Separate from the rest of the review because it is the only part that is not a
    judgement call. Winning every query might be true; a competitor raising a
    parser error is a broken harness every time, and CI treats the two
    differently.

    Args:
        document: The result document.

    Returns:
        The crashes, empty if there are none.
    """
    suite = document["suite"]
    found: dict[str, list[str]] = {}
    for key, entry in document.get("results", {}).items():
        if entry.get("ok"):
            continue
        if "Traceback (most recent call last)" not in entry.get("note", ""):
            continue
        query, engine = key.split("/")
        found.setdefault(engine, []).append(query)

    lines = []
    for engine, failed in sorted(found.items()):
        detail = exception_line(document["results"][f"{failed[0]}/{engine}"]["note"])
        lines.append(
            f"{engine} raised on {len(failed)} {suite} queries "
            f"({', '.join(sorted(failed))}): {detail[:160]}. That is a harness "
            "bug until proven otherwise, and those rows are missing from the "
            "comparison rather than lost by the engine."
        )
    return lines


def review(document: dict) -> list[str]:
    """Returns everything about a result file that a human should look at.

    Args:
        document: The result document.

    Returns:
        The concerns, empty if there are none.
    """
    concerns: list[str] = []
    suite = document["suite"]
    engines = sorted(document.get("engines", {}))

    # Crashes first, and for every engine rather than only ours. A competing
    # engine that did not run is a hole in the comparison whoever it belongs to,
    # and a harness bug that silently removes a competitor from half the table is
    # the most expensive kind to publish.
    concerns.extend(crashes(document))

    if SUBJECT not in engines:
        return concerns

    total = len(query_registry.for_suite(suite))
    ran = 0
    wins = 0
    compared = 0
    for query in query_registry.for_suite(suite):
        mine = document["results"].get(f"{query.name}/{SUBJECT}")
        if not (mine and mine.get("ok")):
            continue
        ran += 1
        if not document.get("agreement", {}).get(query.name, {}).get("agreed", False):
            continue
        for other in engines:
            if other == SUBJECT:
                continue
            theirs = document["results"].get(f"{query.name}/{other}")
            if not (theirs and theirs.get("ok")) or mine["median_s"] <= 0:
                continue
            compared += 1
            speedup = theirs["median_s"] / mine["median_s"]
            if speedup > 1:
                wins += 1
            if speedup > IMPLAUSIBLE:
                concerns.append(
                    f"{query.name}: {SUBJECT} is {speedup:.0f}x faster than {other}, "
                    "which is past what a layout and a vector width explain. Check "
                    "that both engines materialized the same answer."
                )

    if ran and total and ran / total < MINIMUM_COVERAGE:
        concerns.append(
            f"{SUBJECT} ran {ran} of {total} {suite} queries. A scorecard over "
            "that subset is a claim about those queries and the report has to "
            "say so where the number appears."
        )
    if compared and wins == compared:
        concerns.append(
            f"{SUBJECT} won all {compared} comparisons in this file. That is the "
            "shape of a benchmark written by the authors of the winning engine, "
            "whether or not it is true here, so it wants a sentence in the report "
            "explaining what the engine is not doing that the others are."
        )
    return concerns


def main(argv: list[str] | None = None) -> int:
    """Reviews result files from the command line.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        Zero when nothing needs a human, one otherwise. One is not a failure, it
        is a request.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="*", type=Path)
    parser.add_argument(
        "--crashes-only",
        action="store_true",
        help="report only engines that raised, which is the part that is never a "
        "judgement call and is therefore the part CI can fail on",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent / "results"
    paths = args.results or sorted(root.glob("*.json"))
    if not paths:
        raise SystemExit("no result files to review")

    flagged = 0
    for path in paths:
        document = json.loads(path.read_text())
        if "results" not in document:
            continue
        concerns = crashes(document) if args.crashes_only else review(document)
        if not concerns:
            print(f"{path.name}: nothing to flag")
            continue
        flagged += len(concerns)
        print(f"\n{path.name}")
        for concern in concerns:
            print(f"  - {concern}")

    if flagged:
        noun = "thing" if flagged == 1 else "things"
        if args.crashes_only:
            print(f"\n{flagged} {noun} crashed. This file is not a comparison.")
        else:
            print(f"\n{flagged} {noun} for a human to look at before this is published")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
