#!/usr/bin/env python3
"""Checks an external engine's TPC-H answers, handed over as CSV files.

`tools/validate_tpch.py` calls the engine in process, which works for the three
Python engines and cannot work for firepanda: its driver is a compiled binary
and there is nothing to import. So the driver writes each answer to a CSV and
this reads them back and runs the same comparison against `tpch_answers()`, the
validation output the TPC publishes alongside the specification.

The comparison is the one in `validate_tpch.py` and is imported from there
rather than written again, because two checkers that disagree about what counts
as a match are worse than one.

Usage:
    python tools/validate_tpch_csv.py --dir /path/to/answers --size sf1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pyarrow.csv as pacsv
import tpch
from validate_tpch import compare


def main(argv: list[str] | None = None) -> int:
    """Validates a directory of answer CSVs from the command line.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        Zero if every answer present reproduced the published one.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--size", default="sf1")
    parser.add_argument("--queries", default="")
    args = parser.parse_args(argv)

    if args.size not in tpch.SCALES:
        raise SystemExit(f"unknown size '{args.size}'. Known: {', '.join(tpch.SCALES)}")
    answers = tpch.official_answers(tpch.SCALES[args.size])
    if not answers:
        raise SystemExit(f"no published answers at {args.size}")

    only = [q.strip() for q in args.queries.split(",") if q.strip()]
    root = Path(args.dir)
    failures = 0
    for number in range(1, 23):
        query = f"q{number}"
        if only and query not in only:
            continue
        path = root / f"{query}.csv"
        if not path.exists():
            print(f"  {query:<4} skipped: no answer file")
            continue
        try:
            table = pacsv.read_csv(path)
            verdict = compare(table, answers[query])
        except Exception as error:
            verdict = f"{type(error).__name__}: {error}"
        if not verdict:
            print(f"  {query:<4} ok")
        else:
            print(f"  {query:<4} {verdict}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
