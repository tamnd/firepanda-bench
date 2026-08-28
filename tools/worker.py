#!/usr/bin/env python3
"""Runs one engine on one query, in a process of its own, and prints the result.

One process per engine and query, rather than one process for everything, and the
reason is memory. Peak resident memory is a per process high water mark, so an
engine measured after another engine has already allocated eight gigabytes and
freed it reports a peak it never reached. A fresh process per pairing is the only
way to attribute memory to the thing that used it.

The process prints exactly one line of JSON on stdout. Anything an engine prints
goes to stderr, so a chatty engine cannot corrupt the result.

Usage:
    python tools/worker.py --engine pandas --query q1 --suite db-benchmark
        --manifest data/db-benchmark/0.5GB/manifest.json --io memory --runs 10
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics
import queries

import engines


def table_paths(manifest: dict, root: Path, needed: tuple[str, ...]) -> dict[str, str]:
    """Resolves the file each table the query reads lives in.

    Args:
        manifest: The parsed dataset manifest.
        root: The directory the manifest sits in.
        needed: The table names the query reads.

    Returns:
        A mapping from table name to path.

    Raises:
        FileNotFoundError: If the dataset has no Parquet for a needed table.
    """
    paths = {}
    for table in needed:
        entry = manifest["files"].get(table)
        if entry is None or "parquet" not in entry:
            raise FileNotFoundError(f"the dataset has no parquet for '{table}'")
        # The manifest records an absolute path from the machine that generated
        # the data. Resolving against the manifest's own directory instead means a
        # dataset copied to another machine still works.
        paths[table] = str(root / f"{table}.parquet")
    return paths


def run_external(
    module,
    engine_name: str,
    query_name: str,
    manifest: dict,
    suite: str,
    runs: int,
    timeout_s: int,
) -> metrics.Measurement:
    """Measures an engine that is not a Python library, by running it.

    The child does its own timing and reads its own memory out of `/proc`, because
    an engine measured from outside reports the interpreter's startup as part of
    its peak. What comes back is one JSON object, and this turns it into the same
    `Measurement` the in process engines produce so the report cannot tell them
    apart.

    The child reports CPU for the whole timed region rather than per run, since
    the kernel's per process CPU counter has a ten millisecond tick. Splitting it
    evenly across the runs is what the summary needs, and it leaves the total, and
    therefore the parallelism, exactly as measured.

    Args:
        module: The engine module.
        engine_name: The engine name.
        query_name: The query name.
        manifest: The parsed dataset manifest.
        suite: The suite name.
        runs: How many timed runs.
        timeout_s: How long to wait for the child.

    Returns:
        The measurement.
    """
    measurement = metrics.Measurement(engine=engine_name, query=query_name, ok=False)
    result = module.measure(
        query=query_name,
        rows=int(manifest.get("rows", 0)),
        runs=runs,
        suite=suite,
        timeout_s=timeout_s,
    )
    if not result.get("ok"):
        measurement.note = result.get("note", "the engine reported a failure")
        return measurement

    child_runs = result.get("runs", [])
    if not child_runs:
        measurement.note = "the engine reported no runs"
        return measurement

    share = 1.0 / len(child_runs)
    cpu_user = float(result.get("runs_cpu_user_s", 0.0)) * share
    cpu_sys = float(result.get("runs_cpu_sys_s", 0.0)) * share
    peak = int(result.get("peak_rss_bytes", 0))
    for index, run in enumerate(child_runs):
        measurement.samples.append(
            metrics.Sample(
                wall_s=float(run["wall_s"]),
                cpu_user_s=cpu_user,
                cpu_sys_s=cpu_sys,
                peak_rss_bytes=int(run.get("peak_rss_bytes", peak)),
                # Not measured per run by the child, and reported as zero rather
                # than guessed at.
                rss_delta_bytes=0,
                rss_peak_during_bytes=int(run.get("rss_bytes", 0)) or peak,
                minor_faults=0,
                major_faults=0,
                voluntary_switches=int(result.get("voluntary_switches", 0)) if index == 0 else 0,
                involuntary_switches=(
                    int(result.get("involuntary_switches", 0)) if index == 0 else 0
                ),
                block_reads=0,
                block_writes=0,
                threads_peak=int(result.get("threads", 0)),
                cold=index == 0,
            )
        )
    measurement.load_s = float(result.get("load_s", 0.0))
    measurement.rows_out = int(result.get("rows_out", 0))
    measurement.cols_out = int(result.get("cols_out", 0))
    measurement.sums = {k: float(v) for k, v in result.get("sums", {}).items()}
    # An external engine that can return a text column has to compute the same
    # FNV-1a the harness does, and until it does it sends no hashes and the
    # fingerprint is over the numbers alone.
    measurement.hashes = {k: int(v) for k, v in result.get("hashes", {}).items()}
    measurement.checksum = engines.fingerprint(
        measurement.rows_out, measurement.sums, measurement.hashes
    )
    measurement.ok = True
    return measurement


def run(
    engine_name: str,
    query_name: str,
    manifest_path: Path,
    suite: str,
    io: str,
    runs: int,
    timeout_s: int,
) -> metrics.Measurement:
    """Loads the data, runs the query the requested number of times and measures it.

    Args:
        engine_name: Which engine.
        query_name: Which query.
        manifest_path: The dataset manifest written by `data.py` or `tpch.py`.
        suite: Which suite the query belongs to.
        io: Whether every engine gets the data in memory, or an engine that can
            scan the file is allowed to.
        runs: How many times to run the query.
        timeout_s: How long an external engine gets before it is given up on.

    Returns:
        The measurement, with `ok` false and a note if anything went wrong.
    """
    measurement = metrics.Measurement(engine=engine_name, query=query_name, ok=False)
    try:
        query = queries.lookup(suite, query_name)
    except SystemExit as exc:
        measurement.note = str(exc)
        return measurement

    try:
        module = engines.load_engine(engine_name)
    except SystemExit as exc:
        measurement.note = str(exc)
        return measurement

    manifest = json.loads(manifest_path.read_text())

    if getattr(module, "EXTERNAL", False):
        try:
            return run_external(module, engine_name, query_name, manifest, suite, runs, timeout_s)
        except Exception:
            measurement.note = traceback.format_exc(limit=4).strip().replace("\n", " | ")
            return measurement

    runner = engines.query_map(module, suite).get(query_name)
    if runner is None:
        measurement.note = f"{engine_name} does not implement {query_name} for {suite}"
        return measurement

    try:
        paths = table_paths(manifest, manifest_path.resolve().parent, query.needs)
    except FileNotFoundError as exc:
        measurement.note = str(exc)
        return measurement

    try:
        started = time.perf_counter()
        # An engine that prints on load must not print onto the result line.
        with contextlib.redirect_stdout(sys.stderr):
            context = module.load(paths, suite=suite, io=io)
        measurement.load_s = time.perf_counter() - started

        with contextlib.redirect_stdout(sys.stderr):
            samples, answer = metrics.measure(lambda: runner(context), runs)
        measurement.samples = samples

        rows, cols, checksum, sums, hashes = engines.digest(answer)
        measurement.rows_out = rows
        measurement.cols_out = cols
        measurement.checksum = checksum
        measurement.sums = sums
        measurement.hashes = hashes
        measurement.ok = True
    except NotImplementedError as exc:
        measurement.note = str(exc) or f"{engine_name} cannot run {query_name} yet"
    except Exception:
        measurement.note = traceback.format_exc(limit=4).strip().replace("\n", " | ")
    return measurement


def main(argv: list[str] | None = None) -> int:
    """Runs one measurement from the command line.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        A process exit status. Zero even when the query failed, because a failure
        is a result the report has to show rather than a crash.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--suite", default="db-benchmark")
    parser.add_argument("--io", default="memory", choices=engines.IO_MODES)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args(argv)

    measurement = run(
        args.engine,
        args.query,
        args.manifest,
        args.suite,
        args.io,
        args.runs,
        args.timeout,
    )
    # Written to the real stdout, whatever the engine did to it.
    sys.__stdout__.write(metrics.dump(measurement) + "\n")
    sys.__stdout__.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
