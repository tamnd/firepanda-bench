#!/usr/bin/env python3
"""The exact answer check, run after the timing and never inside it.

Every published run compares answers with a fingerprint: the row count, the sum of
each numeric column and an order independent FNV-1a digest of each text column.
That check is on the timed path and it has to be, because the Mojo driver computes
it itself without an Arrow sort available, and it is cheap enough to leave on.

It is also weak in one specific way. It reduces every column on its own, so it
knows the multiset of values in each column and nothing about which row each value
sits on. Two answers with the same row count, the same column sums and the same
multiset of strings can still pair the wrong name with the wrong total. A join on
the wrong key produces exactly that, and the fingerprint calls it agreement.

So this exists. Each engine writes its answer to Arrow IPC after the last timed
run, and this reads them back and hands them to `fpcompat.compare`, the comparison
layer the conformance oracle runs on, which sorts both answers by every column and
then compares them row by row and value by value. That check cannot be fooled by a
permutation, and it is far too slow and far too memory hungry to sit on the timed
path, which is why it is off by default and why it runs once per release.

It needs a firepanda-compat checkout because it imports from it. There is no pip
package for it on purpose: the comparison layer is the thing that decides whether
two answers are the same answer, and a vendored copy of that in a second
repository is two definitions of correctness waiting to disagree. Point this at a
checkout with `--compat`, or set FIREPANDA_COMPAT, or leave it and it looks for a
sibling directory. Whichever it finds, its commit goes in the output, because a
verified answer is only verified against a particular idea of what verified means.

Usage:
    python tools/verify.py --answers results/answers/db-benchmark/0.5GB
    python tools/verify.py --answers results/answers --compat ../firepanda-compat
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

ROOT = Path(__file__).resolve().parent.parent

# Where a checkout is looked for when nobody says. A sibling of this repository,
# which is where it is on every machine that has both.
DEFAULT_COMPAT = ROOT.parent / "firepanda-compat"
COMPAT_ENV = "FIREPANDA_COMPAT"

# The engine every other engine is compared against. pandas, for the same reason it
# is the oracle in the conformance suite: it is the API being reimplemented, so a
# difference from it is the difference a user would experience. When a run did not
# include pandas the first engine alphabetically stands in, and the output says so
# rather than letting a reader assume the reference was pandas.
REFERENCE = "pandas"

# The float tolerance class each query is compared under, by suite and query name.
#
# ACCUMULATION for everything, and not the stricter SINGLE, because every answer in
# this harness is a sum or a mean over millions of rows and the engines add in
# different orders. A parallel sum that reproduced a serial one bit for bit would
# not be a parallel sum, and demanding that would be demanding that every engine be
# slow in the same way.
#
# STATISTICAL for the one query that is a correlation. It is computed from the
# moments in every engine here, which subtracts quantities of similar size, and the
# last bits of that are not a fact about anything.
DEFAULT_TOLERANCE = "ACCUMULATION"
TOLERANCE: dict[tuple[str, str], str] = {
    ("db-benchmark", "q9"): "STATISTICAL",
}

# Why the comparison sorts before it compares, in the words the verdict prints.
ORDER_REASON = (
    "every engine here is told not to sort: pandas runs its group by with "
    "sort=False and the others are hash aggregations that have no order to give "
    "up, so row order is not part of any answer in this harness and both sides "
    "are sorted by every column before they are compared"
)

TOLERANCE_REASON = (
    "the answers are sums and means over millions of rows and the engines "
    "accumulate in different orders, so the last bits differ by arithmetic rather "
    "than by disagreement"
)

# Columns whose values are bounded, and the size below which a value in them is
# zero. A closed list with a column name and a number in it, because this is the
# one place the exact check is deliberately not exact and it should be as hard to
# add to as it was to add the first one.
#
# There is exactly one entry. db-benchmark q9 is a squared correlation, computed
# from the moments in every engine here because the alternative in pandas is a
# Python level apply that would measure the interpreter. Computing it that way
# subtracts quantities near 1e16 to get a numerator near 1, so a group whose true
# correlation is zero comes out as whatever the rounding left behind, and the
# engines leave behind different things: pandas gets 0.0 and Polars gets 1e-35.
# Both of those are zero. A relative tolerance cannot say so, because the relative
# difference between 1e-35 and 0.0 is 1, and that is not a defect in the tolerance:
# a relative tolerance is a demand for bitwise equality at zero and it should be,
# everywhere the zero is a real answer rather than a cancellation residue.
#
# So the rule is stated in terms of the column rather than the tolerance. r2 is a
# squared correlation and lives in [0, 1], so 1e-12 is small relative to the only
# scale the column has, and anything below it is compared as zero on both sides.
# What this gives up is the ability to tell a true 1e-13 from a true zero in that
# column, and there is no such thing: the cancellation floor is around 1e-32, so
# every value between there and 1e-12 is noise in both engines already.
NEAR_ZERO: dict[tuple[str, str], dict[str, float]] = {
    ("db-benchmark", "q9"): {"r2": 1e-12},
}

# Cross engine differences that are real, understood and not bugs, each with the
# looser tolerance class it needs and the sentence saying why it needs one.
#
# This works the way the compat divergence registry does and for the same reason.
# A difference that is explained is not a failure, and a difference that is
# tolerated without being explained is a failure nobody will ever look at again. So
# an entry names an engine and a suite, it names the class the comparison is redone
# under when the strict one fails, and it carries a reason. Anything larger than
# that class is still a disagreement, so the entry cannot grow to cover a bug that
# arrives later. And an entry that stops being needed is reported as stale, because
# a registry of known differences that is never pruned becomes a list of excuses.
#
# There is one entry. Polars answers the TPC-H revenue queries in the third and
# fourth decimal place of a number in the millions, and pandas and DuckDB agree
# with each other against it. The cause is decimal scale: `l_extendedprice` and
# `l_discount` are both DECIMAL(15,2), the specification's arithmetic gives their
# product scale 4, and Polars brings the product back to scale 2 before summing it
# while DuckDB keeps the wider scale and pandas is in float64 and keeps everything.
# Every engine still reproduces the specification's published validation output,
# which is checked separately by validate_tpch.py at a relative tolerance of 1e-6,
# and the difference here is around 5e-9. So this is not a wrong answer, it is two
# defensible readings of decimal multiplication, and it is worth knowing about
# because no check in this repository could see it before: the fingerprint compares
# column sums at 1e-7, which is a hundred times looser than the difference.
KNOWN: dict[tuple[str, str], tuple[str, str]] = {
    ("tpch", "polars"): (
        "STATISTICAL",
        "Polars rounds a decimal product back to the scale of its operands, so the "
        "TPC-H money columns differ from pandas and DuckDB at about five parts in a "
        "billion. All three still reproduce the specification's published answers, "
        "which validate_tpch.py checks at a thousand times this size.",
    ),
}


def compat_root(given: str | None) -> Path:
    """Finds the firepanda-compat checkout to import the comparison layer from.

    Args:
        given: What `--compat` said, or None.

    Returns:
        The directory.

    Raises:
        SystemExit: If there is nothing there, with the three ways to fix it.
    """
    candidate = Path(given) if given else Path(os.environ.get(COMPAT_ENV, DEFAULT_COMPAT))
    if not (candidate / "fpcompat" / "compare.py").is_file():
        raise SystemExit(
            f"no firepanda-compat checkout at {candidate}. The exact check imports its "
            f"comparison layer rather than carrying a copy, so it needs one: pass "
            f"--compat, set {COMPAT_ENV}, or clone it next to this repository with "
            f"git clone https://github.com/tamnd/firepanda-compat.git"
        )
    return candidate.resolve()


def load_compare(root: Path):
    """Imports `fpcompat.compare` out of a checkout.

    Args:
        root: The checkout.

    Returns:
        The module.
    """
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import fpcompat.compare as module

    return module


def compat_revision(root: Path) -> str:
    """Reads the commit the comparison layer came from.

    A verdict is a claim about two answers under one definition of sameness, and
    that definition has a version. Recording it means a verdict from six months ago
    can be read, and a verdict that changed can be attributed.

    Args:
        root: The checkout.

    Returns:
        The short commit, or `unknown` when it is not a git checkout.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return out.stdout.strip() or "unknown"


