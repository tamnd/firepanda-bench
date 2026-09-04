#!/usr/bin/env python3
"""Rejects result files that cannot say what produced them.

A benchmark result is only evidence if it names the engines, their versions, the
machine and the Mojo toolchain that compiled firepanda. The Mojo ABI is not
stable and its codegen changes release to release, so a timing without a
toolchain version is not reproducible even in principle.

It also has to say how the data reached each engine. A run where every engine was
handed the same in memory tables and a run where two of them read the Parquet
inside the timed region are different measurements, and a file that does not say
which one it is cannot be compared against anything.

This runs in CI on every pull request. It is the one piece of the harness that is
worth having before the harness exists.

Usage:
    python tools/validate_results.py results
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED = {
    "suite": "which suite",
    "size": "at which size or scale factor",
    "io": "whether engines were handed memory or a file to scan",
    "engines": "every engine with its exact version",
    "mojo_version": "the toolchain that compiled firepanda; the ABI is not stable",
    "firepanda_ref": "the commit, so a regression is attributable",
    "machine": "instance type, physical core count, RAM",
    "runs": "how many runs the median was taken over",
    "results": "per query: median, IQR, peak RSS, cache state",
    "agreement": "whether the engines produced the same answers",
}

PER_RESULT = ("median_s", "iqr_s", "peak_rss_bytes", "cache")

# Anything else that lives in the results directory. `env.json` is written by
# `env_report.py` and is not a result file.
NOT_RESULTS = ("env.json",)


def check(path: Path) -> list[str]:
    """Finds everything wrong with one result file.

    Args:
        path: The file.

    Returns:
        The problems, empty if there are none.
    """
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"not valid JSON: {exc}"]

    problems = [f"missing '{key}' ({why})" for key, why in REQUIRED.items() if key not in doc]
    if problems:
        return problems

    if not isinstance(doc["engines"], dict) or not doc["engines"]:
        problems.append("'engines' must be a non-empty mapping of engine name to version")
    else:
        unpinned = [name for name, ver in doc["engines"].items() if not ver]
        if unpinned:
            problems.append(f"engines with no recorded version: {', '.join(sorted(unpinned))}")

    # Present but empty passes the check above, and for a while every file
    # written outside CI was exactly that: a firepanda column with no commit and
    # no toolchain behind it. Only demanded when firepanda ran, because a
    # pandas against Polars run on a machine with no Mojo on it owes neither.
    if "firepanda" in (doc.get("engines") or {}):
        for key in ("firepanda_ref", "mojo_version"):
            if not str(doc.get(key) or "").strip():
                problems.append(f"'{key}' is empty, so the firepanda numbers name nothing")

    if doc["io"] not in ("memory", "scan"):
        problems.append(f"io is '{doc['io']}', which is not a mode the harness runs")

    # A single number with no spread is not a measurement.
    for name, entry in (doc.get("results") or {}).items():
        if not entry.get("ok"):
            # A pairing that did not run has to say why, and that is all it owes.
            if not entry.get("note"):
                problems.append(f"result '{name}' failed without saying why")
            continue
        missing = [k for k in PER_RESULT if k not in entry]
        if missing:
            problems.append(f"result '{name}' missing {', '.join(missing)}")

    # A disagreement is allowed to be in the file. Publishing it as a comparison
    # is not, and the report is what enforces that, so this only insists the file
    # records the verdict rather than leaving the reader to work it out.
    for query, verdict in (doc.get("agreement") or {}).items():
        if "agreed" not in verdict or "by_engine" not in verdict:
            problems.append(f"agreement for '{query}' does not say who produced what")

    # Optional, because it is only there when somebody ran with --verify exact.
    # When it is there it is a much stronger claim than the fingerprint, and a
    # stronger claim needs to say what it was checked against: a verdict is a
    # statement about two answers under one definition of sameness, and that
    # definition is a commit of the compat comparison layer.
    #
    # A check that could not run is allowed to be in the file and only owes a
    # reason, the same deal a pairing that did not run gets.
    verification = doc.get("verification")
    if verification is not None:
        if "agreed" not in verification:
            problems.append("'verification' does not say whether the answers agreed")
        if not verification.get("ran"):
            if not verification.get("note"):
                problems.append("'verification' did not run and does not say why")
        else:
            if "queries" not in verification:
                problems.append("'verification' ran and names no queries")
            if not str((verification.get("compat") or {}).get("revision") or "").strip():
                problems.append(
                    "'verification' does not name the compat revision it was checked against"
                )

    if doc.get("runs", 0) < 3:
        problems.append(f"runs={doc.get('runs')} is too few to report a median over")

    return problems


def main(argv: list[str]) -> int:
    """Validates every result file under a directory.

    Args:
        argv: The command line, whose second element is the directory.

    Returns:
        A process exit status.
    """
    root = Path(argv[1] if len(argv) > 1 else "results")
    files = [p for p in sorted(root.glob("*.json")) if p.name not in NOT_RESULTS]
    if not files:
        print(f"no result files under {root}/ yet")
        return 0

    failed = False
    for path in files:
        problems = check(path)
        if problems:
            failed = True
            for problem in problems:
                print(f"::error file={path}::{problem}")
        else:
            print(f"ok   {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
