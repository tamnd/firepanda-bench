"""pandas, which is the API being replaced and the audience being addressed.

pandas 3.0 and not 2.x, deliberately. 2.x is what most people are running and it
is not what firepanda is competing with, and using it would inflate every number
in a way that is indefensible the moment somebody notices.

Two things are done to pandas here that are not obvious and are both in its
favour. Grouping passes `observed=True`, without which a categorical key produces
the full cross product of categories and the query becomes a memory benchmark.
And every query keeps `sort=False`, because sorting the groups is not part of the
question and every other engine is allowed to skip it too.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa

from . import pandas_tpch

NAME = "pandas"


def version() -> str:
    """Returns the installed version.

    Returns:
        The version string.
    """
    return pd.__version__


def load(paths: dict[str, str], suite: str = "db-benchmark", io: str = "memory") -> dict:
    """Reads the tables this run needs.

    pandas has no lazy scan, so `io` changes nothing here and the argument exists
    only so every engine takes the same one. That asymmetry is the point rather
    than an oversight: in scan mode pandas is reading whole columns that Polars
    and DuckDB never touch, and the report says which engines that applies to.

    Args:
        paths: A mapping from table name to a Parquet path.
        suite: Which suite is being run.
        io: How the tables should reach the engine.

    Returns:
        A mapping from table name to a DataFrame.
    """
    # Arrow backed, because that is what pandas 3.0 recommends and what makes the
    # comparison one between engines rather than one between memory layouts.
    frames = {name: pd.read_parquet(path, dtype_backend="pyarrow") for name, path in paths.items()}
    if suite == "tpch":
        for frame in frames.values():
            for column in frame.columns:
                arrow_type = frame[column].dtype.pyarrow_dtype
                # The TPC-H date columns come out of Parquet as date32. Comparing
                # those against a timestamp raises, and casting once here is both
                # faster and closer to what a user would have than casting inside
                # every query.
                if pa.types.is_date(arrow_type):
                    frame[column] = frame[column].astype("timestamp[us][pyarrow]")
                # The money columns are DECIMAL(15,2), which is what the
                # specification says they are and what DuckDB and Polars both
                # carry through the whole query. Arrow backed pandas cannot.
                # q1 multiplies a price by two discount factors, and Arrow works
                # out the result needs a precision of sixty one and refuses,
                # since its decimals stop at thirty eight. There is no way to
                # widen it and no way to ask for a narrower intermediate.
                #
                # So pandas gets float64 prices. It is worth being clear about
                # which way that bias runs: float multiplication is faster than
                # decimal multiplication, so this makes pandas look better than a
                # decimal pandas would, and pandas is the baseline firepanda is
                # being sold against. The report says so next to the TPC-H table.
                elif pa.types.is_decimal(arrow_type):
                    frame[column] = frame[column].astype("double[pyarrow]")
    return frames


def finish(frame: pd.DataFrame) -> pa.Table:
    """Materializes an answer and hands it to the digest.

    Args:
        frame: The answer.

    Returns:
        The answer as an Arrow table.
    """
    return pa.Table.from_pandas(frame, preserve_index=False)


def q1(ctx: dict) -> pa.Table:
    """Sums v1 by id1.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    return finish(df.groupby("id1", as_index=False, sort=False, observed=True)["v1"].sum())


def q2(ctx: dict) -> pa.Table:
    """Sums v1 by id1 and id2.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    return finish(df.groupby(["id1", "id2"], as_index=False, sort=False, observed=True)["v1"].sum())


def q3(ctx: dict) -> pa.Table:
    """Sums v1 and averages v3 by id3.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    return finish(
        df.groupby("id3", as_index=False, sort=False, observed=True).agg(
            v1=("v1", "sum"), v3=("v3", "mean")
        )
    )


def q4(ctx: dict) -> pa.Table:
    """Averages v1, v2 and v3 by id4.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    return finish(
        df.groupby("id4", as_index=False, sort=False, observed=True).agg(
            v1=("v1", "mean"), v2=("v2", "mean"), v3=("v3", "mean")
        )
    )


def q5(ctx: dict) -> pa.Table:
    """Sums v1, v2 and v3 by id6.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    return finish(
        df.groupby("id6", as_index=False, sort=False, observed=True).agg(
            v1=("v1", "sum"), v2=("v2", "sum"), v3=("v3", "sum")
        )
    )