def widen(table: pa.Table) -> pa.Table:
    """Casts away the representation choices an engine is allowed to make.

    Four of them, and each one is a hole in this check as well as a fix for a false
    alarm, so each is named here rather than being a line in a cast.

    **Integer width.** No query in this suite declares an output type, so a count
    that comes back as int32 from one engine and int64 from another is the same
    answer written down differently. Everything integer becomes int64, and the cast
    is checked, so a uint64 too large to fit raises here instead of wrapping into a
    negative number that would then compare unequal for the wrong reason. What this
    gives up is the ability to notice an engine that silently narrows a column, and
    that is a thing worth noticing, so it belongs in a schema check rather than
    being smuggled into an answer check.

    **Float width.** Same argument, and float32 against float64 would fail on the
    seventh digit rather than on the answer.

    **Decimal.** Two different things wear this type and they are treated
    differently. A decimal with a scale is money: TPC-H carries DECIMAL(15,2) and
    DuckDB and Polars keep it, while pandas has to fall back to float64 because
    Arrow will not give it the exact arithmetic, so the comparison happens in the
    wider and lossier of the two and the tolerance class is what makes that
    survivable. A decimal with no scale is an integer wearing a wider coat: DuckDB
    sums an INTEGER column into a HUGEINT and hands back DECIMAL(38,0), which is
    the sum of some integers and nothing else, so it becomes int64 like every other
    integer. That cast is checked, and a sum too large for int64 falls back to
    float64 rather than raising, which turns an unlikely overflow into a reported
    dtype difference instead of a crashed check.

    **Dictionary encoding.** An engine may hand back a grouped key column dictionary
    encoded and another may not, and the values are the same either way. The
    comparison layer already compares categorical values through their categories,
    so this only matters when one side is a plain string column, which is the common
    case here.

    **Date against timestamp.** pandas has no date dtype. A Parquet DATE comes back
    from it as datetime64 at midnight, while DuckDB and Polars keep it as a date,
    which is TPC-H `o_orderdate` in three queries. Dates become timestamps, which is
    exact in that direction, and a real time of day on one side still differs
    because midnight is a value rather than a wildcard.

    Args:
        table: The answer as it was written.

    Returns:
        The same answer, widened.
    """
    columns = []
    for field in table.schema:
        column = table.column(field.name)
        kind = field.type
        if pa.types.is_dictionary(kind):
            column = pc.cast(column, kind.value_type)
            kind = column.type
        if pa.types.is_decimal(kind) and kind.scale == 0:
            try:
                column = pc.cast(column, pa.int64())
            except pa.ArrowInvalid:
                column = pc.cast(column, pa.float64())
            kind = column.type
        if pa.types.is_integer(kind):
            column = pc.cast(column, pa.int64())
        elif pa.types.is_floating(kind) or pa.types.is_decimal(kind):
            column = pc.cast(column, pa.float64())
        elif pa.types.is_date(kind):
            column = pc.cast(column, pa.timestamp("us"))
        columns.append(column)
    return pa.Table.from_arrays(columns, names=table.column_names)


