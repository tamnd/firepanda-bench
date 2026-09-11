"""firepanda, which is written in Mojo and therefore does not run in this process.

Everything else in this directory is a Python library the worker imports. firepanda
is a Mojo library, so the adapter compiles a driver against the firepanda checkout
and runs it as a child process. The driver prints one line of JSON: how long each
run took, how much memory the kernel says it used, and the row count, column sums
and text column digests of its answer, which is what the cross engine fingerprint
is built from.

There is a fairness problem here and it is worth stating plainly rather than
burying it in a footnote.

firepanda has no Parquet decoder of its own. It can open a Parquet file, and the
way it does that is to hand the file to DuckDB and read DuckDB's vectors back as
Arrow. That is a sensible thing for a dataframe library to do and it is not a
thing that can be put on a timer in a table where DuckDB is one of the four
engines, because the number that came out would be DuckDB's reader wearing
firepanda's name. Parquet is what the other three engines are handed for
db-benchmark and TPC-H, so for those two suites firepanda cannot be handed the
same file. What the driver does instead is generate the same data, using the same
splitmix64 stream in the same counter form as `tools/data.py`, and the fingerprint
check is what makes that claim testable rather than asserted.

The ingestion suite is the exception and it is the honest one. That suite's data
is CSV, firepanda opens the same file as everybody else, and nothing about the
comparison depends on two generators agreeing.

That works for db-benchmark, whose data is generated. It does not work for TPC-H,
whose data comes from dbgen, and it does not work for ClickBench, whose data is a
recorded web log: neither has a seed to reproduce. So both are run the only other
honest way, which is to read the same Parquet files everybody else reads, through
DuckDB, before the clock starts. That is exactly what polars and pandas do under
`--io memory`, where the whole table is loaded eagerly in `load` and the query is
timed on frames that are already in memory. It is not what happens under `--io
scan`, where a native reader is most of the answer, and firepanda is reported as
unable to run that mode until it decodes Parquet itself.

There is a second TPC-H caveat and it runs the other way. The money columns are
DECIMAL(15,2) in the specification, DuckDB and polars carry decimals through, and
firepanda's Arrow import cannot read decimal128, so the driver casts them to
double at load. pandas does the same. Float multiplication is faster than decimal
multiplication, so this flatters firepanda and pandas both, and the twenty two
answers are still checked against the published validation output before any
number is reported.

The generated path is also not free of doubt even where it applies. Generating a
column is not the same as reading one, so firepanda's load time is not comparable
to anyone else's and the report keeps it in its own column rather than adding it
to the query time.
"""

from __future__ import annotations

import hashlib
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
DRIVER_DIR = ROOT / "engines" / "firepanda"
DRIVER_SOURCE = DRIVER_DIR / "main.mojo"

# Where the queries stand today. The driver refuses anything not in here, and the
# reasons are reported next to the empty cells rather than left to be guessed.
#
# The five string keyed group by queries moved in here when firepanda learned to
# put a string column in a `DataFrame`, group by one and aggregate one. q9 moved
# in with 0.6.24, which added a correlation that reads two columns at once, and
# q8 with the per group top-n kernel. All fifteen run.
SUPPORTED = (
    "q1",
    "q2",
    "q3",
    "q4",
    "q5",
    "q6",
    "q7",
    "q8",
    "q9",
    "q10",
    "j1",
    "j2",
    "j3",
    "j4",
    "j5",
    "j6",
)

UNSUPPORTED: dict[str, str] = {}

# All twenty two TPC-H queries. Each one reproduces the specification's published
# validation output at sf1, checked by `tools/validate_tpch_csv.py` against the
# answers DuckDB ships, before it was allowed in here.
TPCH_SUPPORTED = tuple(f"q{number}" for number in range(1, 23))

# The eight tables, in the order `table_names` in the driver returns them. The
# driver takes one `--path-<table>=` flag per table and leaves the ones it is not
# given unread.
TPCH_TABLES = (
    "customer",
    "lineitem",
    "nation",
    "orders",
    "part",
    "partsupp",
    "region",
    "supplier",
)

