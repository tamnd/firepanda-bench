#!/usr/bin/env python3
"""Measures what it costs each engine to get the 105 column hits table in memory.

Every other number in this repository is a query. This one is the load, on its
own, because on this table the load is the largest allocation any engine makes
and no query in the suite comes close to it. A query number that is measured
after the table is already resident says nothing about that, and the peak
resident set a run reports is the high water mark of the whole process, so the
load is hidden inside it rather than reported by it.

The shape of the table is why it is worth a tool. 105 columns of which almost
every query reads three, 28 of them stored as bytes that every engine converts to
text on the way in, and one date and three timestamps that are converted too. In
memory mode nobody gets to skip any of that, so this is the fairest comparison in
the suite: four engines, one file, the same conversions, nothing timed but the
load.

One process per engine, for the reason `worker.py` gives: a peak is a per process
high water mark, and an engine measured after another has allocated and freed
eight gigabytes reports a peak it never reached. Each child also reports what it
had allocated before it touched the file, so the interpreter and the imports can
be taken off the number rather than charged to the engine.

Usage:
    python tools/clickbench_load.py --size 1M
    python tools/clickbench_load.py --size 1M --engine pandas --measure
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import clickbench  # noqa: E402
import metrics  # noqa: E402

import engines  # noqa: E402

ENGINES = ("firepanda", *(name for name in engines.KNOWN if name != "firepanda"))
"""The engines to measure, the subject first and then the three it is measured
against. Taken from the registry rather than typed out again, so an engine added
there is measured here without anybody remembering to."""


def peak_rss_bytes() -> int:
    """Returns this process's high water mark of resident memory.

    Returns:
        The peak, in bytes on both platforms.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * metrics._RSS_UNIT


def pattern_for(size: str) -> str:
    """Returns the partition glob for one downloaded size.

    Args:
        size: `1M`, `10M` or `100M`.

    Returns:
        The glob the engines' loaders take.

    Raises:
        SystemExit: If that size was never downloaded.
    """
    directory = ROOT / "data" / "clickbench" / size
    if not directory.is_dir():
        raise SystemExit(
            f"no {size} dataset at {directory}. "
            f"Run: python tools/data.py --suite clickbench --size {size}"
        )
    return str(directory / "hits_*.parquet")


def measure(engine: str, pattern: str) -> dict:
    """Loads the table in this process and reports what it cost.

    The imports happen before the baseline is taken, so what the engine is charged
    with is the table and not pandas' own thirty megabytes of module objects.

    Args:
        engine: Which engine to load with.
        pattern: The partition glob.

    Returns:
        The measurement, as the parent prints it.

    Raises:
        SystemExit: If the engine is not one this knows.
    """
    if engine == "pandas":
        from engines import pandas_engine as module

        def load():
            """Loads the table.

            Returns:
                Whatever the engine holds it as.
            """
            return module.load_clickbench(pattern)

    elif engine == "polars":
        from engines import polars_engine as module

        def load():
            """Loads the table.

            Returns:
                Whatever the engine holds it as.
            """
            # Collected, because the memory mode frame is a lazy wrapper around an
            # Arrow table that is already resident and collecting it is what the
            # other two engines have already done by this point.
            return module.load_clickbench(pattern, "memory").collect()

    elif engine == "duckdb":
        import duckdb

        from engines import duckdb_engine as module

        def load():
            """Loads the table.

            Returns:
                Whatever the engine holds it as.
            """
            return module.load_clickbench(duckdb.connect(), pattern, "memory")

    else:
        raise SystemExit(f"clickbench_load: no engine called {engine}")

    baseline = peak_rss_bytes()
    started = time.perf_counter()
    held = load()
    took = time.perf_counter() - started
    peak = peak_rss_bytes()
    # Read after the measurement so that nothing here is optimized away, and
    # printed nowhere, since what it holds is a hundred million rows.
    assert held is not None
    return {
        "engine": engine,
        "load_s": took,
        "peak_rss_bytes": peak,
        "baseline_rss_bytes": baseline,
        "table_rss_bytes": peak - baseline,
    }


def measure_firepanda(pattern: str) -> dict:
    """Asks the firepanda driver to load the table and say what that cost.

    A separate function because firepanda is a compiled binary rather than a
    module this can import, so its schema mode is what reports the numbers and
    this reads them back. Its baseline is what the driver had allocated before it
    opened the file, which for a Mojo binary with no interpreter under it is
    small but is not nothing.

    Args:
        pattern: The partition glob.

    Returns:
        The measurement, in the same shape the Python engines report.

    Raises:
        SystemExit: If the driver cannot be built or refuses the file.
    """
    from engines import firepanda_engine

    binary = firepanda_engine.build()
    finished = subprocess.run(
        [str(binary), "--schema=clickbench", f"--path={pattern}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        raise SystemExit(f"the firepanda driver exited {finished.returncode}: {finished.stderr}")
    answer = json.loads(finished.stdout.strip().splitlines()[-1])
    if not answer.get("ok"):
        raise SystemExit(f"the firepanda driver refused the file: {answer.get('note')}")
    return {
        "engine": "firepanda",
        "load_s": answer["load_s"],
        "peak_rss_bytes": answer["peak_rss_bytes"],
        "baseline_rss_bytes": 0,
        "table_rss_bytes": answer["peak_rss_bytes"],
        "columns": len(answer["columns"]),
        "rows": answer["rows"],
    }


def run_child(engine: str, size: str) -> dict:
    """Runs one engine's measurement in a process of its own.

    Args:
        engine: Which engine.
        size: Which downloaded size.

    Returns:
        The measurement, or a record saying why there is not one.
    """
    if engine == "firepanda":
        try:
            return measure_firepanda(pattern_for(size))
        except SystemExit as reason:
            return {"engine": engine, "note": str(reason)}
    finished = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--measure",
            "--engine",
            engine,
            "--size",
            size,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        return {"engine": engine, "note": finished.stderr.strip().splitlines()[-1:] or ["failed"]}
    return json.loads(finished.stdout.strip().splitlines()[-1])


def gigabytes(value: int) -> str:
    """Formats a byte count the way the report formats one.

    Args:
        value: The bytes.

    Returns:
        The count in gigabytes, to two decimal places.
    """
    return f"{value / 1e9:.2f} GB"


def main(argv: list[str] | None = None) -> int:
    """Measures every engine's load and prints a table.

    Args:
        argv: The arguments, or None for the command line.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", default="1M", choices=sorted(clickbench.SIZES))
    parser.add_argument("--engine", default=None, help="one engine rather than all four")
    parser.add_argument("--measure", action="store_true", help="the child mode, not for people")
    parser.add_argument("--json", default=None, help="also write the measurements here")
    args = parser.parse_args(argv)

    if args.measure:
        print(json.dumps(measure(args.engine, pattern_for(args.size))))
        return 0

    wanted = [args.engine] if args.engine else list(ENGINES)
    measured = [run_child(engine, args.size) for engine in wanted]

    print(f"the hits table at {args.size}, 105 columns, loaded into memory")
    print(f"{'engine':<12}{'load':>10}{'peak rss':>12}{'over baseline':>16}")
    for record in measured:
        if "note" in record:
            print(f"{record['engine']:<12}{'did not run':>10}  {record['note']}")
            continue
        print(
            f"{record['engine']:<12}"
            f"{record['load_s']:>9.2f}s"
            f"{gigabytes(record['peak_rss_bytes']):>12}"
            f"{gigabytes(record['table_rss_bytes']):>16}"
        )

    if args.json:
        Path(args.json).write_text(
            json.dumps({"size": args.size, "engines": measured}, indent=2) + "\n"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