def snap(table: pa.Table, floors: dict[str, float]) -> pa.Table:
    """Replaces cancellation residue with the zero it is standing in for.

    Applied to both sides, to the declared columns only, and nowhere else. See
    `NEAR_ZERO` for why there is one entry in that table and what it costs.

    Args:
        table: The answer.
        floors: The floor per column name, for the columns that have one.

    Returns:
        The answer with those columns snapped.
    """
    if not floors:
        return table
    columns = []
    for name in table.column_names:
        column = table.column(name)
        floor = floors.get(name)
        if floor is not None and pa.types.is_floating(column.type):
            column = pc.if_else(pc.less(pc.abs(column), floor), 0.0, column)
        columns.append(column)
    return pa.Table.from_arrays(columns, names=table.column_names)


def read_answer(path: Path, floors: dict[str, float] | None = None) -> pa.Table:
    """Reads one Arrow IPC answer file.

    Args:
        path: The file.
        floors: The near zero floors declared for this query, if any.

    Returns:
        The table, widened and snapped.
    """
    with pa.ipc.open_file(path) as reader:
        return snap(widen(reader.read_all()), floors or {})


def collect(root: Path, suite: str = "") -> dict[tuple[str, str], dict[str, Path]]:
    """Finds every answer file under a directory.

    The layout `run.py` writes is `<suite>/<size>/<query>/<engine>.arrow`, and this
    walks for the files rather than assuming the depth, so pointing it at one suite,
    one size or the lot all work.

    The suite matters, because it is half of the key that chooses the tolerance and
    the near zero floors. A run passes it explicitly, since it is pointing at a
    directory that is already inside the suite and has nothing above it to read the
    name off. Anyone running this by hand over `results/answers` gets it from the
    path, and a directory shallow enough to have neither is compared under the
    default tolerance, which is the strictest of the two.

    Args:
        root: Where to look.
        suite: The suite, when the caller knows it. Overrides the path.

    Returns:
        A mapping from (suite, query) to the file each engine wrote.
    """
    found: dict[tuple[str, str], dict[str, Path]] = {}
    for path in sorted(root.rglob("*.arrow")):
        parts = path.relative_to(root).parts
        if len(parts) < 2:
            continue
        query = parts[-2]
        named = suite or (parts[0] if len(parts) > 2 else "unknown")
        found.setdefault((named, query), {})[path.stem] = path
    return found


