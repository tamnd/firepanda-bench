#!/usr/bin/env python3
"""Checks each engine's TPC-H answers against the specification's published output.

DuckDB runs the official query text, so its answers are the specification's by
construction. The pandas and Polars versions are not: somebody had to read
twenty two SQL statements and write them again against a dataframe API, and that
somebody makes mistakes. A subquery turned into a join that drops rows, a filter
applied after an aggregate instead of before it, a left join written as an inner
one, all of these produce a plausible table quickly, and a benchmark that
publishes a fast wrong answer is worse than no benchmark.

So every query is checked against `tpch_answers()`, which is the validation output
the TPC publishes alongside the specification. A query that does not reproduce it
does not go in the results table, and this script is what the CI runs to decide.

The comparison is not string equality. The answer text carries a fixed number of
decimal places and a dataframe engine will not always produce the same ones, so
numbers are compared to a relative tolerance and text is compared after stripping
the padding the answer format uses. Row order is compared as given, because every
TPC-H query that does not have an ORDER BY has exactly one row.

Usage:
    python tools/validate_tpch.py --engines pandas,polars,duckdb --size sf1
"""

from __future__ import annotations

import argparse
import math
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import queries as query_registry
import tpch

import engines as engine_registry

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "data"

# How far a number may be from the published one. The answers carry two decimal
# places on money and up to six on ratios, and a sum of six million line items
# computed in a different order lands a few units of the last place away. A part
# in a million is far tighter than any real error and far looser than that noise.
TOLERANCE = 1e-6


def parse_answer(text: str) -> tuple[list[str], list[list[str]]]:
    """Reads the pipe separated answer text the extension stores.

    Args:
        text: The answer as stored.

    Returns:
        The header and the rows, each cell stripped.

    Raises:
        ValueError: If the text has no header.
    """
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise ValueError("the answer is empty")
    header = [cell.strip() for cell in lines[0].split("|")]
    rows = [[cell.strip() for cell in line.split("|")] for line in lines[1:]]
    return header, rows


def cell_matches(produced, expected: str) -> bool:
    """Compares one cell against the published one.

    Args:
        produced: What the engine returned, of whatever type it returned it as.
        expected: The published cell, as text.

    Returns:
        Whether they agree.
    """
    if produced is None:
        return expected in ("", "NULL", "None")

    # A number if the published cell reads as one, whatever the engine's type is,
    # because an engine returning a decimal and one returning a float are not in
    # disagreement about the answer.
    try:
        wanted = float(expected)
    except ValueError:
        wanted = None

    if wanted is not None:
        try:
            got = float(produced)
        except (TypeError, ValueError):
            return str(produced).strip() == expected
        if math.isnan(got) or math.isnan(wanted):
            return math.isnan(got) and math.isnan(wanted)
        scale = max(abs(got), abs(wanted), 1.0)
        return abs(got - wanted) <= TOLERANCE * scale

    got_text = str(produced).strip()
    if got_text == expected:
        return True
    # Dates come back as timestamps in a dataframe engine and as dates in the
    # published answer, and a midnight timestamp is that date.
    return got_text.replace(" 00:00:00", "") == expected


