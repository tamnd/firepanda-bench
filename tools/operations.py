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
and the set of operations the 37 queries touch becomes something that can be printed
rather than something somebody would have to read 37 implementations to find out.

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
        lines.append("")
    gaps = coverage()
    lines.append("## What the suites touch that the matrix does not measure")
    lines.append("")
    if gaps["missing"]:
        lines.append(", ".join(gaps["missing"]))
        lines.append("")
        if set(gaps["missing"]) == {"pandas.read_csv"}:
            lines.append(
                "That one is a deliberate exclusion rather than a hole. Reading a CSV is "
                "what the ingestion suite above measures, on five file shapes against four "
                "engines, and the compat corpus is Arrow on disk rather than text, so a row "
                "over there would be a worse version of a table that already exists."
            )
        else:
            lines.append(
                "Each of those is a pandas operation a published benchmark query runs and "
                "the cost matrix has no row for. They are the rows the matrix should grow next."
            )
    else:
        lines.append("Nothing. Every operation the 37 queries touch has a row.")
    return "\n".join(lines) + "\n"


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
