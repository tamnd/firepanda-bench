#!/usr/bin/env python3
"""Checks the ClickBench ports against something other than each other.

`validate_tpch.py` compares every implementation against the validation output the
TPC publishes with the specification. ClickBench publishes no answers, so this is
what stands in its place, and it does two things that the cross engine fingerprint
and the exact check cannot do on their own.

The first is a second opinion. `clickbench_hand.py` is ten of the queries written
again in plain Python over the raw Parquet columns, sharing no planner, no
dataframe library and no type conversion with anything it checks. Four engines
agreeing means four ports made the same decision; this is the only thing here that
can tell a right decision from a popular one. Its answers are checked in under
`suites/clickbench/expected/` so that a change to either side turns up in a diff
rather than in a number.

The second is the tie set. Twelve queries at 1M return ten rows out of a group
that the ordering expression cannot tell apart, so which ten come back is the
engine's choice and not the statement's. Which queries those are is a fact about
the data and changes with the size, so `tools/queries.py` cannot simply be believed
and this recomputes it: every statement is ranked by its own ordering expression,
every boundary it has is inspected, and what comes out is compared against the
registry. A size where the set is different is reported with the numbers in it.

Usage:
    python tools/validate_clickbench.py --engines pandas,polars,duckdb --size 1M
    python tools/validate_clickbench.py --write
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import traceback
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clickbench
import clickbench_hand
import queries as query_registry

import engines as engine_registry

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "data"
SUITE_ROOT = ROOT / "suites" / "clickbench"
EXPECTED = SUITE_ROOT / "expected"

# How far a number may be from the one the hand implementation produced. The only
# floats in these ten answers are the two `AVG(STRLEN(...))` columns, which are a
# sum of small integers divided by a count, so the engines land within a few units
# of the last place of each other and a part in a billion is far looser than that
# and far tighter than any real mistake.
TOLERANCE = 1e-9

# The tail of a statement, which is what says where it cuts. The queries file is
# one statement per line and this is the same shape the port tests use to take a
# published statement's last clause back off.
TAIL = re.compile(
    r"\s+ORDER\s+BY\s+(.*?)(?:\s+LIMIT\s+(\d+))?(?:\s+OFFSET\s+(\d+))?$", re.IGNORECASE
)
LIMIT_ONLY = re.compile(r"\s+LIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?$", re.IGNORECASE)


def statements() -> list[str]:
    """Reads the vendored queries file.

    Returns:
        The 43 statements, one per line, with the semicolon off.
    """
    text = (SUITE_ROOT / "queries.sql").read_text()
    return [line.strip().rstrip(";") for line in text.splitlines() if line.strip()]


def cell(value):
    """Turns one answer value into something that compares and stores cleanly.

    A date stays a date and a timestamp at exactly midnight becomes one, which is
    the same trade `verify.widen` makes and for the same reason: pandas has no date
    dtype, so a DATE column can arrive as either, and no query in this suite
    declares an output type. What it gives up is telling a date apart from a
    timestamp that happens to land on midnight, and both sides give it up together.

    Args:
        value: The value, as the engine or the hand implementation produced it.

    Returns:
        None, a bool, an int, a float or a str.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.date().isoformat()
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def rows_of(table) -> list[tuple]:
    """Reads an Arrow answer as canonical Python rows.

    Args:
        table: The answer.

    Returns:
        One tuple per row, in the order the engine returned them.
    """
    columns = [table.column(name).to_pylist() for name in table.schema.names]
    return [tuple(cell(column[i]) for column in columns) for i in range(table.num_rows)]


def project(rows: list[tuple], columns: tuple[str, ...], wanted: list[str]) -> list[list]:
    """Keeps the named columns and sorts, so two answers compare as multisets.

    Sorted because row order is not part of any answer this repository compares.
    Even a query with an ORDER BY leaves the order of two rows with the same
    ordering key up to the engine, so comparing by position would report a
    difference where the statement permits both.

    Args:
        rows: The rows.
        columns: The names of every column in a row.
        wanted: The names to keep.

    Returns:
        The projection, sorted.
    """
    at = [columns.index(name) for name in wanted]
    return sorted(([row[position] for position in at] for row in rows), key=repr)


