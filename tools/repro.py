#!/usr/bin/env python3
"""Re-runs exactly the configuration that produced a result file.

Anyone should be able to check any number this project publishes, and the way to
make that true is to make it one command. A result file records the suite, the
size, the io mode, the engines and the number of runs, which is everything the
runner needs, so this reads them back out and starts the same run.

It also compares. A rerun that lands within the noise the original run recorded
is a confirmation; one that does not is either a different machine, a different
version, or a number that should not have been published, and the output says
which of those it can rule out.

The answers are compared as well as the timings, on the digest for every query
whose statement determines which rows come back and on the row count for the
twelve ClickBench queries whose statement does not. A reproduction of one of those
that returns a different ten rows out of a tie has reproduced.

Usage:
    python tools/repro.py results/2026-08-28-tpch-sf1-memory.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics
import queries as query_registry

ROOT = Path(__file__).resolve().parent.parent

# How far a rerun may drift before it is called a disagreement. Twenty five
# percent is loose, and it is loose on purpose: a shared machine, a different
# kernel and a warmer page cache all move a query by more than the run to run
# spread does, and a check that fires on all of those is a check nobody reads.
#
# It is a percentage and nothing else, which means on a suite whose medians are
# milliseconds it is a measurement of how quiet the machine is. ClickBench at 1M
# is that suite: most of its 129 pairings are under ten milliseconds and a laptop
# compiling something in another window moves them by three times, in whichever
# direction the load happened to fall. That is not a reason to add a floor, since
# raising one to a millisecond here changed 79 flagged pairings to 74, so the
# drift is the load rather than the timer. It is a reason to reproduce a file on
# the machine that produced it and while that machine is doing nothing else, and
# the sizes the scheduled job publishes have medians in seconds.
DRIFT = 0.25


def rerun(document: dict, output: Path) -> dict:
    """Runs the same configuration again.

    Args:
        document: The original result document.
        output: Where to write the new result file.

    Returns:
        The new document.

    Raises:
        SystemExit: If the run fails.
    """
    command = [
        sys.executable,
        str(Path(__file__).resolve().parent / "run.py"),
        "--suite",
        document["suite"],
        "--size",
        document["size"],
        "--io",
        document.get("io", "memory"),
        "--engines",
        ",".join(sorted(document["engines"])),
        "--runs",
        str(document["runs"]),
        "--output",
        str(output),
    ]
    print("running: " + " ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"the rerun failed with status {completed.returncode}")
    return json.loads(output.read_text())


def compare(original: dict, fresh: dict) -> int:
    """Prints how the rerun differed and returns how many numbers moved.

    Args:
        original: The published document.
        fresh: The new one.

    Returns:
        How many pairings drifted further than the tolerance.
    """
    print("\n| pairing | published | rerun | change |")
    print("| --- | ---: | ---: | ---: |")
    drifted = 0
    for key, was in sorted(original["results"].items()):
        now = fresh["results"].get(key)
        if not (was.get("ok") and now and now.get("ok")):
            continue
        before, after = was["median_s"], now["median_s"]
        if before <= 0:
            continue
        change = (after - before) / before
        flag = ""
        if abs(change) > DRIFT:
            flag = "  <- moved"
            drifted += 1
        print(f"| {key} | {before:.4f} s | {after:.4f} s | {change:+.1%}{flag} |")

    suite = original["suite"]
    for key, was in sorted(original["results"].items()):
        now = fresh["results"].get(key)
        if not (was.get("ok") and now and now.get("ok")):
            continue
        # Twelve of the ClickBench statements do not determine which rows come
        # back, and an engine is free to return a different ten of a tie on a
        # second run, which several of them do because the order a parallel
        # aggregation finishes in is not fixed. Comparing the digest there would
        # report a dozen changed answers on a reproduction that reproduced. The row
        # count is what the statement does determine, so that is what is checked,
        # and a query that came back with nine rows is still caught.
        reason = query_registry.undetermined(suite, key.split("/")[0])
        if reason:
            if was.get("rows_out") != now.get("rows_out"):
                print(
                    f"\n{key} returned {now.get('rows_out')} rows and the file has "
                    f"{was.get('rows_out')}. The rows this query returns are not "
                    f"determined, since {reason}, so the digest is not compared. "
                    f"How many of them come back is."
                )
                drifted += 1
            continue
        if was.get("checksum") != now.get("checksum"):
            print(f"\nthe answer changed for {key}, which is not a timing difference")
            drifted += 1
    return drifted


def main(argv: list[str] | None = None) -> int:
    """Reproduces a result file from the command line.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        Zero if nothing drifted further than the tolerance.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    original = json.loads(args.result.read_text())
    output = args.out or args.result.with_name(args.result.stem + "-repro.json")

    was_machine = original.get("machine", {})
    now_machine = metrics.machine_facts()
    if was_machine.get("cpu_model") != now_machine.get("cpu_model"):
        print(
            "warning: this is a different CPU than the one in the file "
            f"({now_machine.get('cpu_model')} against {was_machine.get('cpu_model')}). "
            "A timing difference here says nothing about the original run.",
            file=sys.stderr,
        )

    fresh = rerun(original, output)
    drifted = compare(original, fresh)
    if drifted:
        print(f"\n{drifted} numbers moved by more than {DRIFT:.0%}", file=sys.stderr)
        return 1
    print(f"\nevery number reproduced within {DRIFT:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
