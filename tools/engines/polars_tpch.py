"""The twenty two TPC-H queries in Polars, written against the lazy API.

These are the official queries expressed as dataframe operations rather than SQL,
which is the only way a dataframe library can be in a TPC-H table at all. The
substitution parameters are the specification's validation values, the same ones
DuckDB's `tpch_queries()` carries, so all four engines answer the same question.
`tools/validate_tpch.py` checks each answer against the specification's published
output, and a query that does not reproduce it does not go in a result file.

Everything is `scan_parquet` and `collect`, not `read_parquet`. Handing Polars an
already materialized frame and asking it to filter would measure a Polars that has
had its main advantage taken away, and no honest TPC-H comparison does that. The
load step therefore costs almost nothing here and the query step carries the read,
which is exactly what the report has to say when it shows the numbers.

`collect` is the materialization the db-benchmark methodology insists on. A lazy
frame that is never collected has done nothing.
"""

from __future__ import annotations

from datetime import date

import polars as pl


# The eight tables, scanned lazily. The plan decides which of them are touched.
def scan(paths: dict[str, str]) -> dict[str, pl.LazyFrame]:
    """Opens every table the query needs as a lazy scan.

    Args:
        paths: A mapping from table name to a Parquet path.

    Returns:
        A mapping from table name to lazy frame.
    """
    return {name: pl.scan_parquet(path) for name, path in paths.items()}


