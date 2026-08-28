"""Polars, which is the performance bar and the reference implementation of this design.

Every query is written lazily and collected, because that is the idiomatic Polars
and the fast one, and refusing to use it would be benchmarking a straw man. The
`collect` at the end is the materialization the harness insists on: a lazy engine
that is never asked for an answer has not done any work.

The data is read eagerly once, outside the timing, so that what is measured is the
query rather than the Parquet reader. The ingestion suite is where the reader is
measured, and it measures it on purpose.
"""

from __future__ import annotations

import polars as pl
import pyarrow as pa
import queries

from . import polars_tpch

NAME = "polars"


def version() -> str:
    """Returns the installed version.

    Returns:
        The version string.
    """
    return pl.__version__


def load(paths: dict[str, str], suite: str = "db-benchmark", io: str = "memory") -> dict:
    """Reads the tables this run needs.

    In memory mode the frames are read here and the queries call `.lazy()` on
    them, so Polars keeps its optimizer and loses only the Parquet pushdown. In
    scan mode nothing is read here and the whole read happens inside the timed
    region, which is the end to end number and the one Polars is designed for.

    Args:
        paths: A mapping from table name to a Parquet path.
        suite: Which suite is being run.
        io: How the tables should reach the engine.

    Returns:
        A mapping from table name to a frame, eager or lazy depending on `io`, or
        to a path for the ingestion suite, where reading the file is the thing
        being timed.
    """
    if suite == "ingestion":
        return dict(paths)
    if io == "scan":
        return {name: pl.scan_parquet(path) for name, path in paths.items()}
    return {name: pl.read_parquet(path) for name, path in paths.items()}


def finish(frame: pl.DataFrame) -> pa.Table:
    """Hands a collected answer to the digest.

    Args:
        frame: The answer.

    Returns:
        The answer as an Arrow table.
    """
    return frame.to_arrow()


def q1(ctx: dict) -> pa.Table:
    """Sums v1 by id1.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(ctx["groupby"].lazy().group_by("id1").agg(pl.col("v1").sum()).collect())


def q2(ctx: dict) -> pa.Table:
    """Sums v1 by id1 and id2.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(ctx["groupby"].lazy().group_by("id1", "id2").agg(pl.col("v1").sum()).collect())


def q3(ctx: dict) -> pa.Table:
    """Sums v1 and averages v3 by id3.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(
        ctx["groupby"].lazy().group_by("id3").agg(pl.col("v1").sum(), pl.col("v3").mean()).collect()
    )


def q4(ctx: dict) -> pa.Table:
    """Averages v1, v2 and v3 by id4.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(
        ctx["groupby"]
        .lazy()
        .group_by("id4")
        .agg(pl.col("v1").mean(), pl.col("v2").mean(), pl.col("v3").mean())
        .collect()
    )


def q5(ctx: dict) -> pa.Table:
    """Sums v1, v2 and v3 by id6.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(
        ctx["groupby"]
        .lazy()
        .group_by("id6")
        .agg(pl.col("v1").sum(), pl.col("v2").sum(), pl.col("v3").sum())
        .collect()
    )


def q6(ctx: dict) -> pa.Table:
    """Takes the median and standard deviation of v3 by id4 and id6.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(
        ctx["groupby"]
        .lazy()
        .group_by("id4", "id6")
        .agg(
            pl.col("v3").median().alias("v3_median"),
            pl.col("v3").std().alias("v3_sd"),
        )
        .collect()
    )


def q7(ctx: dict) -> pa.Table:
    """Takes the largest v1 minus the smallest v2 by id3.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(
        ctx["groupby"]
        .lazy()
        .group_by("id3")
        .agg((pl.col("v1").max() - pl.col("v2").min()).alias("range_v1_v2"))
        .collect()
    )


def q8(ctx: dict) -> pa.Table:
    """Takes the two largest v3 per id6.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(
        ctx["groupby"]
        .lazy()
        .select("id6", "v3")
        .group_by("id6")
        .agg(pl.col("v3").top_k(2))
        .explode("v3")
        .collect()
    )


def q9(ctx: dict) -> pa.Table:
    """Takes the squared correlation of v1 and v2 by id2 and id4.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(
        ctx["groupby"]
        .lazy()
        .group_by("id2", "id4")
        .agg((pl.corr("v1", "v2") ** 2).alias("r2"))
        .collect()
    )


def q10(ctx: dict) -> pa.Table:
    """Sums v3 and counts by all six key columns.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return finish(
        ctx["groupby"]
        .lazy()
        .group_by("id1", "id2", "id3", "id4", "id5", "id6")
        .agg(pl.col("v3").sum(), pl.len().alias("count"))
        .collect()
    )


