#!/usr/bin/env python3
"""Reject result files that cannot say what produced them.

A benchmark result is only evidence if it names the engines, their versions, the
machine and the Mojo toolchain that compiled firepanda. The Mojo ABI is not stable
and its codegen changes release to release, so a timing without a toolchain version
is not reproducible even in principle.

This runs in CI on every pull request. It is the one piece of the harness that is
worth having before the harness exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED = {
    "suite": "which suite, at which size or scale factor",
    "engines": "every engine with its exact version",
    "mojo_version": "the toolchain that compiled firepanda; the ABI is not stable",
    "firepanda_ref": "the commit, so a regression is attributable",
    "machine": "instance type, physical core count, RAM",
    "runs": "how many runs the median was taken over",
    "results": "per query: median, IQR, peak RSS, cache state",
}

PER_RESULT = ("median_s", "iqr_s", "peak_rss_bytes", "cache")


def check(path: Path) -> list[str]:
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

    # A single number with no spread is not a measurement.
    for name, entry in (doc.get("results") or {}).items():
        missing = [k for k in PER_RESULT if k not in entry]
        if missing:
            problems.append(f"result '{name}' missing {', '.join(missing)}")

    if doc.get("runs", 0) < 3:
        problems.append(f"runs={doc.get('runs')} is too few to report a median over")

    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "results")
    files = sorted(root.glob("*.json"))
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
