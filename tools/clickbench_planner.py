#!/usr/bin/env python3
"""The 43 queries run twice, once written by hand and once through the planner.

M2c measured what a planner is worth on TPC-H by writing the plans by hand and
comparing. ClickBench is the other half of that measurement and a more awkward
one, because there are no joins here at all: everything a planner does about join
order, build sides and predicate transfer is inert on all 43. What is left is the
part nobody writes a paper about, which is reading three columns out of 105,
applying five predicates in the order that discards the most rows soonest, and not
computing a sort whose result is thrown away.

Both routes run in the firepanda driver over the same loaded table. The hand
written route is `engines/firepanda/clickbench.mojo`, which is what the published
table reports, with the projections tight and the predicates in an order somebody
chose. The planner route hands the SQL front end the published statement and lets
it choose, and parsing and planning are inside the timed region, because a planner
that is not paid for is not being measured.

One process per query per route, which is what the harness does everywhere else
and for the same reason: a peak is per process, and an engine that has already
allocated and freed the answer to q23 is not in the state the next query would
find it in.

A query the SQL layer does not run yet is reported as a refusal with the sentence
the layer gave, and left out of both totals. A total over a different set of
queries on each side is not a comparison.

Usage:
    python tools/clickbench_planner.py --size 1M
    python tools/clickbench_planner.py --size 1M --runs 5 --json planner.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import clickbench  # noqa: E402
import validate_clickbench  # noqa: E402
from clickbench_load import pattern_for  # noqa: E402

STATEMENTS = ROOT / "suites" / "clickbench" / "queries.sql"
"""The vendored statements, which is what the planner route is handed. The hand
written route is compiled into the driver, so the two are only the same queries
because `tools/validate_clickbench.py` says they are."""


def run_one(binary: Path, query: str, pattern: str, runs: int, planner: bool) -> dict:
    """Runs one query one way and returns what the driver printed.

    Args:
        binary: The built driver.
        query: The query name.
        pattern: The partition glob.
        runs: How many times to run it inside the process.
        planner: True for the SQL route, False for the hand written one.

    Returns:
        The driver's JSON line, parsed, or a record saying why there is not one.
    """
    command = [
        str(binary),
        "--suite=clickbench",
        f"--query={query}",
        f"--path={pattern}",
        f"--runs={runs}",
    ]
    if planner:
        command.append(f"--statements={STATEMENTS}")
    finished = subprocess.run(command, capture_output=True, text=True, check=False)
    if finished.returncode != 0:
        tail = (finished.stderr or "").strip().splitlines()
        return {"ok": False, "note": tail[-1] if tail else "the driver exited"}
    lines = finished.stdout.strip().splitlines()
    if not lines:
        return {"ok": False, "note": "the driver printed nothing"}
    return json.loads(lines[-1])


def seconds(answer: dict) -> float:
    """Returns the median run out of what the driver reported.

    Args:
        answer: The driver's JSON line.

    Returns:
        The median wall clock, in seconds.
    """
    return statistics.median(run["wall_s"] for run in answer["runs"])


def digest(answer: dict) -> tuple:
    """Reduces one answer to something the two routes can be compared on.

    Not the column names. The hand written route names a count `count_star()`
    and the SQL route names it after the expression it was written as, and the
    two are the same number under two names. What has to agree is the shape and
    the values, so the names are dropped and the sums and the hashes are sorted.

    Args:
        answer: The driver's JSON line.

    Returns:
        The row count, the column count, the sums and the hashes.
    """
    return (
        answer["rows_out"],
        answer["cols_out"],
        tuple(sorted(answer.get("sums", {}).values())),
        tuple(sorted(answer.get("hashes", {}).values())),
    )


def agree(hand: dict, planned: dict) -> bool:
    """Whether the two routes answered the same thing.

    A sum is a float and the two routes add the same numbers in the same order,
    so they are compared for equality rather than within a tolerance, and a
    difference in the last bit is a difference worth seeing.

    Args:
        hand: The hand written route's answer.
        planned: The planner route's answer.

    Returns:
        True when the shape, the sums and the hashes all agree.
    """
    return digest(hand) == digest(planned)


def measure(binary: Path, pattern: str, runs: int) -> list[dict]:
    """Runs all 43 both ways.

    Args:
        binary: The built driver.
        pattern: The partition glob.
        runs: How many times to run each query inside its process.

    Returns:
        One record per query.
    """
    out = []
    for number in range(len(validate_clickbench.statements())):
        query = f"q{number}"
        hand = run_one(binary, query, pattern, runs, False)
        planned = run_one(binary, query, pattern, runs, True)
        record = {"query": query}
        if hand.get("ok"):
            record["hand_s"] = seconds(hand)
            record["hand_rows"] = hand["rows_out"]
        else:
            record["hand_note"] = hand.get("note", "")
        if planned.get("ok"):
            record["planner_s"] = seconds(planned)
            record["planner_rows"] = planned["rows_out"]
        else:
            record["planner_note"] = planned.get("note", "")
        if hand.get("ok") and planned.get("ok"):
            record["agree"] = agree(hand, planned)
        out.append(record)
        print(f"  {query} done", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    """Runs the pair and prints the table.

    Args:
        argv: The arguments, or None for the command line.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", default="1M", choices=sorted(clickbench.SIZES))
    parser.add_argument("--runs", type=int, default=3, help="runs per query per route")
    parser.add_argument("--json", default=None, help="also write the measurements here")
    args = parser.parse_args(argv)

    from engines import firepanda_engine

    binary = firepanda_engine.build()
    measured = measure(binary, pattern_for(args.size), args.runs)

    both = [r for r in measured if "hand_s" in r and "planner_s" in r]
    hand_total = sum(r["hand_s"] for r in both)
    planner_total = sum(r["planner_s"] for r in both)
    argued = [r["query"] for r in measured if r.get("agree") is False]

    print(f"the 43 queries at {args.size}, median of {args.runs}, in milliseconds")
    print(f"{'query':<8}{'hand':>10}{'planner':>10}{'ratio':>9}  {'note':<40}")
    for record in measured:
        hand = record.get("hand_s")
        planned = record.get("planner_s")
        note = record.get("planner_note") or record.get("hand_note") or ""
        if hand is None or planned is None:
            print(
                f"{record['query']:<8}"
                f"{(hand * 1000 if hand is not None else 0):>10.1f}"
                f"{'refused':>10}"
                f"{'':>9}  {note[:60]}"
            )
            continue
        rows = ""
        if record["hand_rows"] != record["planner_rows"]:
            rows = f"{record['hand_rows']} rows by hand against {record['planner_rows']}"
        elif record.get("agree") is False:
            rows = "the two routes do not answer the same thing"
        print(
            f"{record['query']:<8}"
            f"{hand * 1000:>10.1f}"
            f"{planned * 1000:>10.1f}"
            f"{planned / hand:>9.2f}  {rows}"
        )
    print(
        f"{'total':<8}{hand_total * 1000:>10.1f}{planner_total * 1000:>10.1f}"
        f"{planner_total / hand_total:>9.2f}  over the {len(both)} both routes run"
    )
    if argued:
        print(f"the routes disagree on {', '.join(argued)}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "size": args.size,
                    "runs": args.runs,
                    "queries": measured,
                    "hand_total_s": hand_total,
                    "planner_total_s": planner_total,
                    "both": len(both),
                },
                indent=2,
            )
            + "\n"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
