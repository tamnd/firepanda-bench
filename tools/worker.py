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
        [--answer results/answers/db-benchmark/q1/pandas.arrow]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import traceback
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics
import queries

import engines

# Which file format each suite's engines are handed. Everything but ingestion is
# compared on Parquet, and ingestion is the suite that measures the CSV reader.
SUITE_FORMAT = {"ingestion": "csv"}


def save_answer(answer, path: Path) -> str:
    """Writes an engine's answer to Arrow IPC so it can be compared exactly later.

    This happens after every timed run has finished and never inside one. The
    fingerprint is the check that sits on the timed path and this is the check that
    does not, so writing a gigabyte to disk here cannot move a published number.

    Arrow IPC rather than Parquet, because the point is to hand another process the
    answer the engine actually produced. Parquet would re-encode it, and a
    comparison run against a re-encoded copy is a comparison against the encoder.

    Args:
        answer: Whatever the query returned.
        path: Where to write it.

    Returns:
        A note if the answer could not be written, empty otherwise.
    """
    try:
        table = engines.as_arrow(answer)
        path.parent.mkdir(parents=True, exist_ok=True)
        with pa.ipc.new_file(path, table.schema) as writer:
            writer.write_table(table)
    except Exception as exc:
        # A failure here is not a failed measurement. The timings are already
        # taken and they are still good, so this reports and does not raise.
        return f"the answer could not be written for verification: {exc}"
    return ""


def table_paths(
    manifest: dict, root: Path, needed: tuple[str, ...], fmt: str = "parquet"
) -> dict[str, str]:
    """Resolves the file each table the query reads lives in.

    Args:
        manifest: The parsed dataset manifest.
        root: The directory the manifest sits in.
        needed: The table names the query reads.
        fmt: Which of the written formats to hand over.

    Returns:
        A mapping from table name to path.

    Raises:
        FileNotFoundError: If the dataset has no file of that format for a needed
            table.
    """
    paths = {}
    for table in needed:
        entry = manifest["files"].get(table)
        if entry is None or fmt not in entry:
            raise FileNotFoundError(f"the dataset has no {fmt} for '{table}'")
        # The manifest records an absolute path from the machine that generated
        # the data. Resolving against the manifest's own directory instead means a
        # dataset copied to another machine still works.
        paths[table] = str(root / f"{table}.{fmt}")
    return paths


def cold_note(dropped: bool) -> str:
    """Says what the cold run of an ingestion query was actually measuring.

    A cold number that was taken against a file still sitting in the page cache
    is a warm number with a misleading name, so which of the two happened is
    recorded next to the result rather than assumed.

    A note only when the eviction failed, because a note every measurement in the
    suite carries is not a note, it is a paragraph in the wrong place, and it
    crowds out the one that is about a single engine. That the eviction worked is
    already visible in the sample: `block_reads` on the cold run comes to the size
    of the file.

    Args:
        dropped: What `evict_page_cache` reported.

    Returns:
        A note for the measurement, empty if there is nothing surprising to say.
    """
    if dropped:
        return ""
    return (
        "the page cache could not be dropped on this machine, so the cold run is "
        "warm and only the warm numbers mean anything"
    )


def run_external(
    module,
    engine_name: str,
    query_name: str,
    manifest: dict,
    suite: str,
    runs: int,
    timeout_s: int,
    paths: dict[str, str],
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
        paths: The file each table the query reads lives in, empty for a suite
            whose data the child generates for itself.

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
        paths=paths,
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
    answer_path: Path | None = None,
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
        answer_path: Where to write the answer for the exact check, or None for
            the usual run, which writes nothing.

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
    fmt = SUITE_FORMAT.get(suite, "parquet")

    try:
        paths = table_paths(manifest, manifest_path.resolve().parent, query.needs, fmt)
    except FileNotFoundError as exc:
        measurement.note = str(exc)
        return measurement

    if getattr(module, "EXTERNAL", False):
        try:
            # The child does all of its runs itself, so the eviction that makes
            # the first one cold has to happen here, before it starts.
            if suite == "ingestion":
                measurement.note = cold_note(metrics.evict_page_cache(list(paths.values())))
            external = run_external(
                module, engine_name, query_name, manifest, suite, runs, timeout_s, paths
            )
            if external.ok and measurement.note:
                external.note = measurement.note
            if answer_path is not None:
                # An external engine reports numbers over a pipe rather than
                # handing back a table, so there is nothing here to write. It
                # abstains from the exact check the same way it abstains from the
                # text digest, which is reported rather than read as agreement.
                aside = f"{engine_name} does not write an answer file, so it is not verified"
                external.note = f"{external.note}; {aside}" if external.note else aside
            return external
        except Exception:
            measurement.note = traceback.format_exc(limit=4).strip().replace("\n", " | ")
            return measurement

    runner = engines.query_map(module, suite).get(query_name)
    if runner is None:
        measurement.note = f"{engine_name} does not implement {query_name} for {suite}"
        return measurement

    try:
        started = time.perf_counter()
        # An engine that prints on load must not print onto the result line.
        with contextlib.redirect_stdout(sys.stderr):
            context = module.load(paths, suite=suite, io=io)
        measurement.load_s = time.perf_counter() - started

        # For the ingestion suite `load` has read nothing, the timed function is
        # the read itself, and the first run is made cold on purpose.
        before_cold = None
        if suite == "ingestion":
            targets = list(paths.values())

            def evict() -> None:
                measurement.note = cold_note(metrics.evict_page_cache(targets))

            before_cold = evict

        with contextlib.redirect_stdout(sys.stderr):
            samples, answer = metrics.measure(
                lambda: runner(context), runs, before_cold=before_cold
            )
        measurement.samples = samples

        # Converted once, here, rather than once per thing that wants it. A DuckDB
        # relation is a stream and reading it a second time yields nothing, so a
        # digest and an answer file that each called `as_arrow` would produce a
        # correct digest and an empty answer, which looks exactly like an engine
        # that returned no rows.
        answer = engines.as_arrow(answer)

        reduce = engines.read_digest if suite == "ingestion" else engines.digest
        rows, cols, checksum, sums, hashes = reduce(answer)
        measurement.rows_out = rows
        measurement.cols_out = cols
        measurement.checksum = checksum
        measurement.sums = sums
        measurement.hashes = hashes
        # An engine that had to reconfigure itself to read a file at all says so
        # through its context, and that has to reach the result: a number taken
        # with different options is not the same number, and the reader of the
        # table has no other way to know.
        aside = context.get("note") if isinstance(context, dict) else None
        if aside:
            measurement.note = f"{measurement.note}; {aside}" if measurement.note else aside
        if answer_path is not None:
            failed = save_answer(answer, answer_path)
            if failed:
                measurement.note = f"{measurement.note}; {failed}" if measurement.note else failed
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
    # Off unless asked for. An answer file is the size of the answer, and for the
    # ingestion suite that is the size of the input, so this is a thing a release
    # run turns on rather than a thing every run pays for.
    parser.add_argument("--answer", type=Path, default=None)
    args = parser.parse_args(argv)

    measurement = run(
        args.engine,
        args.query,
        args.manifest,
        args.suite,
        args.io,
        args.runs,
        args.timeout,
        args.answer,
    )
    # Written to the real stdout, whatever the engine did to it.
    sys.__stdout__.write(metrics.dump(measurement) + "\n")
    sys.__stdout__.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