def q1(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Pricing Summary Report.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    disc_price = pl.col("l_extendedprice") * (1 - pl.col("l_discount"))
    return (
        t["lineitem"]
        .filter(pl.col("l_shipdate") <= date(1998, 9, 2))
        .group_by("l_returnflag", "l_linestatus")
        .agg(
            pl.sum("l_quantity").alias("sum_qty"),
            pl.sum("l_extendedprice").alias("sum_base_price"),
            disc_price.sum().alias("sum_disc_price"),
            (disc_price * (1 + pl.col("l_tax"))).sum().alias("sum_charge"),
            pl.mean("l_quantity").alias("avg_qty"),
            pl.mean("l_extendedprice").alias("avg_price"),
            pl.mean("l_discount").alias("avg_disc"),
            pl.len().alias("count_order"),
        )
        .sort("l_returnflag", "l_linestatus")
        .collect()
    )


def q2(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Minimum Cost Supplier.

    The correlated subquery becomes a group by on part key followed by a join back
    against the same intermediate, which is what a planner does to it too.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    europe = (
        t["region"]
        .filter(pl.col("r_name") == "EUROPE")
        .join(t["nation"], left_on="r_regionkey", right_on="n_regionkey")
        .join(t["supplier"], left_on="n_nationkey", right_on="s_nationkey")
        .join(t["partsupp"], left_on="s_suppkey", right_on="ps_suppkey")
    )
    wanted = t["part"].filter((pl.col("p_size") == 15) & pl.col("p_type").str.ends_with("BRASS"))
    joined = europe.join(wanted, left_on="ps_partkey", right_on="p_partkey")
    cheapest = joined.group_by("ps_partkey").agg(pl.min("ps_supplycost").alias("ps_supplycost"))
    return (
        joined.join(cheapest, on=["ps_partkey", "ps_supplycost"])
        .select(
            "s_acctbal",
            "s_name",
            "n_name",
            pl.col("ps_partkey").alias("p_partkey"),
            "p_mfgr",
            "s_address",
            "s_phone",
            "s_comment",
        )
        .sort(
            ["s_acctbal", "n_name", "s_name", "p_partkey"],
            descending=[True, False, False, False],
        )
        .head(100)
        .collect()
    )


def q3(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Shipping Priority.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    cutoff = date(1995, 3, 15)
    return (
        t["customer"]
        .filter(pl.col("c_mktsegment") == "BUILDING")
        .join(t["orders"], left_on="c_custkey", right_on="o_custkey")
        .filter(pl.col("o_orderdate") < cutoff)
        .join(t["lineitem"], left_on="o_orderkey", right_on="l_orderkey")
        .filter(pl.col("l_shipdate") > cutoff)
        .with_columns((pl.col("l_extendedprice") * (1 - pl.col("l_discount"))).alias("revenue"))
        .group_by("o_orderkey", "o_orderdate", "o_shippriority")
        .agg(pl.sum("revenue"))
        .select(
            pl.col("o_orderkey").alias("l_orderkey"),
            "revenue",
            "o_orderdate",
            "o_shippriority",
        )
        .sort(["revenue", "o_orderdate"], descending=[True, False])
        .head(10)
        .collect()
    )


def q4(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Order Priority Checking.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    late = t["lineitem"].filter(pl.col("l_commitdate") < pl.col("l_receiptdate"))
    return (
        t["orders"]
        .filter(
            (pl.col("o_orderdate") >= date(1993, 7, 1))
            & (pl.col("o_orderdate") < date(1993, 10, 1))
        )
        .join(late, left_on="o_orderkey", right_on="l_orderkey", how="semi")
        .group_by("o_orderpriority")
        .agg(pl.len().alias("order_count"))
        .sort("o_orderpriority")
        .collect()
    )


def q5(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Local Supplier Volume.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    return (
        t["region"]
        .filter(pl.col("r_name") == "ASIA")
        .join(t["nation"], left_on="r_regionkey", right_on="n_regionkey")
        .join(t["customer"], left_on="n_nationkey", right_on="c_nationkey")
        .join(t["orders"], left_on="c_custkey", right_on="o_custkey")
        .filter(
            (pl.col("o_orderdate") >= date(1994, 1, 1)) & (pl.col("o_orderdate") < date(1995, 1, 1))
        )
        .join(t["lineitem"], left_on="o_orderkey", right_on="l_orderkey")
        .join(
            t["supplier"],
            left_on=["l_suppkey", "n_nationkey"],
            right_on=["s_suppkey", "s_nationkey"],
        )
        .with_columns((pl.col("l_extendedprice") * (1 - pl.col("l_discount"))).alias("revenue"))
        .group_by("n_name")
        .agg(pl.sum("revenue"))
        .sort("revenue", descending=True)
        .collect()
    )


def q6(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Forecasting Revenue Change.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    return (
        t["lineitem"]
        .filter(
            (pl.col("l_shipdate") >= date(1994, 1, 1))
            & (pl.col("l_shipdate") < date(1995, 1, 1))
            & (pl.col("l_discount") >= 0.05)
            & (pl.col("l_discount") <= 0.07)
            & (pl.col("l_quantity") < 24)
        )
        .select((pl.col("l_extendedprice") * pl.col("l_discount")).sum().alias("revenue"))
        .collect()
    )


def q7(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Volume Shipping.

    The disjunctive nation pair predicate is written as two directed joins and a
    concatenation, because a filter over the full cross product is the shape the
    query is trying to avoid and no engine should be measured running it.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    nations = t["nation"].filter(pl.col("n_name").is_in(["FRANCE", "GERMANY"]))
    shipping = (
        t["lineitem"]
        .filter(
            (pl.col("l_shipdate") >= date(1995, 1, 1))
            & (pl.col("l_shipdate") <= date(1996, 12, 31))
        )
        .join(t["supplier"], left_on="l_suppkey", right_on="s_suppkey")
        .join(
            nations.select(pl.col("n_nationkey"), pl.col("n_name").alias("supp_nation")),
            left_on="s_nationkey",
            right_on="n_nationkey",
        )
        .join(t["orders"], left_on="l_orderkey", right_on="o_orderkey")
        .join(t["customer"], left_on="o_custkey", right_on="c_custkey")
        .join(
            nations.select(pl.col("n_nationkey"), pl.col("n_name").alias("cust_nation")),
            left_on="c_nationkey",
            right_on="n_nationkey",
        )
        .filter(pl.col("supp_nation") != pl.col("cust_nation"))
        .with_columns(
            pl.col("l_shipdate").dt.year().alias("l_year"),
            (pl.col("l_extendedprice") * (1 - pl.col("l_discount"))).alias("volume"),
        )
    )
    return (
        shipping.group_by("supp_nation", "cust_nation", "l_year")
        .agg(pl.sum("volume").alias("revenue"))
        .sort("supp_nation", "cust_nation", "l_year")
        .collect()
    )


def q8(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """National Market Share.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    nation = t["nation"]
    america = (
        t["region"]
        .filter(pl.col("r_name") == "AMERICA")
        .join(nation, left_on="r_regionkey", right_on="n_regionkey")
        .select("n_nationkey")
    )
    return (
        t["part"]
        .filter(pl.col("p_type") == "ECONOMY ANODIZED STEEL")
        .join(t["lineitem"], left_on="p_partkey", right_on="l_partkey")
        .join(t["orders"], left_on="l_orderkey", right_on="o_orderkey")
        .filter(
            (pl.col("o_orderdate") >= date(1995, 1, 1))
            & (pl.col("o_orderdate") <= date(1996, 12, 31))
        )
        .join(t["customer"], left_on="o_custkey", right_on="c_custkey")
        .join(america, left_on="c_nationkey", right_on="n_nationkey")
        .join(t["supplier"], left_on="l_suppkey", right_on="s_suppkey")
        .join(
            nation.select(pl.col("n_nationkey"), pl.col("n_name").alias("nation")),
            left_on="s_nationkey",
            right_on="n_nationkey",
        )
        .with_columns(
            pl.col("o_orderdate").dt.year().alias("o_year"),
            # Float rather than decimal, and only here. The prices in the
            # Parquet are decimal with two places, and Polars gives a quotient of
            # two decimals the scale of the numerator, so the market share came
            # out as 0.03 where the specification wants 0.0344358904066548. Every
            # other query keeps the decimals, because a sum of money should not
            # go through a float, and this one is a ratio rather than money.
            (
                pl.col("l_extendedprice").cast(pl.Float64)
                * (1 - pl.col("l_discount").cast(pl.Float64))
            ).alias("volume"),
        )
        .group_by("o_year")
        .agg(
            (
                pl.when(pl.col("nation") == "BRAZIL").then(pl.col("volume")).otherwise(0).sum()
                / pl.sum("volume")
            ).alias("mkt_share")
        )
        .sort("o_year")
        .collect()
    )


def q9(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Product Type Profit Measure.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    return (
        t["part"]
        .filter(pl.col("p_name").str.contains("green"))
        .join(t["lineitem"], left_on="p_partkey", right_on="l_partkey")
        .join(t["supplier"], left_on="l_suppkey", right_on="s_suppkey")
        .join(
            t["partsupp"],
            left_on=["p_partkey", "l_suppkey"],
            right_on=["ps_partkey", "ps_suppkey"],
        )
        .join(t["orders"], left_on="l_orderkey", right_on="o_orderkey")
        .join(t["nation"], left_on="s_nationkey", right_on="n_nationkey")
        .with_columns(
            pl.col("n_name").alias("nation"),
            pl.col("o_orderdate").dt.year().alias("o_year"),
            (
                pl.col("l_extendedprice") * (1 - pl.col("l_discount"))
                - pl.col("ps_supplycost") * pl.col("l_quantity")
            ).alias("amount"),
        )
        .group_by("nation", "o_year")
        .agg(pl.sum("amount").alias("sum_profit"))
        .sort(["nation", "o_year"], descending=[False, True])
        .collect()
    )


def q10(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Returned Item Reporting.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    return (
        t["customer"]
        .join(t["orders"], left_on="c_custkey", right_on="o_custkey")
        .filter(
            (pl.col("o_orderdate") >= date(1993, 10, 1))
            & (pl.col("o_orderdate") < date(1994, 1, 1))
        )
        .join(t["lineitem"], left_on="o_orderkey", right_on="l_orderkey")
        .filter(pl.col("l_returnflag") == "R")
        .join(t["nation"], left_on="c_nationkey", right_on="n_nationkey")
        .with_columns((pl.col("l_extendedprice") * (1 - pl.col("l_discount"))).alias("revenue"))
        .group_by(
            "c_custkey",
            "c_name",
            "c_acctbal",
            "c_phone",
            "n_name",
            "c_address",
            "c_comment",
        )
        .agg(pl.sum("revenue"))
        .select(
            "c_custkey",
            "c_name",
            "revenue",
            "c_acctbal",
            "n_name",
            "c_address",
            "c_phone",
            "c_comment",
        )
        .sort("revenue", descending=True)
        .head(20)
        .collect()
    )


def q11(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Important Stock Identification.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    german = (
        t["partsupp"]
        .join(t["supplier"], left_on="ps_suppkey", right_on="s_suppkey")
        .join(t["nation"], left_on="s_nationkey", right_on="n_nationkey")
        .filter(pl.col("n_name") == "GERMANY")
        .with_columns((pl.col("ps_supplycost") * pl.col("ps_availqty")).alias("value"))
    )
    threshold = german.select((pl.sum("value") * 0.0001).alias("threshold"))
    return (
        german.group_by("ps_partkey")
        .agg(pl.sum("value"))
        .join(threshold, how="cross")
        .filter(pl.col("value") > pl.col("threshold"))
        .select("ps_partkey", "value")
        .sort("value", descending=True)
        .collect()
    )


def q12(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Shipping Modes and Order Priority.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    urgent = pl.col("o_orderpriority").is_in(["1-URGENT", "2-HIGH"])
    return (
        t["orders"]
        .join(t["lineitem"], left_on="o_orderkey", right_on="l_orderkey")
        .filter(
            pl.col("l_shipmode").is_in(["MAIL", "SHIP"])
            & (pl.col("l_commitdate") < pl.col("l_receiptdate"))
            & (pl.col("l_shipdate") < pl.col("l_commitdate"))
            & (pl.col("l_receiptdate") >= date(1994, 1, 1))
            & (pl.col("l_receiptdate") < date(1995, 1, 1))
        )
        .group_by("l_shipmode")
        .agg(
            urgent.sum().alias("high_line_count"),
            (~urgent).sum().alias("low_line_count"),
        )
        .sort("l_shipmode")
        .collect()
    )


def q13(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Customer Distribution.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    orders = t["orders"].filter(~pl.col("o_comment").str.contains("special.*requests"))
    return (
        t["customer"]
        .join(orders, left_on="c_custkey", right_on="o_custkey", how="left")
        .group_by("c_custkey")
        .agg(pl.col("o_orderkey").count().alias("c_count"))
        .group_by("c_count")
        .agg(pl.len().alias("custdist"))
        .sort(["custdist", "c_count"], descending=[True, True])
        .collect()
    )


def q14(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Promotion Effect.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    revenue = pl.col("l_extendedprice") * (1 - pl.col("l_discount"))
    return (
        t["lineitem"]
        .filter(
            (pl.col("l_shipdate") >= date(1995, 9, 1)) & (pl.col("l_shipdate") < date(1995, 10, 1))
        )
        .join(t["part"], left_on="l_partkey", right_on="p_partkey")
        .select(
            (
                100.00
                * pl.when(pl.col("p_type").str.starts_with("PROMO"))
                .then(revenue)
                .otherwise(0)
                .sum()
                / revenue.sum()
            ).alias("promo_revenue")
        )
        .collect()
    )


def q15(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Top Supplier.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    revenue = (
        t["lineitem"]
        .filter(
            (pl.col("l_shipdate") >= date(1996, 1, 1)) & (pl.col("l_shipdate") < date(1996, 4, 1))
        )
        .group_by("l_suppkey")
        .agg((pl.col("l_extendedprice") * (1 - pl.col("l_discount"))).sum().alias("total_revenue"))
    )
    best = revenue.select(pl.max("total_revenue").alias("total_revenue"))
    return (
        t["supplier"]
        .join(revenue, left_on="s_suppkey", right_on="l_suppkey")
        .join(best, on="total_revenue")
        .select("s_suppkey", "s_name", "s_address", "s_phone", "total_revenue")
        .sort("s_suppkey")
        .collect()
    )


def q16(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Parts/Supplier Relationship.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    complained = t["supplier"].filter(pl.col("s_comment").str.contains("Customer.*Complaints"))
    return (
        t["part"]
        .filter(
            (pl.col("p_brand") != "Brand#45")
            & ~pl.col("p_type").str.starts_with("MEDIUM POLISHED")
            & pl.col("p_size").is_in([49, 14, 23, 45, 19, 3, 36, 9])
        )
        .join(t["partsupp"], left_on="p_partkey", right_on="ps_partkey")
        .join(
            complained.select("s_suppkey"),
            left_on="ps_suppkey",
            right_on="s_suppkey",
            how="anti",
        )
        .group_by("p_brand", "p_type", "p_size")
        .agg(pl.col("ps_suppkey").n_unique().alias("supplier_cnt"))
        .sort(
            ["supplier_cnt", "p_brand", "p_type", "p_size"],
            descending=[True, False, False, False],
        )
        .collect()
    )


def q17(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Small-Quantity-Order Revenue.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    wanted = t["part"].filter(
        (pl.col("p_brand") == "Brand#23") & (pl.col("p_container") == "MED BOX")
    )
    lines = t["lineitem"].join(
        wanted.select("p_partkey"), left_on="l_partkey", right_on="p_partkey"
    )
    average = lines.group_by("l_partkey").agg((0.2 * pl.mean("l_quantity")).alias("threshold"))
    return (
        lines.join(average, on="l_partkey")
        .filter(pl.col("l_quantity") < pl.col("threshold"))
        .select((pl.sum("l_extendedprice") / 7.0).alias("avg_yearly"))
        .collect()
    )


def q18(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Large Volume Customer.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    heavy = (
        t["lineitem"]
        .group_by("l_orderkey")
        .agg(pl.sum("l_quantity").alias("total"))
        .filter(pl.col("total") > 300)
        .select("l_orderkey")
    )
    return (
        t["orders"]
        .join(heavy, left_on="o_orderkey", right_on="l_orderkey", how="semi")
        .join(t["customer"], left_on="o_custkey", right_on="c_custkey")
        .join(t["lineitem"], left_on="o_orderkey", right_on="l_orderkey")
        .group_by("c_name", "o_custkey", "o_orderkey", "o_orderdate", "o_totalprice")
        # The specification's select list carries no alias here, so the column
        # is named for the expression, which is what DuckDB running the official
        # text produces and therefore what the cross engine check compares.
        .agg(pl.sum("l_quantity").alias("sum(l_quantity)"))
        .select(
            "c_name",
            pl.col("o_custkey").alias("c_custkey"),
            "o_orderkey",
            "o_orderdate",
            "o_totalprice",
            "sum(l_quantity)",
        )
        .sort(["o_totalprice", "o_orderdate"], descending=[True, False])
        .head(100)
        .collect()
    )


def q19(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Discounted Revenue.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    joined = (
        t["lineitem"]
        .filter(
            pl.col("l_shipmode").is_in(["AIR", "AIR REG"])
            & (pl.col("l_shipinstruct") == "DELIVER IN PERSON")
        )
        .join(t["part"], left_on="l_partkey", right_on="p_partkey")
    )
    first = (
        (pl.col("p_brand") == "Brand#12")
        & pl.col("p_container").is_in(["SM CASE", "SM BOX", "SM PACK", "SM PKG"])
        & (pl.col("l_quantity") >= 1)
        & (pl.col("l_quantity") <= 11)
        & (pl.col("p_size") >= 1)
        & (pl.col("p_size") <= 5)
    )
    second = (
        (pl.col("p_brand") == "Brand#23")
        & pl.col("p_container").is_in(["MED BAG", "MED BOX", "MED PKG", "MED PACK"])
        & (pl.col("l_quantity") >= 10)
        & (pl.col("l_quantity") <= 20)
        & (pl.col("p_size") >= 1)
        & (pl.col("p_size") <= 10)
    )
    third = (
        (pl.col("p_brand") == "Brand#34")
        & pl.col("p_container").is_in(["LG CASE", "LG BOX", "LG PACK", "LG PKG"])
        & (pl.col("l_quantity") >= 20)
        & (pl.col("l_quantity") <= 30)
        & (pl.col("p_size") >= 1)
        & (pl.col("p_size") <= 15)
    )
    return (
        joined.filter(first | second | third)
        .select((pl.col("l_extendedprice") * (1 - pl.col("l_discount"))).sum().alias("revenue"))
        .collect()
    )


def q20(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Potential Part Promotion.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    forest = t["part"].filter(pl.col("p_name").str.starts_with("forest"))
    shipped = (
        t["lineitem"]
        .filter(
            (pl.col("l_shipdate") >= date(1994, 1, 1)) & (pl.col("l_shipdate") < date(1995, 1, 1))
        )
        .group_by("l_partkey", "l_suppkey")
        .agg((0.5 * pl.sum("l_quantity")).alias("threshold"))
    )
    candidates = (
        t["partsupp"]
        .join(forest.select("p_partkey"), left_on="ps_partkey", right_on="p_partkey")
        .join(
            shipped,
            left_on=["ps_partkey", "ps_suppkey"],
            right_on=["l_partkey", "l_suppkey"],
        )
        .filter(pl.col("ps_availqty") > pl.col("threshold"))
        .select("ps_suppkey")
        .unique()
    )
    return (
        t["supplier"]
        .join(t["nation"], left_on="s_nationkey", right_on="n_nationkey")
        .filter(pl.col("n_name") == "CANADA")
        .join(candidates, left_on="s_suppkey", right_on="ps_suppkey", how="semi")
        .select("s_name", "s_address")
        .sort("s_name")
        .collect()
    )


def q21(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Suppliers Who Kept Orders Waiting.

    The two self joins become counts of distinct suppliers per order, one over
    every line and one over the late lines. That is the same predicate: there is
    another supplier on the order, and no other supplier on it was late.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"]
    suppliers_per_order = lineitem.group_by("l_orderkey").agg(
        pl.col("l_suppkey").n_unique().alias("distinct_suppliers")
    )
    late_per_order = (
        lineitem.filter(pl.col("l_receiptdate") > pl.col("l_commitdate"))
        .group_by("l_orderkey")
        .agg(pl.col("l_suppkey").n_unique().alias("distinct_late_suppliers"))
    )
    return (
        lineitem.filter(pl.col("l_receiptdate") > pl.col("l_commitdate"))
        .join(suppliers_per_order, on="l_orderkey")
        .join(late_per_order, on="l_orderkey")
        .filter((pl.col("distinct_suppliers") > 1) & (pl.col("distinct_late_suppliers") == 1))
        .join(t["orders"], left_on="l_orderkey", right_on="o_orderkey")
        .filter(pl.col("o_orderstatus") == "F")
        .join(t["supplier"], left_on="l_suppkey", right_on="s_suppkey")
        .join(t["nation"], left_on="s_nationkey", right_on="n_nationkey")
        .filter(pl.col("n_name") == "SAUDI ARABIA")
        .group_by("s_name")
        .agg(pl.len().alias("numwait"))
        .sort(["numwait", "s_name"], descending=[True, False])
        .head(100)
        .collect()
    )


def q22(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Global Sales Opportunity.

    Args:
        t: The scanned tables.

    Returns:
        The answer.
    """
    codes = ["13", "31", "23", "29", "30", "18", "17"]
    customer = t["customer"].with_columns(pl.col("c_phone").str.slice(0, 2).alias("cntrycode"))
    selected = customer.filter(pl.col("cntrycode").is_in(codes))
    average = selected.filter(pl.col("c_acctbal") > 0.0).select(
        pl.mean("c_acctbal").alias("avg_acctbal")
    )
    return (
        selected.join(average, how="cross")
        .filter(pl.col("c_acctbal") > pl.col("avg_acctbal"))
        .join(
            t["orders"].select("o_custkey"),
            left_on="c_custkey",
            right_on="o_custkey",
            how="anti",
        )
        .group_by("cntrycode")
        .agg(
            pl.len().alias("numcust"),
            pl.sum("c_acctbal").alias("totacctbal"),
        )
        .sort("cntrycode")
        .collect()
    )


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
    "q11": q11,
    "q12": q12,
    "q13": q13,
    "q14": q14,
    "q15": q15,
    "q16": q16,
    "q17": q17,
    "q18": q18,
    "q19": q19,
    "q20": q20,
    "q21": q21,
    "q22": q22,
}