def _join(ctx: dict, right: str, key: str, how: str) -> pa.Table:
    """Joins the left table against one of the right tables.

    The reduction at the end is part of the query for every engine, so it costs
    all of them the same and it forces the join to be materialized rather than
    left as a plan.

    Args:
        ctx: The loaded tables.
        right: Which right table.
        key: The join key.
        how: The join kind, in Polars spelling.

    Returns:
        The answer.
    """
    return finish(
        ctx["left"]
        .lazy()
        .join(ctx[right].lazy(), on=key, how=how)
        .select(
            pl.len().alias("rows"),
            pl.col("v1").sum().cast(pl.Float64).alias("v1"),
            pl.col("v2").sum().cast(pl.Float64).alias("v2"),
        )
        .collect()
    )


def j1(ctx: dict) -> pa.Table:
    """Inner joins the small right table.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return _join(ctx, "right_small", "id1", "inner")


def j2(ctx: dict) -> pa.Table:
    """Inner joins the medium right table.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return _join(ctx, "right_medium", "id2", "inner")


def j3(ctx: dict) -> pa.Table:
    """Left outer joins the medium right table.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return _join(ctx, "right_medium", "id2", "left")


def j4(ctx: dict) -> pa.Table:
    """Inner joins the big right table.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return _join(ctx, "right_big", "id3", "inner")


def j5(ctx: dict) -> pa.Table:
    """Left outer joins the big right table.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    return _join(ctx, "right_big", "id3", "left")


QUERIES = {
    "q1": q1,
    "q2": q2,
    "q3": q3,
    "q4": q4,
    "q5": q5,
    "q6": q6,
    "q7": q7,
    "q8": q8,
    "q9": q9,
    "q10": q10,
    "j1": j1,
    "j2": j2,
    "j3": j3,
    "j4": j4,
    "j5": j5,
}


def _lazy(ctx: dict) -> dict:
    """Presents the loaded tables as lazy frames whichever mode they arrived in.

    Args:
        ctx: The loaded tables.

    Returns:
        A mapping from table name to lazy frame.
    """
    return {
        name: frame if isinstance(frame, pl.LazyFrame) else frame.lazy()
        for name, frame in ctx.items()
    }


def _tpch(name: str):
    """Wraps one TPC-H query so it returns an Arrow table like everything else.

    Args:
        name: The query name.

    Returns:
        A callable taking the loaded tables.
    """

    def run(ctx: dict) -> pa.Table:
        """Runs the query.

        Args:
            ctx: The loaded tables.

        Returns:
            The answer.
        """
        return finish(polars_tpch.QUERIES[name](_lazy(ctx)))

    run.__name__ = f"tpch_{name}"
    return run


TPCH_QUERIES = {name: _tpch(name) for name in polars_tpch.QUERIES}


# The neutral names in `queries.NARROW_SCHEMA`, in Polars.
POLARS_TYPES = {"int64": pl.Int64, "float64": pl.Float64, "string": pl.String}


def read_one(ctx: dict, table: str, schema: dict | None = None) -> pl.DataFrame:
    """Reads one CSV file.

    `read_csv` rather than `scan_csv().collect()`, and the difference is worth a
    sentence because everywhere else in this harness Polars is used lazily. There
    is no projection or predicate to push into a read of a whole file, so the lazy
    form would do the same work behind a planner, and the eager call is what a
    Polars user writes when what they want is the file.

    No `finish` here, unlike every other query in this file, and that one missing
    call was worth an order of magnitude. Polars stores text as a view into a
    buffer and Arrow wants it offset encoded, so `to_arrow` rebuilds every string
    column: on the wide file the read takes 9.6 ms and the conversion 707 ms.
    Timing the conversion would have published Polars as the slowest CSV reader
    here when it is among the fastest. The harness converts afterwards, outside the
    timed region, where every engine pays for its own conversion equally.

    Args:
        ctx: The paths from `load`.
        table: Which file.
        schema: Declared types, or None to infer them.

    Returns:
        The frame that was read.
    """
    if schema is None:
        return pl.read_csv(ctx[table])
    return pl.read_csv(ctx[table], schema=schema)


INGESTION_QUERIES = {
    "csv_narrow": lambda ctx: read_one(ctx, "narrow"),
    "csv_narrow_typed": lambda ctx: read_one(
        ctx, "narrow", {name: POLARS_TYPES[kind] for name, kind in queries.NARROW_SCHEMA}
    ),
    "csv_wide": lambda ctx: read_one(ctx, "wide"),
    "csv_quoted": lambda ctx: read_one(ctx, "quoted"),
    "csv_nulls": lambda ctx: read_one(ctx, "nulls"),
}