def q6(ctx: dict) -> pa.Table:
    """Takes the median and standard deviation of v3 by id4 and id6.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    return finish(
        df.groupby(["id4", "id6"], as_index=False, sort=False, observed=True).agg(
            v3_median=("v3", "median"), v3_sd=("v3", "std")
        )
    )


def q7(ctx: dict) -> pa.Table:
    """Takes the largest v1 minus the smallest v2 by id3.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    grouped = df.groupby("id3", as_index=False, sort=False, observed=True).agg(
        v1_max=("v1", "max"), v2_min=("v2", "min")
    )
    grouped["range_v1_v2"] = grouped["v1_max"] - grouped["v2_min"]
    return finish(grouped[["id3", "range_v1_v2"]])


def q8(ctx: dict) -> pa.Table:
    """Takes the two largest v3 per id6.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"][["id6", "v3"]]
    top = df.sort_values("v3", ascending=False).groupby("id6", sort=False, observed=True).head(2)
    return finish(top.reset_index(drop=True))


def q9(ctx: dict) -> pa.Table:
    """Takes the squared correlation of v1 and v2 by id2 and id4.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    # Computed from the moments rather than through `.corr()`, because `.corr()`
    # on a group by runs a Python level apply in pandas and would be measuring the
    # interpreter rather than the engine.
    grouped = df.groupby(["id2", "id4"], as_index=False, sort=False, observed=True).agg(
        n=("v1", "size"),
        sx=("v1", "sum"),
        sy=("v2", "sum"),
        sxx=("v1", lambda s: float(np.dot(s.to_numpy(), s.to_numpy()))),
        syy=("v2", lambda s: float(np.dot(s.to_numpy(), s.to_numpy()))),
    )
    products = (
        df.assign(xy=df["v1"].astype("float64") * df["v2"].astype("float64"))
        .groupby(["id2", "id4"], as_index=False, sort=False, observed=True)["xy"]
        .sum()
    )
    grouped = grouped.merge(products, on=["id2", "id4"])
    n = grouped["n"].astype("float64")
    cov = grouped["xy"].astype("float64") - grouped["sx"] * grouped["sy"] / n
    vx = grouped["sxx"] - grouped["sx"] ** 2 / n
    vy = grouped["syy"] - grouped["sy"] ** 2 / n
    grouped["r2"] = (cov * cov / (vx * vy)).astype("float64")
    return finish(grouped[["id2", "id4", "r2"]])


def q10(ctx: dict) -> pa.Table:
    """Sums v3 and counts by all six key columns.

    Args:
        ctx: The loaded tables.

    Returns:
        The answer.
    """
    df = ctx["groupby"]
    keys = ["id1", "id2", "id3", "id4", "id5", "id6"]
    return finish(
        df.groupby(keys, as_index=False, sort=False, observed=True).agg(
            v3=("v3", "sum"), count=("v1", "size")
        )
    )


def _join(ctx: dict, right: str, key: str, how: str) -> pa.Table:
    """Joins the left table against one of the right tables.

    Args:
        ctx: The loaded tables.
        right: Which right table.
        key: The join key.
        how: The join kind.

    Returns:
        The answer, reduced to a shape a digest can compare.
    """
    joined = ctx["left"].merge(ctx[right], on=key, how=how, sort=False)
    # The join result is as large as the left table, and comparing a hundred
    # million rows across four engines is a benchmark of the comparison. The
    # reduction is part of the query for every engine, so it costs all of them the
    # same and it forces the join to be materialized.
    return finish(
        pd.DataFrame(
            {
                "rows": [len(joined)],
                "v1": [float(joined["v1"].sum())],
                "v2": [float(joined["v2"].sum(skipna=True))],
            }
        )
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
        return finish(pandas_tpch.QUERIES[name](ctx))

    run.__name__ = f"tpch_{name}"
    return run


TPCH_QUERIES = {name: _tpch(name) for name in pandas_tpch.QUERIES}