def reference_engine(engines: dict[str, Path]) -> str:
    """Picks the engine everything else is compared against.

    Args:
        engines: The answer file per engine.

    Returns:
        The engine name.
    """
    return REFERENCE if REFERENCE in engines else sorted(engines)[0]


def verify_query(compare, suite: str, query: str, engines: dict[str, Path]) -> dict:
    """Compares every engine's answer for one query against the reference.

    Args:
        compare: The `fpcompat.compare` module.
        suite: The suite name, which chooses the tolerance.
        query: The query name.
        engines: The answer file per engine.

    Returns:
        A verdict document for the query.
    """
    tolerance = compare.Tolerance[TOLERANCE.get((suite, query), DEFAULT_TOLERANCE)]
    rules = compare.Rules(
        tolerance=tolerance,
        relaxations=frozenset({"grouped_order"}),
        reason=f"{ORDER_REASON}. And {TOLERANCE_REASON}",
    )
    floors = NEAR_ZERO.get((suite, query), {})
    base = reference_engine(engines)
    result: dict = {
        "reference": base,
        "rows": 0,
        "engines": {},
        "agreed": True,
        "tolerance": tolerance.name,
    }
    if floors:
        result["near_zero"] = floors
    try:
        left = read_answer(engines[base], floors)
    except Exception as exc:
        result["agreed"] = False
        result["engines"][base] = {"equal": False, "differences": [f"unreadable: {exc}"]}
        return result
    result["rows"] = left.num_rows

    for name in sorted(engines):
        if name == base:
            result["engines"][name] = {"equal": True, "differences": []}
            continue
        try:
            right = read_answer(engines[name], floors)
        except Exception as exc:
            result["agreed"] = False
            result["engines"][name] = {"equal": False, "differences": [f"unreadable: {exc}"]}
            continue
        verdict = compare.compare(left, right, rules)
        entry = {
            "equal": bool(verdict.equal),
            "differences": list(verdict.differences),
            "extra": verdict.extra,
        }
        if not verdict.equal and (suite, name) in KNOWN:
            # A registered difference is compared again under the class it says it
            # needs. Passing then means this is the difference the registry
            # describes; failing means it is a larger one, and a larger one is not
            # covered by an entry about a smaller one.
            looser, reason = KNOWN[(suite, name)]
            again = compare.compare(left, right, replace_tolerance(compare, rules, looser))
            if again.equal:
                entry = {
                    "equal": True,
                    "known": reason,
                    "tolerance": looser,
                    "differences": list(verdict.differences),
                    "extra": verdict.extra,
                }
        result["engines"][name] = entry
        if not entry["equal"]:
            result["agreed"] = False
    return result


def replace_tolerance(compare, rules, name: str):
    """Returns the same rules with a different float tolerance class.

    Args:
        compare: The `fpcompat.compare` module.
        rules: The rules to copy.
        name: The tolerance class name.

    Returns:
        The new rules.
    """
    return compare.Rules(
        tolerance=compare.Tolerance[name],
        relaxations=rules.relaxations,
        reason=rules.reason,
    )