def same(left, right) -> bool:
    """Compares two canonical values.

    Args:
        left: One value.
        right: The other.

    Returns:
        Whether they agree, with a tolerance on floats and nothing else.
    """
    if isinstance(left, float) or isinstance(right, float):
        try:
            a, b = float(left), float(right)
        except (TypeError, ValueError):
            return left == right
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return abs(a - b) <= TOLERANCE * max(abs(a), abs(b), 1.0)
    return left == right


def differs(produced: list[list], expected: list[list]) -> str:
    """Finds the first difference between two projections.

    Args:
        produced: What the engine returned.
        expected: What the expected file holds.

    Returns:
        An empty string when they agree, otherwise the first difference.
    """
    if len(produced) != len(expected):
        return f"{len(produced)} rows against {len(expected)}"
    for index, (got, want) in enumerate(zip(produced, expected, strict=True)):
        for position, (a, b) in enumerate(zip(got, want, strict=True)):
            if not same(a, b):
                return f"row {index + 1} column {position + 1}: got {a!r}, expected {b!r}"
    return ""


def expected_for(name: str, hand: clickbench_hand.Hand, size: str, statement: str) -> dict:
    """Builds the expected file for one query out of the hand implementation.

    Args:
        name: The query name.
        hand: What the hand implementation produced.
        size: The dataset size the numbers are for.
        statement: The published statement.

    Returns:
        The document, ready to write.
    """
    answer = [tuple(cell(value) for value in row) for row in hand.answer()]
    ties = hand.ties()
    checked = list(hand.columns) if not ties else [hand.columns[i] for i in hand.key]
    return {
        "query": name,
        "size": size,
        "statement": statement,
        "columns": list(hand.columns),
        "rows_out": len(answer),
        "determined": not ties,
        "ties": {str(cut): count for cut, count in sorted(ties.items())},
        "checked": checked,
        "answer": project(answer, hand.columns, checked) if checked else [],
    }


def note_for(document: dict) -> str:
    """Writes the sentence that says what a file does and does not pin down.

    Args:
        document: The expected file.

    Returns:
        The note.
    """
    if document["determined"]:
        return (
            f"Every value of all {len(document['columns'])} columns over "
            f"{document['rows_out']} rows, computed by tools/clickbench_hand.py "
            f"from the statement above and compared as a multiset, because the "
            f"order of two rows with the same ordering key is the engine's choice."
        )
    if not document["checked"]:
        return (
            "Nothing but the shape. The statement has no ORDER BY, so any ten "
            "groups are a correct answer and the only real check is that the ten "
            "an engine returned are groups that exist with the counts they claim, "
            "which the validator does against the hand implementation rather than "
            "against this file."
        )
    ties = ", ".join(
        f"{count} rows tie across the cut at {cut}" for cut, count in document["ties"].items()
    )
    return (
        f"The {', '.join(document['checked'])} column only, because {ties}, so which "
        f"rows carry those values is the engine's choice and the values themselves "
        f"are not. Membership of each returned row in the full answer is checked "
        f"separately, against the hand implementation rather than against this file."
    )


def load_expected(name: str) -> dict | None:
    """Reads one expected file.

    Args:
        name: The query name.

    Returns:
        The document, or None when there is no file for that query.
    """
    path = EXPECTED / f"{name}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def write_expected(size: str, hits: clickbench_hand.Hits) -> list[str]:
    """Regenerates every expected file from the hand implementation.

    Args:
        size: The dataset size.
        hits: The table.

    Returns:
        The files written.
    """
    lines = statements()
    EXPECTED.mkdir(parents=True, exist_ok=True)
    written = []
    for name, build in clickbench_hand.HAND.items():
        statement = lines[int(name[1:])]
        document = expected_for(name, build(hits), size, statement)
        ordered = {"query": name, "size": size, "note": note_for(document), **document}
        path = EXPECTED / f"{name}.json"
        path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")
        written.append(str(path.relative_to(ROOT)))
    return written