def compare(table, expected_text: str) -> str:
    """Compares an engine's answer against the published one.

    Args:
        table: The answer as an Arrow table.
        expected_text: The published answer.

    Returns:
        An empty string if they agree, otherwise the first disagreement.
    """
    header, rows = parse_answer(expected_text)
    if table.num_rows != len(rows):
        return f"{table.num_rows} rows, the specification has {len(rows)}"
    if table.num_columns != len(header):
        return (
            f"{table.num_columns} columns ({', '.join(table.column_names)}), "
            f"the specification has {len(header)} ({', '.join(header)})"
        )
    # Names, not just how many. The cross engine fingerprint sums columns by
    # name, so an engine that answers correctly under a name of its own choosing
    # gets reported as disagreeing with the engines that used the specification's
    # name, and the disagreement points at the query rather than at the naming.
    #
    # A name matches if it is the published one or begins with it. The stored
    # answers truncate a name at times: Q18's select list has no alias on
    # `sum(l_quantity)` and the answer file calls that column `sum`, so DuckDB
    # running the specification's own text disagrees with the specification's own
    # answer header. The select list is the better authority, and this accepts
    # both rather than pretending the shorter one is wrong.
    mismatched = [
        f"{produced} for {expected}"
        for produced, expected in zip(table.column_names, header, strict=True)
        if produced != expected and not produced.startswith(expected)
    ]
    if mismatched:
        return "columns named " + ", ".join(mismatched)

    columns = [table.column(i).to_pylist() for i in range(table.num_columns)]
    for row_index, expected_row in enumerate(rows):
        for column_index, expected_cell in enumerate(expected_row):
            produced = columns[column_index][row_index]
            if not cell_matches(produced, expected_cell):
                return (
                    f"row {row_index + 1} column {header[column_index]}: "
                    f"got {produced!r}, the specification has {expected_cell!r}"
                )
    return ""


def check_engine(name: str, size: str, only: list[str]) -> dict[str, str]:
    """Runs every TPC-H query on one engine and compares each answer.

    Args:
        name: The engine name.
        size: The scale factor name.
        only: Which queries to check, or empty for all of them.

    Returns:
        A mapping from query name to an empty string when it agreed and the
        reason when it did not.
    """
    module = engine_registry.load_engine(name)
    runners = engine_registry.query_map(module, "tpch")
    answers = tpch.official_answers(tpch.SCALES[size])
    root = DATA_ROOT / "tpch" / size
    paths = {table: str(root / f"{table}.parquet") for table in tpch.TABLES}
    context = module.load(paths, suite="tpch", io="memory")

    verdicts = {}
    for query in query_registry.for_suite("tpch"):
        if only and query.name not in only:
            continue
        if query.name not in answers:
            verdicts[query.name] = "no published answer at this scale factor"
            continue
        runner = runners.get(query.name)
        if runner is None:
            verdicts[query.name] = f"{name} does not implement it"
            continue
        try:
            table = engine_registry.as_arrow(runner(context))
            verdicts[query.name] = compare(table, answers[query.name])
        except Exception:
            verdicts[query.name] = traceback.format_exc(limit=3).strip().replace("\n", " | ")
    return verdicts


def main(argv: list[str] | None = None) -> int:
    """Validates the engines from the command line.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        Zero if every implemented query reproduced the published answer.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engines", default="pandas,polars,duckdb")
    parser.add_argument("--size", default="sf1")
    parser.add_argument("--queries", default="")
    args = parser.parse_args(argv)

    if args.size not in tpch.SCALES:
        raise SystemExit(f"unknown size '{args.size}'. Known: {', '.join(tpch.SCALES)}")
    root = DATA_ROOT / "tpch" / args.size
    if not (root / "lineitem.parquet").exists():
        raise SystemExit(
            f"no TPC-H data at {root}. Run: pixi run data --suite tpch --size {args.size}"
        )

    only = [q.strip() for q in args.queries.split(",") if q.strip()]
    failures = 0
    for name in [e.strip() for e in args.engines.split(",")]:
        print(f"\n{name} against the TPC-H validation output at {args.size}")
        verdicts = check_engine(name, args.size, only)
        for query, verdict in sorted(verdicts.items(), key=lambda kv: int(kv[0][1:])):
            if not verdict:
                print(f"  {query:<4} ok")
            elif verdict.startswith("no published answer") or "does not implement" in verdict:
                print(f"  {query:<4} skipped: {verdict}")
            else:
                print(f"  {query:<4} WRONG: {verdict[:150]}")
                failures += 1

    if failures:
        print(f"\n{failures} queries did not reproduce the published answer", file=sys.stderr)
        return 1
    print("\nevery implemented query reproduced the published answer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
