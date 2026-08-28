"""firepanda, which is written in Mojo and therefore does not run in this process.

Everything else in this directory is a Python library the worker imports. firepanda
is a Mojo library, so the adapter compiles a driver against the firepanda checkout
and runs it as a child process. The driver prints one line of JSON: how long each
run took, how much memory the kernel says it used, and the row count, column sums
and text column digests of its answer, which is what the cross engine fingerprint
is built from.

There is a fairness problem here and it is worth stating plainly rather than
burying it in a footnote.

firepanda cannot read a Parquet file yet. It has a CSV field scanner and field
parsers and no reader on top of them, and it has no Parquet at all. So it cannot
be handed the file the other three engines are handed. What the driver does
instead is generate the same data, using the same splitmix64 stream in the same
counter form as `tools/data.py`, and the fingerprint check is what makes that
claim testable rather than asserted.

That works for db-benchmark, whose data is generated. It does not work for TPC-H,
whose data comes from dbgen, and no amount of cleverness makes it work: there is
no way to run a TPC-H query against a table you cannot read. firepanda is
therefore reported as unable to run TPC-H, with that reason, until it has a
reader. It is not omitted from the table, because a table that silently contains
only the suites we do well on is an advertisement.

The generated path is also not free of doubt even where it applies. Generating a
column is not the same as reading one, so firepanda's load time is not comparable
to anyone else's and the report keeps it in its own column rather than adding it
to the query time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

NAME = "firepanda"

# This engine is a separate process rather than an importable library, so the
# worker calls `measure` instead of `load` and a query map.
EXTERNAL = True

ROOT = Path(__file__).resolve().parent.parent.parent
DRIVER_SOURCE = ROOT / "engines" / "firepanda" / "main.mojo"

# Where the queries stand today. The driver refuses anything not in here, and the
# reasons are reported next to the empty cells rather than left to be guessed.
#
# The five string keyed group by queries moved in here when firepanda learned to
# put a string column in a `DataFrame`, group by one and aggregate one. What is
# left is the two queries that need a kernel nobody has written.
SUPPORTED = (
    "q1",
    "q2",
    "q3",
    "q4",
    "q5",
    "q6",
    "q7",
    "q10",
    "j1",
    "j2",
    "j3",
    "j4",
    "j5",
)

UNSUPPORTED = {
    "q8": "needs a top-k per group, which is not a kernel that exists",
    "q9": "needs a correlation aggregate, which is not a kernel that exists",
}


def firepanda_home() -> Path:
    """Finds the firepanda checkout to build against.

    Args:
        None.

    Returns:
        The checkout path.

    Raises:
        SystemExit: If no checkout can be found.
    """
    candidates = []
    env = os.environ.get("FIREPANDA_HOME")
    if env:
        candidates.append(Path(env))
    candidates.append(ROOT.parent / "firepanda")
    candidates.append(Path.home() / "firepanda")
    for path in candidates:
        if (path / "firepanda" / "__init__.mojo").exists():
            return path
    raise SystemExit(
        "cannot find a firepanda checkout. Set FIREPANDA_HOME to one, or put it "
        "beside this repository."
    )


def version() -> str:
    """Returns the version of the firepanda checkout being measured.

    Returns:
        The version string, or empty if it cannot be read.
    """
    try:
        source = (firepanda_home() / "firepanda" / "version.mojo").read_text()
    except (OSError, SystemExit):
        return ""
    found = re.search(r'comptime VERSION = "([^"]+)"', source)
    return found.group(1) if found else ""


def git_ref() -> str:
    """Returns the commit the firepanda checkout is on.

    The benchmark machines get the checkout as a tarball with `.git` excluded,
    because shipping the history to run a benchmark is a waste of a link, so `git
    rev-parse` finds nothing there and every result file written on a real machine
    carried an empty ref. That is the one field that makes a regression
    attributable, so it is also read from `FIREPANDA_REF` and from a `GIT_REF`
    file the sync writes into the checkout.

    Returns:
        The short hash, or empty if none of the three know it.
    """
    env = os.environ.get("FIREPANDA_REF", "").strip()
    if env:
        return env
    try:
        home = firepanda_home()
    except SystemExit:
        return ""
    stamp = home / "GIT_REF"
    if stamp.exists():
        return stamp.read_text().strip()
    try:
        completed = subprocess.run(
            ["git", "-C", str(home), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout.strip()


def build(force: bool = False) -> Path:
    """Compiles the driver and returns the binary.

    A compiled binary rather than `mojo run`, and the reason is memory. `mojo run`
    holds the compiler in the same process as the program, and its resident set
    starts above three hundred megabytes before a single row exists. Reporting
    that as firepanda's memory use would be wrong by more than the thing being
    measured.

    Args:
        force: Whether to rebuild even if the binary is newer than the source.

    Returns:
        The binary path.

    Raises:
        SystemExit: If the build fails.
    """
    home = firepanda_home()
    binary = ROOT / "engines" / "firepanda" / "firepanda-driver"
    if not force and binary.exists() and binary.stat().st_mtime > DRIVER_SOURCE.stat().st_mtime:
        return binary

    command = [
        "pixi",
        "run",
        "--manifest-path",
        str(home / "pixi.toml"),
        "mojo",
        "build",
        "-I",
        str(home),
        str(DRIVER_SOURCE),
        "-o",
        str(binary),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not binary.exists():
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-6:]
        raise SystemExit("cannot build the firepanda driver: " + " | ".join(tail))
    return binary


def measure(query: str, rows: int, runs: int, suite: str, timeout_s: int) -> dict:
    """Runs one query in a child process and returns what it measured.

    Args:
        query: The query name.
        rows: How many rows in the large table.
        runs: How many timed runs.
        suite: Which suite is being run.
        timeout_s: How long to wait for the child.

    Returns:
        A mapping with `ok` and, when true, the timings, memory and answer digest
        inputs. When false, `note` says why.
    """
    if suite != "db-benchmark":
        return {
            "ok": False,
            "note": (
                f"firepanda cannot run {suite}: it has no Parquet reader, and "
                "unlike db-benchmark this suite's data cannot be regenerated "
                "from a seed"
            ),
        }
    if query not in SUPPORTED:
        return {
            "ok": False,
            "note": UNSUPPORTED.get(query, f"firepanda does not implement {query}"),
        }

    try:
        binary = build()
    except SystemExit as exc:
        return {"ok": False, "note": str(exc)}

    command = [
        str(binary),
        f"--query={query}",
        f"--rows={rows}",
        f"--runs={runs}",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "note": f"timed out after {timeout_s}s"}

    lines = [line for line in completed.stdout.strip().splitlines() if line.startswith("{")]
    if not lines:
        tail = (completed.stderr or "").strip().splitlines()[-3:]
        return {
            "ok": False,
            "note": (
                f"the driver printed no result (exit {completed.returncode}): " + " | ".join(tail)
            ),
        }
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {"ok": False, "note": f"the driver printed unreadable JSON: {exc}"}


def main() -> int:
    """Builds the driver from the command line, so a failure is seen before a run.

    Returns:
        A process exit status.
    """
    binary = build(force="--force" in sys.argv)
    print(f"built {binary} against firepanda {version()} at {git_ref() or 'unknown'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