def check_expected(size: str, hits: clickbench_hand.Hits) -> dict[str, str]:
    """Checks the checked in files still say what the hand implementation says.

    The file is the record and the code is the authority, and they are checked
    against each other so that a change to either one is a diff somebody reviews
    rather than a number that moved.

    Args:
        size: The dataset size.
        hits: The table.

    Returns:
        A verdict per query, empty when it agreed.
    """
    lines = statements()
    verdicts = {}
    for name, build in clickbench_hand.HAND.items():
        stored = load_expected(name)
        if stored is None:
            verdicts[name] = "no expected file, run with --write"
            continue
        if stored["size"] != size:
            verdicts[name] = f"the file is for {stored['size']}, this run is {size}"
            continue
        fresh = expected_for(name, build(hits), size, lines[int(name[1:])])
        for field in ("statement", "columns", "rows_out", "determined", "ties", "checked"):
            if stored[field] != fresh[field]:
                verdicts[name] = f"{field}: file has {stored[field]!r}, hand gives {fresh[field]!r}"
                break
        else:
            verdicts[name] = differs(fresh["answer"], stored["answer"])
    return verdicts


def check_engine(name: str, size: str, hits: clickbench_hand.Hits) -> dict[str, str]:
    """Runs the ten on one engine and checks them against the expected files.

    Three things per query. The column names and the row count, which every
    statement determines. The values of the columns the statement determines, from
    the expected file. And, for the queries it does not determine, that every row
    the engine returned is a row of the full answer, which is what catches an engine
    that invented a row rather than picking a different legitimate one out of a tie.

    Args:
        name: The engine name.
        size: The dataset size.
        hits: The table, for the hand implementation.

    Returns:
        A verdict per query, empty when it agreed.
    """
    module = engine_registry.load_engine(name)
    runners = engine_registry.query_map(module, "clickbench")
    pattern = str(DATA_ROOT / "clickbench" / size / clickbench.PARTITION_GLOB.format(table="hits"))
    context = module.load({"hits": pattern}, suite="clickbench", io="memory")

    verdicts = {}
    for query, build in clickbench_hand.HAND.items():
        stored = load_expected(query)
        if stored is None:
            verdicts[query] = "no expected file, run with --write"
            continue
        runner = runners.get(query)
        if runner is None:
            verdicts[query] = f"{name} does not implement it"
            continue
        try:
            table = engine_registry.as_arrow(runner(context))
            verdicts[query] = compare_answer(table, stored, build, hits)
        except Exception:
            verdicts[query] = traceback.format_exc(limit=3).strip().replace("\n", " | ")
    return verdicts


def compare_answer(table, stored: dict, build, hits: clickbench_hand.Hits) -> str:
    """Compares one engine answer against one expected file.

    Args:
        table: The engine's answer.
        stored: The expected file.
        build: The hand implementation, for the membership check.
        hits: The table.

    Returns:
        An empty string when it agreed, otherwise the first difference.
    """
    columns = tuple(table.schema.names)
    if list(columns) != stored["columns"]:
        return f"columns {list(columns)} against {stored['columns']}"
    if table.num_rows != stored["rows_out"]:
        return f"{table.num_rows} rows against {stored['rows_out']}"
    rows = rows_of(table)
    if stored["checked"]:
        wrong = differs(project(rows, columns, stored["checked"]), stored["answer"])
        if wrong:
            return wrong
    if stored["determined"]:
        return ""
    hand = build(hits)
    canonical = {tuple(cell(value) for value in row) for row in hand.rows}
    missing = [row for row in rows if row not in canonical]
    if missing:
        return f"{len(missing)} rows are not in the answer at all, first {missing[0]!r}"
    return ""


