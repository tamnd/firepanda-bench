#!/usr/bin/env python3
"""Measures what a group by costs in memory when its output is nearly its input.

q32 groups the hits table by `WatchID` and `ClientIP`, which is close to unique
per row, so the answer is within a small factor of the table it was computed
from. That is the one shape in the suite where a group by is an allocation
question rather than a throughput question, and a wall clock number says nothing
about it: an engine that spills is slow and an engine that doubles is fast right
up to the point where it is killed.

What is reported is the peak resident set after the load and the peak after the
query, and the difference between them. The difference is the honest number
because a peak is a high water mark for the whole process, so an engine whose
load costs three gigabytes reports at least three gigabytes for every query it
ever runs, and comparing the raw peaks would mostly compare the loaders.
`tools/clickbench_load.py` is where the loaders are compared on purpose.

One process per engine, for the reason `worker.py` gives: a peak never falls, so
an engine measured after another has allocated and freed eight gigabytes reports
a peak it never reached.

The query runs once rather than the seven or ten times a timed run uses. A peak
does not average and the second run cannot exceed the first by anything except
fragmentation, so repeating it costs minutes and answers nothing.

Usage:
    python tools/clickbench_group_memory.py --size 1M
    python tools/clickbench_group_memory.py --size 10M --query q31
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import clickbench  # noqa: E402
from clickbench_load import ENGINES, gigabytes, pattern_for, peak_rss_bytes  # noqa: E402

import engines  # noqa: E402

DEFAULT_QUERY = "q32"
"""The group by with no filter on it, so every row of the table reaches the key
and the group count is as close to the row count as this suite gets. q31 is the
same query behind a `SearchPhrase` filter, which cuts the rows first and is worth
measuring as the smaller case rather than instead of this one."""


def measure(engine: str, query: str, pattern: str) -> dict:
    """Loads the table and runs one query in this process, reading the peak twice.

    Args:
        engine: Which engine.
        query: Which clickbench query.
        pattern: The partition glob.

    Returns:
        The measurement, as the parent prints it.

    Raises:
        SystemExit: If the engine does not implement the query.
    """
    module = engines.load_engine(engine)
    queries = engines.query_map(module, "clickbench")
    if query not in queries:
        raise SystemExit(f"{engine} does not implement {query}")

    baseline = peak_rss_bytes()
    load_started = time.perf_counter()
    tables = module.load({"hits": pattern}, "clickbench", "memory")
    load_took = time.perf_counter() - load_started
    loaded = peak_rss_bytes()

    started = time.perf_counter()
    answer = queries[query](tables)
    took = time.perf_counter() - started
    peak = peak_rss_bytes()

    # Read after the peak, because the digest allocates and what it allocates is
    # not the group by. Ten rows, so it allocates almost nothing, but the reason
    # to read it at all is that an engine which never materialized its answer has
    # not run the query and would otherwise report a peak of nothing.
    rows, _, _, _, _ = engines.digest(answer)
    return {
        "engine": engine,
        "query": query,
        "load_s": load_took,
        "query_s": took,
        "rows_out": rows,
        "baseline_rss_bytes": baseline,
        "peak_after_load_bytes": loaded,
        "peak_rss_bytes": peak,
        "query_rss_bytes": peak - loaded,
    }


def _read_driver(query: str, pattern: str) -> dict:
    """Builds the firepanda driver, runs one query and returns what it printed.

    Args:
        query: Which clickbench query.
        pattern: The partition glob.

    Returns:
        The driver's JSON line, parsed.

    Raises:
        SystemExit: If the driver cannot be built or refuses the query.
    """
    from engines import firepanda_engine

    binary = firepanda_engine.build()
    finished = subprocess.run(
        [
            str(binary),
            "--suite=clickbench",
            f"--query={query}",
            f"--path={pattern}",
            "--runs=1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        raise SystemExit(f"the firepanda driver exited {finished.returncode}: {finished.stderr}")
    answer = json.loads(finished.stdout.strip().splitlines()[-1])
    if not answer.get("ok"):
        raise SystemExit(f"the firepanda driver refused {query}: {answer.get('note')}")
    return answer


def measure_firepanda(query: str, pattern: str) -> dict:
    """Asks the firepanda driver for the same two readings.

    The driver already takes them, one after the load and one at the end of the
    timed region, so this asks for a single run and reads the two fields back
    rather than teaching it a mode of its own.

    One run rather than the default ten, because what is wanted is the peak and a
    peak does not average.

    Args:
        query: Which clickbench query.
        pattern: The partition glob.

    Returns:
        The measurement, in the same shape the Python engines report.

    Raises:
        SystemExit: If the driver cannot be built or refuses the query.
    """
    answer = _read_driver(query, pattern)
    loaded = answer["peak_rss_after_load_bytes"]
    peak = answer["peak_rss_bytes"]
    return {
        "engine": "firepanda",
        "query": query,
        "load_s": answer["load_s"],
        "query_s": answer["runs"][0]["wall_s"],
        "rows_out": answer["rows_out"],
        "baseline_rss_bytes": 0,
        "peak_after_load_bytes": loaded,
        "peak_rss_bytes": peak,
        "query_rss_bytes": peak - loaded,
    }


def run_child(engine: str, query: str, size: str) -> dict:
    """Runs one engine's measurement in a process of its own.

    Args:
        engine: Which engine.
        query: Which clickbench query.
        size: Which downloaded size.

    Returns:
        The measurement, or a record saying why there is not one.
    """
    if engine == "firepanda":
        try:
            return measure_firepanda(query, pattern_for(size))
        except SystemExit as reason:
            return {"engine": engine, "note": str(reason)}
    finished = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--measure",
            "--engine",
            engine,
            "--query",
            query,
            "--size",
            size,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        tail = finished.stderr.strip().splitlines()
        return {"engine": engine, "note": tail[-1] if tail else "failed"}
    return json.loads(finished.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    """Measures every engine and prints a table.

    Args:
        argv: The arguments, or None for the command line.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", default="1M", choices=sorted(clickbench.SIZES))
    parser.add_argument("--query", default=DEFAULT_QUERY, help="which clickbench query")
    parser.add_argument("--engine", default=None, help="one engine rather than all four")
    parser.add_argument("--measure", action="store_true", help="the child mode, not for people")
    parser.add_argument("--json", default=None, help="also write the measurements here")
    args = parser.parse_args(argv)

    if args.measure:
        print(json.dumps(measure(args.engine, args.query, pattern_for(args.size))))
        return 0

    wanted = [args.engine] if args.engine else list(ENGINES)
    measured = [run_child(engine, args.query, args.size) for engine in wanted]

    print(f"{args.query} on the hits table at {args.size}, one run, peaks read twice")
    print(f"{'engine':<12}{'query':>9}{'rows out':>12}{'after load':>13}{'peak':>10}{'added':>10}")
    for record in measured:
        if "note" in record:
            print(f"{record['engine']:<12}{'did not run':>9}  {record['note']}")
            continue
        print(
            f"{record['engine']:<12}"
            f"{record['query_s']:>8.2f}s"
            f"{record['rows_out']:>12,}"
            f"{gigabytes(record['peak_after_load_bytes']):>13}"
            f"{gigabytes(record['peak_rss_bytes']):>10}"
            f"{gigabytes(record['query_rss_bytes']):>10}"
        )

    if args.json:
        Path(args.json).write_text(
            json.dumps({"size": args.size, "query": args.query, "engines": measured}, indent=2)
            + "\n"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
