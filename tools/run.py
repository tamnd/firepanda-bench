#!/usr/bin/env python3
"""Runs a suite against several engines and writes the result file.

The runner itself does very little. It works out which pairings to measure, starts
a worker process for each one, collects the JSON line each worker prints, and
writes them out with enough context that the file can be replayed. All of the
measurement happens in the worker, because peak memory is a per process number.

Every engine in a result file ran on the same machine in the same invocation. The
harness will not merge files from different machines into one table, because a
normalized cross machine comparison is a model rather than a measurement.

Usage:
    python tools/run.py --suite db-benchmark --size 0.5GB --engines all --runs 10
    python tools/run.py --suite ingestion --size 10M --engines all --runs 7
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_report
import metrics
import queries as query_registry

import engines as engine_registry

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "data"

# Long enough for a fifty gigabyte join and short enough that a hung engine does
# not hold a scheduled run open all weekend.
DEFAULT_TIMEOUT_S = 3600

# The size to run when none is named. Each is the smallest of its suite that is
# worth publishing, which is the one that fits on a laptop and finishes over a
# coffee.
DEFAULT_SIZE = {"db-benchmark": "0.5GB", "tpch": "sf1", "ingestion": "10M"}

# The io mode a suite runs in when none is named. Ingestion is scan and cannot be
# anything else: its timed function is the read, and a memory mode for it would
# mean reading the file before timing the read.
DEFAULT_IO = {"ingestion": "scan"}


def engine_versions(names: list[str]) -> dict[str, str]:
    """Asks each engine for its version.

    A comparison against an unnamed version of anything is not a comparison, so a
    version that cannot be determined is recorded as empty and the validator
    rejects the file.

    Args:
        names: The engine names.

    Returns:
        A mapping from engine name to version.
    """
    found = {}
    for name in names:
        try:
            module = engine_registry.load_engine(name)
            found[name] = module.version()
        except Exception as exc:
            found[name] = ""
            print(f"warning: cannot determine {name} version: {exc}", file=sys.stderr)
    return found


def measure_one(
    engine: str,
    query: str,
    manifest: Path,
    suite: str,
    io: str,
    runs: int,
    timeout_s: int,
) -> metrics.Measurement:
    """Starts a worker for one pairing and reads back what it measured.

    Args:
        engine: The engine name.
        query: The query name.
        manifest: The dataset manifest.
        suite: The suite name.
        io: The io mode, either `memory` or `scan`.
        runs: How many runs.
        timeout_s: How long to wait before giving up on the worker.

    Returns:
        The measurement, or a failed one carrying the reason.
    """
    command = [
        sys.executable,
        str(Path(__file__).resolve().parent / "worker.py"),
        "--engine",
        engine,
        "--query",
        query,
        "--manifest",
        str(manifest),
        "--suite",
        suite,
        "--io",
        io,
        "--runs",
        str(runs),
        # The worker gets a little less than the parent, so an external engine
        # that hangs is reported by the worker with its own name on it rather
        # than showing up here as a worker that produced nothing.
        "--timeout",
        str(max(30, timeout_s - 30)),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return metrics.Measurement(
            engine=engine, query=query, ok=False, note=f"timed out after {timeout_s}s"
        )

    line = completed.stdout.strip().splitlines()
    if not line:
        tail = (completed.stderr or "").strip().splitlines()[-3:]
        return metrics.Measurement(
            engine=engine,
            query=query,
            ok=False,
            note=f"worker produced no result (exit {completed.returncode}): {' | '.join(tail)}",
        )
    return metrics.load(line[-1])


# How far two engines' column sums may be apart and still be the same answer.
#
# A part in ten million. Loose enough for the float noise that is unavoidable
# here: engines add in different orders, and pandas has to carry TPC-H money in
# float64 because Arrow refuses the precision the exact arithmetic needs. Tight
# enough that the errors this is for cannot hide, because an engine that dropped
# a group, kept a null it should not have or read the wrong file moves a total by
# percent, not by parts per million.
AGREEMENT_TOLERANCE = 1e-7


def answers_match(left: metrics.Measurement, right: metrics.Measurement) -> bool:
    """Says whether two engines produced the same answer.

    Args:
        left: One measurement.
        right: The other.

    Returns:
        Whether the row counts are equal, every column sum is within the
        tolerance, and every text digest is identical.
    """
    if left.rows_out != right.rows_out:
        return False
    if set(left.sums) != set(right.sums):
        return False
    for name, value in left.sums.items():
        other = right.sums[name]
        scale = max(abs(value), abs(other), 1.0)
        if abs(value - other) > AGREEMENT_TOLERANCE * scale:
            return False
    # Text is compared exactly and not against the tolerance above. A relative
    # tolerance on a number that lives near two to the sixty four would let almost
    # any two hashes through, which is the same as not comparing them.
    #
    # Only when both engines produced them, because an external engine that cannot
    # yet return a text column sends none, and treating that as a disagreement
    # would report firepanda as computing a different answer when what it did was
    # abstain.
    return not (left.hashes and right.hashes and left.hashes != right.hashes)


def agreement(measurements: list[metrics.Measurement]) -> dict[str, dict]:
    """Groups the answers by query and reports whether the engines agreed.

    This is the check that stops the harness from publishing a comparison in which
    the engines were quietly computing different things. An engine that is fast
    because it grouped fewer rows is not fast.

    The comparison is on the numbers rather than on the digest. The digest rounds
    to nine significant figures before it hashes, so two sums that differ in the
    tenth digit disagree whenever the rounding happens to straddle a boundary, and
    that turned four TPC-H queries into reported disagreements on a run where all
    three engines had matched the specification's published answers exactly.

    Args:
        measurements: Every measurement in the run.

    Returns:
        A mapping from query name to the digests seen and whether the answers
        behind them matched.
    """
    by_query: dict[str, list[metrics.Measurement]] = {}
    for m in measurements:
        if m.ok:
            by_query.setdefault(m.query, []).append(m)
    report = {}
    for query, seen in by_query.items():
        first = seen[0]
        report[query] = {
            "agreed": all(answers_match(first, other) for other in seen[1:]),
            "by_engine": {m.engine: f"{m.rows_out}:{m.checksum}" for m in seen},
        }
    return report


def machine_slug(machine: dict) -> str:
    """Reduces a machine description to something that can go in a file name.

    Args:
        machine: The machine block of a result document.

    Returns:
        The host name in lower case with anything but letters, digits and dashes
        replaced, or `unknown-host` if the machine did not report one.
    """
    host = str(machine.get("host") or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return slug or "unknown-host"


def describe_environment(path: Path | None) -> dict:
    """Builds the environment block that goes into the result file.

    The toolchain version and the firepanda commit used to be read from an
    `env.json` and from nowhere else, so a run started with `pixi run bench`,
    which is how every run outside CI is started, wrote an empty commit into the
    result file. An empty commit makes a regression unattributable, and a file
    left over from an older checkout is worse than empty because it attributes the
    numbers to the wrong commit without saying so. The machine is probed here
    instead, and a file is only consulted for the fields the probe left blank.

    Args:
        path: An `env.json` from env_report.py, or None.

    Returns:
        The record.
    """
    probed = env_report.describe()
    if path is None or not path.exists():
        return probed
    stored = json.loads(path.read_text())
    for key, value in stored.items():
        if not probed.get(key):
            probed[key] = value
    return probed


def main(argv: list[str] | None = None) -> int:
    """Runs the suite and writes the result file.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="db-benchmark", choices=sorted(query_registry.SUITES))
    parser.add_argument("--size", default="")
    parser.add_argument("--engines", default="all")
    parser.add_argument("--queries", default="all")
    parser.add_argument(
        "--io",
        default="",
        choices=("", *engine_registry.IO_MODES),
        help="memory gives every engine the same in memory tables; scan lets an "
        "engine that can push a projection into the file do it. Defaults to "
        "memory, and to scan for the ingestion suite, which has no other mode",
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--env",
        type=Path,
        help="an env.json from env_report.py, consulted only for fields this run could not probe",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    size = args.size or DEFAULT_SIZE[args.suite]
    io = args.io or DEFAULT_IO.get(args.suite, "memory")
    if args.suite == "ingestion" and io != "scan":
        raise SystemExit(
            "the ingestion suite runs in scan mode only. The reader is the thing "
            "being measured, and memory mode would read the file before timing "
            "the read."
        )
    names = (
        list(engine_registry.KNOWN)
        if args.engines in ("all", "")
        else [e.strip() for e in args.engines.split(",")]
    )
    selected = query_registry.select(args.queries, args.suite)

    manifest = DATA_ROOT / args.suite / size / "manifest.json"
    if not manifest.exists():
        raise SystemExit(
            f"no dataset at {manifest}. Run: pixi run data --suite {args.suite} --size {size}"
        )

    environment = describe_environment(args.env)

    print(
        f"{args.suite} at {size}, io {io}: {len(selected)} queries "
        f"x {len(names)} engines x {args.runs} runs"
    )

    measurements: list[metrics.Measurement] = []
    started = time.perf_counter()
    for query in selected:
        for engine in names:
            label = f"  {query.name:<17} {engine:<10}"
            print(label, end="", flush=True)
            measurement = measure_one(
                engine, query.name, manifest, args.suite, io, args.runs, args.timeout
            )
            measurements.append(measurement)
            if measurement.ok:
                summary = measurement.summary()
                print(
                    f" {summary['median_s']:8.3f} s  "
                    f"iqr {summary['iqr_s']:7.3f} s  "
                    f"rss {summary['peak_rss_bytes'] / 1e9:6.2f} GB  "
                    f"{measurement.rows_out:>12,} rows"
                )
            else:
                print(f" skipped: {measurement.note[:88]}")
    elapsed = time.perf_counter() - started

    document = {
        "suite": args.suite,
        "size": size,
        # Which engines were handed a file and which were handed memory. A reader
        # comparing two result files has to know this, because a scan run and a
        # memory run of the same query are not the same measurement.
        "io": io,
        "engines": engine_versions(names),
        "mojo_version": environment.get("mojo_version", ""),
        "firepanda_ref": environment.get("firepanda_ref", ""),
        "machine": environment.get("machine") or metrics.machine_facts(),
        "runs": args.runs,
        "dataset": json.loads(manifest.read_text()),
        "wall_s": round(elapsed, 3),
        "agreement": agreement(measurements),
        "results": {},
        "detail": {},
    }
    for measurement in measurements:
        key = f"{measurement.query}/{measurement.engine}"
        entry = measurement.summary()
        entry.update(
            {
                "ok": measurement.ok,
                "note": measurement.note,
                "load_s": round(measurement.load_s, 4),
                "rows_out": measurement.rows_out,
                "checksum": measurement.checksum,
            }
        )
        if not measurement.ok:
            entry["cache"] = "none"
        document["results"][key] = entry
        document["detail"][key] = [asdict(s) for s in measurement.samples]

    # The machine is in the name, not only inside the file. Two machines running
    # the same suite at the same size in the same io mode on the same day produce
    # the same name otherwise, and the second one to be copied into `results/`
    # silently replaces the first. Nothing in the harness would notice, and the
    # file that survived would look like the only run there had ever been.
    out = args.output or (
        ROOT
        / "results"
        / (
            f"{time.strftime('%Y-%m-%d')}-{machine_slug(document['machine'])}-"
            f"{args.suite}-{size}-{io}.json"
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2) + "\n")
    print(f"\nwrote {out} in {elapsed:.1f} s on {platform.node()}")

    disagreed = [q for q, a in document["agreement"].items() if not a["agreed"]]
    if disagreed:
        print(
            "warning: engines disagreed on " + ", ".join(sorted(disagreed)),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
