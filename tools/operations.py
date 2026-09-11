#!/usr/bin/env python3
"""What each query is made of, and which of those operations has a cost matrix row.

A reader who sees a query where firepanda loses wants to know which operation inside
it lost, and this repository cannot answer that on its own, because it measures
queries and a query is five or six operations wrapped in one number.

[firepanda-compat](https://github.com/tamnd/firepanda-compat) measures the other half:
a row per pandas operation, wall clock and peak resident set, against pandas on a one
million row corpus. Its operation table is published as a file with no numbers in it,
and `cost-matrix.json` beside this module is a copy. Two things follow from that.
A query that names the operations it is made of can be linked to the matching rows,
and the set of operations the suites touch becomes something that can be printed
rather than something somebody would have to read every implementation to find out.

The declarations do not live in `queries.py` on purpose. That file says what a query
is in words rather than in any engine's API, which is what makes it possible to say
that four engines ran the same query. These names are pandas names, taken from the
pandas implementation in `engines/pandas_engine.py` and `engines/pandas_tpch.py`,
because pandas is the specification everywhere in these two repositories and the
compat matrix is keyed by pandas names. Putting them in the query registry would put
one engine's vocabulary in the file that exists to be free of it.

They are read off the implementation rather than off the query text. q9 in
db-benchmark computes a correlation from its moments instead of calling `.corr()`,
and declaring `Series.corr` there would describe a query nobody runs.

An operation with no matching row is not an error and it is not hidden. It is a hole
in the cost matrix with a query attached to it, which is more useful than a complete
looking table, and `pixi run operations` prints those separately.

Until ClickBench arrived there was exactly one such name and it was excluded on
purpose rather than missing. ClickBench added five that are genuinely missing:
counting distinct values of a whole column, a minimum over one, reading the minute
out of a timestamp, a conditional expression, and taking rows at an offset. The
report separates the two kinds, because "the matrix has not measured this yet" and
"the matrix will never measure this and here is why" are different statements and
running them together makes the second one look like an excuse for the first.

There is a third kind, which ClickBench is also the first suite to produce. A row
can exist for an operation and measure a neighbour of it: the matrix counts string
characters and q27 needs bytes, and it replaces a literal where q28 runs a regular
expression with a capture group. Those never show up as holes, because coverage is
keyed by pandas name and the matrix is keyed by row, and a name can have two rows
with different costs. They are listed on their own rather than folded into either
list, since a covered count that included them would be claiming more than is known.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from queries import SUITES, lookup

ROOT = Path(__file__).resolve().parent.parent

# The vendored copy of firepanda-compat's `operations.json`. It is byte identical to
# the file that repository commits, so refreshing it is a copy and a diff means the
# matrix changed. Refresh with:
#
#   curl -fsSL https://raw.githubusercontent.com/tamnd/firepanda-compat/main/operations.json \
#     -o tools/cost-matrix.json
#
# Vendored rather than fetched at run time, because a report that needs the network to
# render is a report that renders differently depending on when you ran it.
MATRIX = Path(__file__).resolve().parent / "cost-matrix.json"

# Where the matrix is documented, for the links in the report.
MATRIX_URL = "https://github.com/tamnd/firepanda-compat/blob/main/docs/specs/09-resources.md"

# What each query is made of, in pandas names, keyed by suite and query name.
#
# The db-benchmark entries come from `engines/pandas_engine.py` and the TPC-H entries
# from `engines/pandas_tpch.py`. Where a query calls `.agg` with a named function, both
# are declared: `.agg` is the operation the engine dispatches and the function is the
# thing that has to be fast, and the cost matrix has rows for some of the functions and
# not others.
#
# `DataFrame.copy`, `DataFrame.reset_index` and `DataFrame.rename` are deliberately not
# declared anywhere. They appear all over the implementations, they are bookkeeping
# rather than work, and declaring them would attach half the suite to a row nobody
# would act on.
DECLARED: dict[tuple[str, str], tuple[str, ...]] = {
    ("db-benchmark", "q1"): ("DataFrame.groupby", "GroupBy.sum"),
    ("db-benchmark", "q2"): ("DataFrame.groupby", "GroupBy.sum"),
    ("db-benchmark", "q3"): ("DataFrame.groupby", "GroupBy.agg", "GroupBy.sum", "GroupBy.mean"),
    ("db-benchmark", "q4"): ("DataFrame.groupby", "GroupBy.agg", "GroupBy.mean"),
    ("db-benchmark", "q5"): ("DataFrame.groupby", "GroupBy.agg", "GroupBy.sum"),
    ("db-benchmark", "q6"): (
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.median",
        "GroupBy.std",
    ),
    ("db-benchmark", "q7"): (
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.max",
        "GroupBy.min",
    ),
    ("db-benchmark", "q8"): (
        "DataFrame.sort_values",
        "DataFrame.groupby",
        "GroupBy.head",
    ),
    ("db-benchmark", "q9"): (
        "DataFrame.assign",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.sum",
        "DataFrame.merge",
        "DataFrame.astype",
    ),
    ("db-benchmark", "q10"): ("DataFrame.groupby", "GroupBy.agg", "GroupBy.sum", "GroupBy.size"),
    ("db-benchmark", "j1"): ("DataFrame.merge", "Series.sum"),
    ("db-benchmark", "j2"): ("DataFrame.merge", "Series.sum"),
    ("db-benchmark", "j3"): ("DataFrame.merge", "Series.sum"),
    ("db-benchmark", "j4"): ("DataFrame.merge", "Series.sum"),
    ("db-benchmark", "j5"): ("DataFrame.merge", "Series.sum"),
    ("db-benchmark", "j6"): ("DataFrame.merge", "Series.sum"),
    ("tpch", "q1"): ("DataFrame.groupby", "GroupBy.agg", "GroupBy.sum", "GroupBy.mean"),
    ("tpch", "q2"): (
        "DataFrame.merge",
        "str.endswith",
        "DataFrame.groupby",
        "GroupBy.min",
        "Series.map",
        "DataFrame.sort_values",
        "DataFrame.head",
    ),
    ("tpch", "q3"): (
        "DataFrame.merge",
        "DataFrame.groupby",
        "GroupBy.sum",
        "DataFrame.sort_values",
        "DataFrame.head",
    ),
    ("tpch", "q4"): (
        "DataFrame.drop_duplicates",
        "DataFrame.merge",
        "DataFrame.groupby",
        "GroupBy.size",
    ),
    ("tpch", "q5"): (
        "DataFrame.merge",
        "DataFrame.groupby",
        "GroupBy.sum",
        "DataFrame.sort_values",
    ),
    ("tpch", "q6"): ("Series.sum",),
    ("tpch", "q7"): (
        "Series.isin",
        "DataFrame.merge",
        "dt.year",
        "DataFrame.groupby",
        "GroupBy.sum",
        "DataFrame.sort_values",
    ),
    ("tpch", "q8"): (
        "DataFrame.merge",
        "dt.year",
        "DataFrame.where",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.sum",
        "DataFrame.sort_values",
    ),
    ("tpch", "q9"): (
        "str.contains",
        "DataFrame.merge",
        "dt.year",
        "DataFrame.groupby",
        "GroupBy.sum",
        "DataFrame.sort_values",
    ),
    ("tpch", "q10"): (
        "DataFrame.merge",
        "DataFrame.groupby",
        "GroupBy.sum",
        "DataFrame.sort_values",
        "DataFrame.head",
    ),
    ("tpch", "q11"): (
        "DataFrame.merge",
        "Series.sum",
        "DataFrame.groupby",
        "GroupBy.sum",
        "DataFrame.sort_values",
    ),
    ("tpch", "q12"): (
        "Series.isin",
        "DataFrame.merge",
        "DataFrame.astype",
        "DataFrame.groupby",
        "GroupBy.sum",
    ),
    ("tpch", "q13"): (
        "str.contains",
        "DataFrame.merge",
        "DataFrame.groupby",
        "GroupBy.count",
        "GroupBy.size",
        "DataFrame.sort_values",
    ),
    ("tpch", "q14"): (
        "DataFrame.merge",
        "DataFrame.where",
        "str.startswith",
        "Series.sum",
    ),
    ("tpch", "q15"): (
        "DataFrame.groupby",
        "GroupBy.sum",
        "Series.max",
        "DataFrame.merge",
        "DataFrame.sort_values",
    ),
    ("tpch", "q16"): (
        "str.contains",
        "str.startswith",
        "Series.isin",
        "DataFrame.merge",
        "DataFrame.groupby",
        "GroupBy.nunique",
        "DataFrame.sort_values",
    ),
    ("tpch", "q17"): (
        "DataFrame.merge",
        "DataFrame.groupby",
        "GroupBy.mean",
        "Series.map",
        "Series.sum",
    ),
    ("tpch", "q18"): (
        "DataFrame.groupby",
        "GroupBy.sum",
        "Series.isin",
        "DataFrame.merge",
        "DataFrame.sort_values",
        "DataFrame.head",
    ),
    ("tpch", "q19"): ("Series.isin", "DataFrame.merge", "Series.sum"),
    ("tpch", "q20"): (
        "str.startswith",
        "DataFrame.groupby",
        "GroupBy.sum",
        "DataFrame.merge",
        "Series.isin",
        "DataFrame.sort_values",
    ),
    ("tpch", "q21"): (
        "DataFrame.groupby",
        "GroupBy.nunique",
        "GroupBy.size",
        "DataFrame.merge",
        "DataFrame.sort_values",
        "DataFrame.head",
    ),
    ("tpch", "q22"): (
        "str.slice",
        "Series.isin",
        "Series.mean",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.sum",
        "GroupBy.size",
    ),
    ("ingestion", "csv_narrow"): ("pandas.read_csv",),
    ("ingestion", "csv_narrow_typed"): ("pandas.read_csv",),
    ("ingestion", "csv_wide"): ("pandas.read_csv",),
    ("ingestion", "csv_quoted"): ("pandas.read_csv",),
    ("ingestion", "csv_nulls"): ("pandas.read_csv",),
    # ClickBench, read off `engines/pandas_clickbench.py` now that the port exists.
    # The first version of this table was read off the published SQL instead, because
    # the registry could not land without declarations and the port had not been
    # written, and the comment here said it would be re-derived. This is that.
    #
    # Six entries changed and the changes are worth naming, because every one of them
    # is a place where what the SQL reads like and what pandas does are not the same
    # thing. q1 and q20 count matching rows by summing a boolean mask rather than by
    # filtering a frame and taking its length, so they are a `Series.sum` and not a
    # filter. q28 pulls its capture group out with `str.replace` and a backreference
    # rather than with `str.extract`, because the query replaces the whole string and
    # extract would return a frame. q34 adds its constant column to the ten rows that
    # survive the limit, so it is q33 exactly, which is the point of the pair. q35
    # writes its three derived keys with `__setitem__` rather than with `assign`. And
    # the limit is `iloc` on every query that has one except q17, because the port
    # takes a limit and an offset through one helper and that helper slices.
    #
    # Elementwise arithmetic and comparisons are not declared, here or anywhere else
    # in this table. TPC-H q6 multiplies two columns and filters on four predicates
    # and declares `Series.sum`, because a pass over a column with an operator on it
    # is a constant factor rather than an algorithm, and declaring it would attach
    # most of three suites to a row nobody would act on. That is why q29, which sums
    # ninety expressions over one column, declares one operation, and why the ninety
    # passes are in the query description instead.
    #
    # A filter is `DataFrame.loc` whichever way it is spelled. The port writes
    # `frame[mask]` more often than `frame.loc[mask]` and the two are one operation
    # in pandas, and the matrix names its row after `loc`.
    ("clickbench", "q0"): ("DataFrame.__len__",),
    ("clickbench", "q1"): ("Series.sum",),
    ("clickbench", "q2"): ("Series.sum", "DataFrame.__len__", "Series.mean"),
    ("clickbench", "q3"): ("Series.mean",),
    ("clickbench", "q4"): ("Series.nunique",),
    ("clickbench", "q5"): ("Series.nunique",),
    ("clickbench", "q6"): ("Series.min", "Series.max"),
    ("clickbench", "q7"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
    ),
    ("clickbench", "q8"): (
        "DataFrame.groupby",
        "GroupBy.nunique",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q9"): (
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.sum",
        "GroupBy.size",
        "GroupBy.mean",
        "GroupBy.nunique",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q10"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.nunique",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q11"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.nunique",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q12"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q13"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.nunique",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q14"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q15"): (
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q16"): (
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    # The one limit in the suite with no ordering under it, so the one that is a
    # `head`. The absent sort is the query, so nothing is declared in its place.
    ("clickbench", "q17"): ("DataFrame.groupby", "GroupBy.size", "DataFrame.head"),
    ("clickbench", "q18"): (
        "DataFrame.assign",
        "dt.minute",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q19"): ("DataFrame.loc",),
    ("clickbench", "q20"): ("str.contains", "Series.sum"),
    ("clickbench", "q21"): (
        "str.contains",
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.min",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q22"): (
        "str.contains",
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.min",
        "GroupBy.size",
        "GroupBy.nunique",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q23"): (
        "str.contains",
        "DataFrame.loc",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q24"): ("DataFrame.loc", "DataFrame.sort_values", "DataFrame.iloc"),
    ("clickbench", "q25"): ("DataFrame.loc", "DataFrame.sort_values", "DataFrame.iloc"),
    ("clickbench", "q26"): ("DataFrame.loc", "DataFrame.sort_values", "DataFrame.iloc"),
    ("clickbench", "q27"): (
        "DataFrame.loc",
        "DataFrame.assign",
        "str.len",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.mean",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q28"): (
        "DataFrame.loc",
        "DataFrame.assign",
        "str.replace",
        "str.len",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.mean",
        "GroupBy.size",
        "GroupBy.min",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q29"): ("Series.sum",),
    ("clickbench", "q30"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.size",
        "GroupBy.sum",
        "GroupBy.mean",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q31"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.size",
        "GroupBy.sum",
        "GroupBy.mean",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q32"): (
        "DataFrame.groupby",
        "GroupBy.agg",
        "GroupBy.size",
        "GroupBy.sum",
        "GroupBy.mean",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q33"): (
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    # Identical to q33 on purpose. The constant column the query adds to its key goes
    # on the ten rows that survive the limit, so a planner that dropped the constant
    # and a port that never grouped on it are doing the same amount of work, and the
    # pair measures whether the other three engines drop it.
    ("clickbench", "q34"): (
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q35"): (
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q36"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q37"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q38"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q39"): (
        "DataFrame.loc",
        "Series.where",
        "DataFrame.assign",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q40"): (
        "DataFrame.loc",
        "Series.isin",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q41"): (
        "DataFrame.loc",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
    ("clickbench", "q42"): (
        "DataFrame.loc",
        "DataFrame.assign",
        "dt.floor",
        "DataFrame.groupby",
        "GroupBy.size",
        "DataFrame.sort_values",
        "DataFrame.iloc",
    ),
}

# The operations the matrix does not measure on purpose, as opposed to the ones it
# has simply not reached yet, and why each one is in here. Everything else with no
# row is a hole, and the report says so in those words.
EXCLUDED_ON_PURPOSE = {
    "pandas.read_csv": (
        "Reading a CSV is what the ingestion suite above measures, on five file shapes "
        "against four engines, and the compat corpus is Arrow on disk rather than text, "
        "so a row over there would be a worse version of a table that already exists."
    ),
    "DataFrame.__len__": (
        "Counting the rows of a frame that is already in memory reads the length of an "
        "index and touches no data, so there is nothing for a row to measure. It is "
        "declared anyway because ClickBench q0 is that and nothing else, and a query "
        "that declared nothing would look like a query nobody had got to. What q0 "
        "actually measures is in scan mode, where the answer comes out of Parquet "
        "metadata or out of a read, and that is a reader measurement rather than an "
        "operation one."
    ),
}

# Operations where a row exists, so they never appear as a hole, and the row measures
# the neighbouring operation rather than the one the query runs.
#
# Coverage is keyed by pandas name and the matrix is keyed by row, and those are not
# the same thing: `str.contains literal` and `str.contains regex` are two rows over one
# name, and they are two rows because the cost is not the same. Where a query runs the
# variant the matrix has not measured, the covered count says it is measured and it is
# measured by its neighbour. That is a smaller problem than a hole and it is not
# nothing, and a table that said nothing about it would be overstating what is known.
MEASURED_NEARBY = {
    "str.len": (
        "The matrix row is a character count and ClickBench q27 and q28 need a byte "
        "count, which is a different answer on these columns by more than two percent "
        "and a different kernel. pandas has no spelling that gives bytes on an Arrow "
        "backed Series, so the port reaches for the Arrow kernel underneath and the "
        "matrix has nothing that measures that."
    ),
    "str.replace": (
        "The matrix row replaces a literal and q28 runs a regular expression with a "
        "capture group, which goes through Python's `re` once per row rather than "
        "through a vectorized kernel. On the published size it may be the slowest "
        "single cell in the table for any engine, and the row that covers it measures "
        "something several orders of magnitude cheaper."
    ),
}


def matrix() -> dict:
    """Reads the vendored compat operation table.

    Returns:
        The table, or an empty one when the file is not there, so that a report can
        still render in a checkout that has not vendored it.
    """
    if not MATRIX.exists():
        return {"count": 0, "chained": 0, "operations": {}}
    return json.loads(MATRIX.read_text())


def declared(suite: str, name: str) -> tuple[str, ...]:
    """What one query is made of.

    Args:
        suite: The suite name.
        name: The query name.

    Returns:
        The pandas operations, in the order they matter, or an empty tuple when the
        query has not declared any.
    """
    return DECLARED.get((suite, name), ())


def rows_for(names: tuple[str, ...], table: dict | None = None) -> list[str]:
    """Which cost matrix rows cover any of these operations.

    Args:
        names: The pandas operations a query declares.
        table: The operation table, read from the vendored file when not given.

    Returns:
        The matching row ids, sorted, so that a report does not change order between
        two runs over the same data.
    """
    entries = (table or matrix())["operations"]
    wanted = set(names)
    return sorted(row for row, entry in entries.items() if wanted.intersection(entry["covers"]))


def uncovered(names: tuple[str, ...], table: dict | None = None) -> list[str]:
    """Which of these operations have no cost matrix row at all.

    A hole in the matrix with a query attached to it, which is worth more than a
    complete looking table. These are the rows the matrix should grow next.

    Args:
        names: The pandas operations a query declares.
        table: The operation table, read from the vendored file when not given.

    Returns:
        The operations with no row, in the order the query declared them.
    """
    entries = (table or matrix())["operations"]
    covered = {name for entry in entries.values() for name in entry["covers"]}
    return [name for name in names if name not in covered]


def nearby(names: tuple[str, ...]) -> list[str]:
    """Which of these operations are covered by a row measuring their neighbour.

    Args:
        names: The pandas operations a query declares.

    Returns:
        The operations in `MEASURED_NEARBY`, in the order the query declared them.
    """
    return [name for name in names if name in MEASURED_NEARBY]


def coverage() -> dict[str, list[str]]:
    """Every operation the suites touch, and which of them the matrix measures.

    Returns:
        A dict with `covered` and `missing`, each a sorted list of pandas names.
    """
    table = matrix()
    every: set[str] = set()
    for suite, queries in SUITES.items():
        for query in queries:
            every.update(declared(suite, query.name))
    known = {name for entry in table["operations"].values() for name in entry["covers"]}
    return {
        "covered": sorted(every & known),
        "missing": sorted(every - known),
    }


def report() -> str:
    """The operation coverage, as text, for `pixi run operations`.

    Returns:
        The report.
    """
    table = matrix()
    lines = [
        f"The cost matrix has {table['count']} operations, {table['chained']} of them chains.",
        "",
    ]
    for suite, queries in SUITES.items():
        lines.append(f"## {suite}")
        lines.append("")
        for query in queries:
            names = declared(suite, query.name)
            gaps = uncovered(names, table)
            lines.append(f"{query.name}: {', '.join(names) if names else 'nothing declared'}")
            measured = len(names) - len(gaps)
            note = f"  {measured} of {len(names)} measured"
            lines.append(note + (f", no row for {', '.join(gaps)}" if gaps else ""))
            near = nearby(names)
            if near:
                lines.append(f"  the row covering {', '.join(near)} measures a neighbour")
        lines.append("")
    gaps = coverage()
    lines.append("## What the suites touch that the matrix does not measure")
    lines.append("")
    excluded = [name for name in gaps["missing"] if name in EXCLUDED_ON_PURPOSE]
    holes = [name for name in gaps["missing"] if name not in EXCLUDED_ON_PURPOSE]
    if not gaps["missing"]:
        lines.append("Nothing. Every operation the suites touch has a row.")
        return "\n".join(lines) + "\n"
    if holes:
        lines.append(", ".join(holes))
        lines.append("")
        lines.append(
            "Each of those is a pandas operation a published benchmark query runs and the "
            "cost matrix has no row for. They are the rows the matrix should grow next."
        )
        lines.append("")
    for name in excluded:
        lines.append(f"{name} is a deliberate exclusion rather than a hole.")
        lines.append("")
        lines.append(EXCLUDED_ON_PURPOSE[name])
        lines.append("")
    touched = {name for name in gaps["covered"] if name in MEASURED_NEARBY}
    if touched:
        lines.append("## Rows that measure the neighbour of what a query runs")
        lines.append("")
        lines.append(
            "These are covered, so they are in neither list above, and the row that "
            "covers them is measuring a cheaper relative of what the query does. The "
            "matrix already splits `str.contains` into a literal row and a regular "
            "expression row for the same reason, so these are rows it should split too."
        )
        lines.append("")
        for name in sorted(touched):
            lines.append(f"{name}: {MEASURED_NEARBY[name]}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Prints what each query is made of and what the matrix does not cover.

    Args:
        argv: Command line arguments.

    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description="What each query is made of.")
    parser.add_argument("--query", help="one query, as suite/name")
    args = parser.parse_args(argv)

    if args.query:
        suite, _, name = args.query.partition("/")
        query = lookup(suite, name)
        names = declared(suite, query.name)
        print(f"{suite}/{query.name}: {query.description}")
        print(f"operations: {', '.join(names) if names else 'nothing declared'}")
        for row in rows_for(names):
            print(f"  matrix row: {row}")
        for gap in uncovered(names):
            print(f"  no matrix row: {gap}")
        return 0

    print(report(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