def verify(root: Path, compat: Path, suite: str = "") -> dict:
    """Runs the exact check over every answer file under a directory.

    Args:
        root: Where the answers are.
        compat: The firepanda-compat checkout.
        suite: The suite, when the caller knows it.

    Returns:
        The verdict document.
    """
    compare = load_compare(compat)
    found = collect(root, suite)
    queries = {}
    suites = set()
    for (name, query), engines in sorted(found.items()):
        suites.add(name)
        queries[f"{name}/{query}" if name != "unknown" else query] = verify_query(
            compare, name, query, engines
        )
    used = {
        (name.split("/")[0], engine)
        for name, entry in queries.items()
        for engine, verdict in entry["engines"].items()
        if verdict.get("known")
    }
    # Only entries for a suite that actually ran can be judged stale. A run of one
    # suite says nothing about a registered difference in another.
    stale = sorted(f"{s}/{e}" for (s, e) in KNOWN if s in suites and (s, e) not in used)
    return {
        "check": "exact",
        "compat": {"path": str(compat), "revision": compat_revision(compat)},
        "answers": str(root),
        "queries": queries,
        "known_unused": stale,
        "agreed": all(entry["agreed"] for entry in queries.values()),
    }


def render(document: dict) -> str:
    """Turns the verdicts into something readable.

    A disagreement prints the lines the comparison layer produced, which name the
    column and the first differing row. That is the whole reason this check exists:
    "two digests differ" tells a reader that something is wrong and nothing about
    what, and a person who has to reproduce the difference by hand before they can
    start looking at it usually does not start looking at it.

    Args:
        document: What `verify` returned.

    Returns:
        The report.
    """
    if not document["queries"]:
        return "no answer files found, so nothing was verified"

    lines = [
        f"exact check against {document['compat']['revision']} of the compat comparison layer",
        "",
    ]
    for name, entry in document["queries"].items():
        others = [e for e in entry["engines"] if e != entry["reference"]]
        if not others:
            lines.append(f"{name}: only {entry['reference']} ran, nothing to compare")
            continue
        known = [e for e in others if entry["engines"][e].get("known")]
        state = "agree" if entry["agreed"] else "DISAGREE"
        if entry["agreed"] and known:
            state = f"agree, {', '.join(known)} under a known difference"
        lines.append(
            f"{name}: {state}, {entry['rows']} rows, "
            f"{', '.join(others)} against {entry['reference']}"
        )
        for engine in others:
            verdict = entry["engines"][engine]
            if verdict["equal"] and not verdict.get("known"):
                continue
            for line in verdict["differences"]:
                lines.append(f"    {engine}: {line}")
            if verdict.get("extra"):
                lines.append(f"    {engine}: and {verdict['extra']} more")
    lines.append("")

    covered = sorted(
        {
            (name.split("/")[0], engine, verdict["known"])
            for name, entry in document["queries"].items()
            for engine, verdict in entry["engines"].items()
            if verdict.get("known")
        }
    )
    for suite, engine, reason in covered:
        lines.append(f"known difference, {suite} against {engine}: {reason}")
    if covered:
        lines.append("")

    disagreed = [name for name, entry in document["queries"].items() if not entry["agreed"]]
    if disagreed:
        lines.append(
            f"{len(disagreed)} of {len(document['queries'])} queries disagree: "
            + ", ".join(disagreed)
        )
        lines.append(
            "A disagreement here that the fingerprint did not catch is the case this "
            "check was built for, and it is a bug in an engine until somebody shows "
            "otherwise."
        )
    else:
        lines.append(
            f"all {len(document['queries'])} queries agree, row by row and value by "
            "value, exactly where nothing above says otherwise"
        )
    if document.get("known_unused"):
        lines.append(
            "unused entries in the known difference registry: "
            + ", ".join(document["known_unused"])
            + ". Each describes a difference that did not turn up. On a full suite "
            "that means the entry is stale and should be deleted, because a "
            "registry nobody prunes becomes a list of excuses. On a run of a few "
            "queries it usually means the query it happens on was not one of them, "
            "so this is reported rather than failed."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Runs the exact check from the command line.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        Zero when every query agreed, one when any did not. This one does exit
        non zero, unlike the worker: it is not a measurement that has to be
        reported whatever it says, it is a check somebody asked to run.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--compat", default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--suite",
        default="",
        help="which suite these answers belong to, when the directory does not "
        "say. It chooses the tolerance class, so a run passes it rather than "
        "letting the strictest default apply to a query that cannot meet it",
    )
    args = parser.parse_args(argv)

    if not args.answers.is_dir():
        raise SystemExit(
            f"no answers at {args.answers}. They are written by a run with "
            f"--verify exact, which is off by default."
        )

    document = verify(args.answers, compat_root(args.compat), args.suite)
    print(render(document))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return 0 if document["agreed"] else 1


if __name__ == "__main__":
    sys.exit(main())