def tie_report(size: str) -> tuple[dict[str, dict], list[str]]:
    """Recomputes which statements the data does not determine an answer for.

    Ranks each answer by the statement's own ordering expression and looks at every
    boundary the statement cuts on, which is two when it has an OFFSET. A boundary
    ties when the rank of the last kept row is the rank of the first dropped one,
    and the count is how many rows share it.

    The rank goes on the end of the select list rather than the front, because these
    statements order by an alias the select list defines and an alias cannot be
    referenced before it exists.

    Args:
        size: The dataset size.

    Returns:
        The tie counts per query, and the differences against `tools/queries.py`.
    """
    module = engine_registry.load_engine("duckdb")
    pattern = str(DATA_ROOT / "clickbench" / size / clickbench.PARTITION_GLOB.format(table="hits"))
    connection = module.load({"hits": pattern}, suite="clickbench", io="scan")["con"]

    found: dict[str, dict] = {}
    for index, line in enumerate(statements()):
        name = f"q{index}"
        tail = TAIL.search(line)
        if not tail:
            only = LIMIT_ONLY.search(line)
            if only:
                found[name] = {"ties": {}, "unordered": True}
            continue
        if not tail.group(2):
            continue
        head, order = line[: tail.start()], tail.group(1)
        limit, offset = int(tail.group(2)), int(tail.group(3) or 0)
        at = head.index(" FROM hits")
        ranked = head[:at] + f", RANK() OVER (ORDER BY {order}) AS fp_rnk" + head[at:]
        ranks = [
            row[0]
            for row in connection.execute(
                f"SELECT fp_rnk FROM ({ranked}) fp_t ORDER BY fp_rnk"
            ).fetchall()
        ]
        cuts = ([offset] if offset else []) + [offset + limit]
        ties = {
            cut: ranks.count(ranks[cut])
            for cut in cuts
            if 0 < cut < len(ranks) and ranks[cut - 1] == ranks[cut]
        }
        found[name] = {"ties": ties, "rows": len(ranks)}

    declared = set(query_registry.CLICKBENCH_UNDETERMINED)
    measured = {name for name, entry in found.items() if entry["ties"] or entry.get("unordered")}
    drift = []
    for name in sorted(measured - declared, key=lambda n: int(n[1:])):
        counts = found[name]["ties"]
        drift.append(
            f"{name} is not in CLICKBENCH_UNDETERMINED and does not determine its "
            f"answer at {size}: " + ", ".join(f"{v} rows tie at {k}" for k, v in counts.items())
        )
    for name in sorted(declared - measured, key=lambda n: int(n[1:])):
        drift.append(
            f"{name} is in CLICKBENCH_UNDETERMINED and does determine its answer at "
            f"{size}, so it is being compared more weakly than it needs to be"
        )
    return found, drift


def main(argv: list[str] | None = None) -> int:
    """Validates the ClickBench ports from the command line.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        Zero when every check passed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engines", default="pandas,polars,duckdb")
    parser.add_argument("--size", default="1M")
    parser.add_argument(
        "--write",
        action="store_true",
        help="regenerate the expected files from the hand implementation instead of "
        "checking against them. The diff is the review",
    )
    parser.add_argument("--skip-ties", action="store_true")
    args = parser.parse_args(argv)

    if args.size not in clickbench.SIZES:
        raise SystemExit(f"unknown size '{args.size}'. Known: {', '.join(clickbench.SIZES)}")
    root = DATA_ROOT / "clickbench" / args.size
    if not clickbench.partitions(str(root / "hits_*.parquet")):
        raise SystemExit(
            f"no ClickBench data at {root}. Run: pixi run data --suite clickbench "
            f"--size {args.size}"
        )

    hits = clickbench_hand.Hits(root)
    if args.write:
        for path in write_expected(args.size, hits):
            print(f"wrote {path}")
        return 0

    failures = 0

    if not args.skip_ties:
        print(f"which statements the data determines an answer for, at {args.size}")
        found, drift = tie_report(args.size)
        for name in sorted(found, key=lambda n: int(n[1:])):
            entry = found[name]
            if entry.get("unordered"):
                print(f"  {name:<4} no ORDER BY at all, so any ten rows are an answer")
            elif entry["ties"]:
                counts = ", ".join(f"{v} rows tie at {k}" for k, v in entry["ties"].items())
                print(f"  {name:<4} {counts}")
        for line in drift:
            print(f"  DRIFT: {line}")
        failures += len(drift)

    print(f"\nthe ten hand written answers against suites/clickbench/expected at {args.size}")
    for query, verdict in check_expected(args.size, hits).items():
        if verdict:
            print(f"  {query:<4} STALE: {verdict[:200]}")
            failures += 1
        else:
            print(f"  {query:<4} ok")

    for name in [engine.strip() for engine in args.engines.split(",")]:
        print(f"\n{name} against the ten hand written answers at {args.size}")
        for query, verdict in check_engine(name, args.size, hits).items():
            if not verdict:
                print(f"  {query:<4} ok")
            elif "does not implement" in verdict:
                print(f"  {query:<4} skipped: {verdict}")
            else:
                print(f"  {query:<4} WRONG: {verdict[:200]}")
                failures += 1

    if failures:
        print(f"\n{failures} checks did not pass", file=sys.stderr)
        return 1
    print("\nevery port reproduces the hand written answers, and the tie set is unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