# The ingestion suite, all of which the CSV reader handles.
INGESTION_SUPPORTED = (
    "csv_narrow",
    "csv_narrow_typed",
    "csv_wide",
    "csv_quoted",
    "csv_nulls",
)

# The ClickBench list is not written down here. The driver knows which of the 43
# it runs and what it says about the ones it does not, and `--list=clickbench`
# asks it, which is one list instead of two that have to be kept in step. The
# tuples above are the older shape and they have gone stale twice: a query landed
# in the driver and the table went on reporting it as missing, because the list
# the harness reads was somewhere else. There is nothing to edit here when q28
# gets its regex engine.
_CLICKBENCH_SUPPORT: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {}


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


def source_fingerprint(home: Path) -> str:
    """Returns a content hash of every source the driver links.

    The driver is one file and the library it measures is several hundred, so
    comparing the binary against the driver alone answers a question nobody
    asked. A library change with no driver change left the old binary in place
    and the harness went on reporting a version of firepanda that no longer
    existed, which is the worst way for a benchmark to be wrong: silently, and
    in whichever direction the last change went.

    This used to compare modification times, and that was wrong in a way that
    cost a real measurement. The benchmark machines get the checkout as a
    tarball, tar restores the modification times the files had in the checkout it
    was made from, and a source edited last week therefore arrives looking older
    than a binary built here yesterday. The harness then reused the binary and
    reported the previous version of firepanda under the new commit's name. It is
    the same failure the mtime check was written to prevent, arriving through the
    one door the check did not cover, so the check now reads the bytes instead of
    the clock.

    What is deliberately not in the hash is the Mojo toolchain. A compiler
    upgrade does not invalidate the binary here, and a stale binary across an
    upgrade is a real hole, but the version is a subprocess away and this
    function runs on every query. Rebuilding after a toolchain change is a
    `--rebuild` away and the report records the toolchain, so the hole is at
    least visible.

    Args:
        home: The firepanda checkout.

    Returns:
        A hex digest over the driver source and the library, path names
        included, so a rename with no edit still counts as a change.
    """
    digest = hashlib.sha256()
    # Every driver source, not just `main.mojo`. The TPC-H queries live in a
    # sibling module, and hashing only the entry point would leave a change to
    # any of the twenty two invisible to this check, which is the exact failure
    # the rest of this docstring is about.
    for path in sorted(DRIVER_DIR.glob("*.mojo")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    library = home / "firepanda"
    for path in sorted(library.rglob("*.mojo")):
        digest.update(str(path.relative_to(library)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build(force: bool = False) -> Path:
    """Compiles the driver and returns the binary.

    A compiled binary rather than `mojo run`, and the reason is memory. `mojo run`
    holds the compiler in the same process as the program, and its resident set
    starts above three hundred megabytes before a single row exists. Reporting
    that as firepanda's memory use would be wrong by more than the thing being
    measured.

    Args:
        force: Whether to rebuild even if the sources are the ones the existing
            binary was built from.

    Returns:
        The binary path.

    Raises:
        SystemExit: If the build fails.
    """
    home = firepanda_home()
    binary = ROOT / "engines" / "firepanda" / "firepanda-driver"
    stamp = binary.with_suffix(".sources")
    fingerprint = source_fingerprint(home)
    if not force and binary.exists() and stamp.exists():
        try:
            if stamp.read_text().strip() == fingerprint:
                return binary
        except OSError:
            pass

    command = [
        "pixi",
        "run",
        "--manifest-path",
        str(home / "pixi.toml"),
        "mojo",
        "build",
        "-I",
        str(home),
        # The driver's own directory, so `main.mojo` can import the TPC-H
        # queries from the module beside it.
        "-I",
        str(DRIVER_DIR),
        str(DRIVER_SOURCE),
        "-o",
        str(binary),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not binary.exists():
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-6:]
        raise SystemExit("cannot build the firepanda driver: " + " | ".join(tail))
    # After the build rather than before it, so a build that fails leaves no
    # stamp and the next run tries again instead of trusting whatever binary an
    # earlier commit left behind.
    stamp.write_text(fingerprint + "\n")
    return binary


def clickbench_support(binary: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    """Asks the driver which ClickBench queries it runs and why it refuses any.

    Cached per binary, because the answer cannot change while the binary does
    not and the alternative is a process launch before every one of 43 queries.

    Args:
        binary: The built driver.

    Returns:
        The supported query names, and a reason per refused one. A driver that
        cannot be asked comes back as supporting nothing, and the caller reports
        that against the query rather than raising.
    """
    key = str(binary)
    if key in _CLICKBENCH_SUPPORT:
        return _CLICKBENCH_SUPPORT[key]
    supported: tuple[str, ...] = ()
    unsupported: dict[str, str] = {}
    try:
        completed = subprocess.run(
            [str(binary), "--list=clickbench"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        listed = json.loads(completed.stdout.strip().splitlines()[-1])
        supported = tuple(listed.get("supported", ()))
        unsupported = dict(listed.get("unsupported", {}))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    _CLICKBENCH_SUPPORT[key] = (supported, unsupported)
    return supported, unsupported


def measure(
    query: str,
    rows: int,
    runs: int,
    suite: str,
    timeout_s: int,
    paths: dict[str, str] | None = None,
    io: str = "memory",
) -> dict:
    """Runs one query in a child process and returns what it measured.

    Args:
        query: The query name.
        rows: How many rows in the large table.
        runs: How many timed runs.
        suite: Which suite is being run.
        timeout_s: How long to wait for the child.
        paths: The file each table lives in, used by the ingestion and TPC-H
            suites and ignored by the one whose data the driver generates.
        io: Whether the data is handed over in memory or scanned from the file.

    Returns:
        A mapping with `ok` and, when true, the timings, memory and answer digest
        inputs. When false, `note` says why.
    """
    if suite == "ingestion":
        if query not in INGESTION_SUPPORTED:
            return {"ok": False, "note": f"firepanda does not implement {query}"}
        if not paths:
            return {"ok": False, "note": "the harness passed no file to read"}
    elif suite == "tpch":
        if io != "memory":
            return {
                "ok": False,
                "note": (
                    f"firepanda cannot run TPC-H with --io {io}: it decodes "
                    "Parquet only by handing the file to DuckDB, which is an "
                    "engine in this table, so the reader being measured would "
                    "not be its own"
                ),
            }
        if query not in TPCH_SUPPORTED:
            return {"ok": False, "note": f"firepanda does not implement {query}"}
        if not paths:
            return {"ok": False, "note": "the harness passed no tables to read"}
    elif suite == "clickbench":
        if io != "memory":
            return {
                "ok": False,
                "note": (
                    f"firepanda cannot run ClickBench with --io {io}: it decodes "
                    "Parquet only by handing the file to DuckDB, which is an "
                    "engine in this table, so the reader being measured would "
                    "not be its own"
                ),
            }
        if not paths:
            return {"ok": False, "note": "the harness passed no table to read"}
    elif suite != "db-benchmark":
        return {
            "ok": False,
            "note": f"firepanda does not run the {suite} suite",
        }
    elif query not in SUPPORTED:
        return {
            "ok": False,
            "note": UNSUPPORTED.get(query, f"firepanda does not implement {query}"),
        }

    try:
        binary = build()
    except SystemExit as exc:
        return {"ok": False, "note": str(exc)}

    # After the build, because the driver is what holds this list and it has to
    # exist before it can be asked.
    if suite == "clickbench":
        supported, unsupported = clickbench_support(binary)
        if query not in supported:
            return {
                "ok": False,
                "note": unsupported.get(query, f"firepanda does not implement {query}"),
            }

    command = [
        str(binary),
        f"--query={query}",
        f"--rows={rows}",
        f"--runs={runs}",
        f"--suite={suite}",
    ]
    if suite == "ingestion" or suite == "clickbench":
        # One path to pass either way. For ingestion it is one file per query,
        # and for ClickBench it is the partition glob, which is what the harness
        # hands every engine for that suite and what DuckDB expands on the way
        # in.
        command.append(f"--path={next(iter(paths.values()))}")
    elif suite == "tpch":
        # One flag per table the query reads. A table it does not read gets no
        # flag, and the driver leaves that frame empty rather than opening a
        # file for the sake of it.
        for table in TPCH_TABLES:
            if table in paths:
                command.append(f"--path-{table}={paths[table]}")
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
