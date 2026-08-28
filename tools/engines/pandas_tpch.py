"""The twenty two TPC-H queries in pandas.

pandas has no query planner, so every one of these is a hand written plan. That is
not a complaint about pandas, it is the measurement: the gap between this and
Polars or DuckDB on the same query is what a planner is worth, and it is the gap
firepanda has to close on kernels alone until it grows one of its own.

The plans here are written the way a competent pandas user would write them, which
means filters before joins, only the needed columns carried through, and no
`apply`. Writing them badly and then reporting the result would be dishonest in a
direction that flatters us. Where pandas still loses by a large factor, that is
the real answer and it goes in the table.

Tables are read once in the load step with `dtype_backend="pyarrow"`, which is the
fastest pandas 3 configuration and the one a user chasing performance would pick.
Unlike the Polars engine, the read cannot be folded into the query, so the load
time is reported separately and the report says which engines carry the scan
inside the timed region and which do not.
"""

from __future__ import annotations

import pandas as pd

# The specification's validation dates, as pandas timestamps built once.
D = pd.Timestamp


def q1(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Pricing Summary Report.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"]
    frame = lineitem[lineitem["l_shipdate"] <= D("1998-09-02")].copy()
    frame["disc_price"] = frame["l_extendedprice"] * (1 - frame["l_discount"])
    frame["charge"] = frame["disc_price"] * (1 + frame["l_tax"])
    grouped = frame.groupby(["l_returnflag", "l_linestatus"], observed=True, sort=True)
    out = grouped.agg(
        sum_qty=("l_quantity", "sum"),
        sum_base_price=("l_extendedprice", "sum"),
        sum_disc_price=("disc_price", "sum"),
        sum_charge=("charge", "sum"),
        avg_qty=("l_quantity", "mean"),
        avg_price=("l_extendedprice", "mean"),
        avg_disc=("l_discount", "mean"),
        count_order=("l_quantity", "size"),
    )
    return out.reset_index()


def q2(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Minimum Cost Supplier.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    region = t["region"][t["region"]["r_name"] == "EUROPE"]
    nation = t["nation"].merge(region, left_on="n_regionkey", right_on="r_regionkey")
    supplier = t["supplier"].merge(nation, left_on="s_nationkey", right_on="n_nationkey")
    partsupp = t["partsupp"].merge(supplier, left_on="ps_suppkey", right_on="s_suppkey")
    part = t["part"]
    wanted = part[(part["p_size"] == 15) & part["p_type"].str.endswith("BRASS")]
    joined = partsupp.merge(wanted, left_on="ps_partkey", right_on="p_partkey")

    cheapest = joined.groupby("ps_partkey", observed=True)["ps_supplycost"].min()
    out = joined[joined["ps_supplycost"] == joined["ps_partkey"].map(cheapest)]
    out = out[
        [
            "s_acctbal",
            "s_name",
            "n_name",
            "p_partkey",
            "p_mfgr",
            "s_address",
            "s_phone",
            "s_comment",
        ]
    ]
    return out.sort_values(
        ["s_acctbal", "n_name", "s_name", "p_partkey"],
        ascending=[False, True, True, True],
    ).head(100)


def q3(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Shipping Priority.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    cutoff = D("1995-03-15")
    customer = t["customer"]
    customer = customer[customer["c_mktsegment"] == "BUILDING"][["c_custkey"]]
    orders = t["orders"]
    orders = orders[orders["o_orderdate"] < cutoff][
        ["o_orderkey", "o_custkey", "o_orderdate", "o_shippriority"]
    ]
    lineitem = t["lineitem"]
    lineitem = lineitem[lineitem["l_shipdate"] > cutoff][
        ["l_orderkey", "l_extendedprice", "l_discount"]
    ]

    frame = customer.merge(orders, left_on="c_custkey", right_on="o_custkey").merge(
        lineitem, left_on="o_orderkey", right_on="l_orderkey"
    )
    frame["revenue"] = frame["l_extendedprice"] * (1 - frame["l_discount"])
    out = (
        frame.groupby(["l_orderkey", "o_orderdate", "o_shippriority"], observed=True)["revenue"]
        .sum()
        .reset_index()
    )
    out = out.sort_values(["revenue", "o_orderdate"], ascending=[False, True]).head(10)
    # The select list order is part of the answer, and `reset_index` puts the
    # grouping keys first.
    return out[["l_orderkey", "revenue", "o_orderdate", "o_shippriority"]]


def q4(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Order Priority Checking.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"]
    late = lineitem[lineitem["l_commitdate"] < lineitem["l_receiptdate"]][
        ["l_orderkey"]
    ].drop_duplicates()
    orders = t["orders"]
    window = orders[
        (orders["o_orderdate"] >= D("1993-07-01")) & (orders["o_orderdate"] < D("1993-10-01"))
    ][["o_orderkey", "o_orderpriority"]]
    frame = window.merge(late, left_on="o_orderkey", right_on="l_orderkey")
    out = (
        frame.groupby("o_orderpriority", observed=True, sort=True)
        .size()
        .reset_index(name="order_count")
    )
    return out


def q5(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Local Supplier Volume.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    region = t["region"][t["region"]["r_name"] == "ASIA"][["r_regionkey"]]
    nation = t["nation"].merge(region, left_on="n_regionkey", right_on="r_regionkey")
    orders = t["orders"]
    orders = orders[
        (orders["o_orderdate"] >= D("1994-01-01")) & (orders["o_orderdate"] < D("1995-01-01"))
    ][["o_orderkey", "o_custkey"]]

    frame = (
        t["customer"][["c_custkey", "c_nationkey"]]
        .merge(nation, left_on="c_nationkey", right_on="n_nationkey")
        .merge(orders, left_on="c_custkey", right_on="o_custkey")
        .merge(
            t["lineitem"][["l_orderkey", "l_suppkey", "l_extendedprice", "l_discount"]],
            left_on="o_orderkey",
            right_on="l_orderkey",
        )
        .merge(
            t["supplier"][["s_suppkey", "s_nationkey"]],
            left_on=["l_suppkey", "n_nationkey"],
            right_on=["s_suppkey", "s_nationkey"],
        )
    )
    frame["revenue"] = frame["l_extendedprice"] * (1 - frame["l_discount"])
    out = frame.groupby("n_name", observed=True)["revenue"].sum().reset_index()
    return out.sort_values("revenue", ascending=False)


def q6(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Forecasting Revenue Change.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"]
    selected = lineitem[
        (lineitem["l_shipdate"] >= D("1994-01-01"))
        & (lineitem["l_shipdate"] < D("1995-01-01"))
        & (lineitem["l_discount"] >= 0.05)
        & (lineitem["l_discount"] <= 0.07)
        & (lineitem["l_quantity"] < 24)
    ]
    revenue = (selected["l_extendedprice"] * selected["l_discount"]).sum()
    return pd.DataFrame({"revenue": [float(revenue)]})


def q7(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Volume Shipping.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    nation = t["nation"]
    pair = nation[nation["n_name"].isin(["FRANCE", "GERMANY"])][["n_nationkey", "n_name"]]
    lineitem = t["lineitem"]
    shipped = lineitem[
        (lineitem["l_shipdate"] >= D("1995-01-01")) & (lineitem["l_shipdate"] <= D("1996-12-31"))
    ][["l_orderkey", "l_suppkey", "l_extendedprice", "l_discount", "l_shipdate"]]

    frame = (
        shipped.merge(
            t["supplier"][["s_suppkey", "s_nationkey"]],
            left_on="l_suppkey",
            right_on="s_suppkey",
        )
        .merge(
            pair.rename(columns={"n_name": "supp_nation"}),
            left_on="s_nationkey",
            right_on="n_nationkey",
        )
        .merge(
            t["orders"][["o_orderkey", "o_custkey"]],
            left_on="l_orderkey",
            right_on="o_orderkey",
        )
        .merge(
            t["customer"][["c_custkey", "c_nationkey"]],
            left_on="o_custkey",
            right_on="c_custkey",
        )
        .merge(
            pair.rename(columns={"n_name": "cust_nation", "n_nationkey": "cust_nationkey"}),
            left_on="c_nationkey",
            right_on="cust_nationkey",
        )
    )
    frame = frame[frame["supp_nation"] != frame["cust_nation"]].copy()
    frame["l_year"] = frame["l_shipdate"].dt.year
    frame["volume"] = frame["l_extendedprice"] * (1 - frame["l_discount"])
    out = (
        frame.groupby(["supp_nation", "cust_nation", "l_year"], observed=True)["volume"]
        .sum()
        .reset_index(name="revenue")
    )
    return out.sort_values(["supp_nation", "cust_nation", "l_year"])


def q8(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """National Market Share.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    region = t["region"][t["region"]["r_name"] == "AMERICA"][["r_regionkey"]]
    nation = t["nation"]
    america = nation.merge(region, left_on="n_regionkey", right_on="r_regionkey")[["n_nationkey"]]
    part = t["part"]
    wanted = part[part["p_type"] == "ECONOMY ANODIZED STEEL"][["p_partkey"]]
    orders = t["orders"]
    window = orders[
        (orders["o_orderdate"] >= D("1995-01-01")) & (orders["o_orderdate"] <= D("1996-12-31"))
    ][["o_orderkey", "o_custkey", "o_orderdate"]]

    frame = (
        wanted.merge(
            t["lineitem"][
                ["l_partkey", "l_orderkey", "l_suppkey", "l_extendedprice", "l_discount"]
            ],
            left_on="p_partkey",
            right_on="l_partkey",
        )
        .merge(window, left_on="l_orderkey", right_on="o_orderkey")
        .merge(
            t["customer"][["c_custkey", "c_nationkey"]],
            left_on="o_custkey",
            right_on="c_custkey",
        )
        .merge(america, left_on="c_nationkey", right_on="n_nationkey")
        .merge(
            t["supplier"][["s_suppkey", "s_nationkey"]],
            left_on="l_suppkey",
            right_on="s_suppkey",
        )
        .merge(
            nation[["n_nationkey", "n_name"]].rename(
                columns={"n_nationkey": "supp_nationkey", "n_name": "nation"}
            ),
            left_on="s_nationkey",
            right_on="supp_nationkey",
        )
    ).copy()
    frame["o_year"] = frame["o_orderdate"].dt.year
    frame["volume"] = frame["l_extendedprice"] * (1 - frame["l_discount"])
    frame["brazil"] = frame["volume"].where(frame["nation"] == "BRAZIL", 0.0)
    grouped = frame.groupby("o_year", observed=True).agg(
        brazil=("brazil", "sum"), total=("volume", "sum")
    )
    grouped["mkt_share"] = grouped["brazil"] / grouped["total"]
    return grouped.reset_index()[["o_year", "mkt_share"]].sort_values("o_year")


def q9(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Product Type Profit Measure.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    part = t["part"]
    green = part[part["p_name"].str.contains("green", regex=False)][["p_partkey"]]
    frame = (
        green.merge(
            t["lineitem"][
                [
                    "l_partkey",
                    "l_suppkey",
                    "l_orderkey",
                    "l_quantity",
                    "l_extendedprice",
                    "l_discount",
                ]
            ],
            left_on="p_partkey",
            right_on="l_partkey",
        )
        .merge(
            t["supplier"][["s_suppkey", "s_nationkey"]],
            left_on="l_suppkey",
            right_on="s_suppkey",
        )
        .merge(
            t["partsupp"][["ps_partkey", "ps_suppkey", "ps_supplycost"]],
            left_on=["l_partkey", "l_suppkey"],
            right_on=["ps_partkey", "ps_suppkey"],
        )
        .merge(
            t["orders"][["o_orderkey", "o_orderdate"]],
            left_on="l_orderkey",
            right_on="o_orderkey",
        )
        .merge(
            t["nation"][["n_nationkey", "n_name"]],
            left_on="s_nationkey",
            right_on="n_nationkey",
        )
    ).copy()
    frame["o_year"] = frame["o_orderdate"].dt.year
    frame["amount"] = (
        frame["l_extendedprice"] * (1 - frame["l_discount"])
        - frame["ps_supplycost"] * frame["l_quantity"]
    )
    out = (
        frame.groupby(["n_name", "o_year"], observed=True)["amount"]
        .sum()
        .reset_index(name="sum_profit")
        .rename(columns={"n_name": "nation"})
    )
    return out.sort_values(["nation", "o_year"], ascending=[True, False])


def q10(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Returned Item Reporting.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    orders = t["orders"]
    window = orders[
        (orders["o_orderdate"] >= D("1993-10-01")) & (orders["o_orderdate"] < D("1994-01-01"))
    ][["o_orderkey", "o_custkey"]]
    lineitem = t["lineitem"]
    returned = lineitem[lineitem["l_returnflag"] == "R"][
        ["l_orderkey", "l_extendedprice", "l_discount"]
    ]

    frame = (
        t["customer"]
        .merge(window, left_on="c_custkey", right_on="o_custkey")
        .merge(returned, left_on="o_orderkey", right_on="l_orderkey")
        .merge(
            t["nation"][["n_nationkey", "n_name"]],
            left_on="c_nationkey",
            right_on="n_nationkey",
        )
    ).copy()
    frame["revenue"] = frame["l_extendedprice"] * (1 - frame["l_discount"])
    out = (
        frame.groupby(
            [
                "c_custkey",
                "c_name",
                "c_acctbal",
                "c_phone",
                "n_name",
                "c_address",
                "c_comment",
            ],
            observed=True,
        )["revenue"]
        .sum()
        .reset_index()
    )
    out = out[
        [
            "c_custkey",
            "c_name",
            "revenue",
            "c_acctbal",
            "n_name",
            "c_address",
            "c_phone",
            "c_comment",
        ]
    ]
    return out.sort_values("revenue", ascending=False).head(20)


def q11(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Important Stock Identification.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    nation = t["nation"]
    germany = nation[nation["n_name"] == "GERMANY"][["n_nationkey"]]
    frame = (
        t["partsupp"]
        .merge(
            t["supplier"][["s_suppkey", "s_nationkey"]],
            left_on="ps_suppkey",
            right_on="s_suppkey",
        )
        .merge(germany, left_on="s_nationkey", right_on="n_nationkey")
    ).copy()
    frame["value"] = frame["ps_supplycost"] * frame["ps_availqty"]
    threshold = float(frame["value"].sum()) * 0.0001
    out = frame.groupby("ps_partkey", observed=True)["value"].sum().reset_index()
    out = out[out["value"] > threshold]
    return out.sort_values("value", ascending=False)


def q12(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Shipping Modes and Order Priority.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"]
    selected = lineitem[
        lineitem["l_shipmode"].isin(["MAIL", "SHIP"])
        & (lineitem["l_commitdate"] < lineitem["l_receiptdate"])
        & (lineitem["l_shipdate"] < lineitem["l_commitdate"])
        & (lineitem["l_receiptdate"] >= D("1994-01-01"))
        & (lineitem["l_receiptdate"] < D("1995-01-01"))
    ][["l_orderkey", "l_shipmode"]]
    frame = selected.merge(
        t["orders"][["o_orderkey", "o_orderpriority"]],
        left_on="l_orderkey",
        right_on="o_orderkey",
    ).copy()
    urgent = frame["o_orderpriority"].isin(["1-URGENT", "2-HIGH"])
    frame["high_line_count"] = urgent.astype("int64")
    frame["low_line_count"] = (~urgent).astype("int64")
    out = (
        frame.groupby("l_shipmode", observed=True, sort=True)[["high_line_count", "low_line_count"]]
        .sum()
        .reset_index()
    )
    return out


def q13(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Customer Distribution.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    orders = t["orders"]
    kept = orders[~orders["o_comment"].str.contains("special.*requests", regex=True)][
        ["o_orderkey", "o_custkey"]
    ]
    frame = t["customer"][["c_custkey"]].merge(
        kept, left_on="c_custkey", right_on="o_custkey", how="left"
    )
    per_customer = (
        frame.groupby("c_custkey", observed=True)["o_orderkey"].count().reset_index(name="c_count")
    )
    out = per_customer.groupby("c_count", observed=True).size().reset_index(name="custdist")
    return out.sort_values(["custdist", "c_count"], ascending=[False, False])


def q14(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Promotion Effect.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"]
    window = lineitem[
        (lineitem["l_shipdate"] >= D("1995-09-01")) & (lineitem["l_shipdate"] < D("1995-10-01"))
    ][["l_partkey", "l_extendedprice", "l_discount"]]
    frame = window.merge(
        t["part"][["p_partkey", "p_type"]], left_on="l_partkey", right_on="p_partkey"
    ).copy()
    frame["revenue"] = frame["l_extendedprice"] * (1 - frame["l_discount"])
    promo = frame["revenue"].where(frame["p_type"].str.startswith("PROMO"), 0.0)
    value = 100.0 * float(promo.sum()) / float(frame["revenue"].sum())
    return pd.DataFrame({"promo_revenue": [value]})


def q15(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Top Supplier.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"]
    window = lineitem[
        (lineitem["l_shipdate"] >= D("1996-01-01")) & (lineitem["l_shipdate"] < D("1996-04-01"))
    ].copy()
    window["revenue"] = window["l_extendedprice"] * (1 - window["l_discount"])
    revenue = (
        window.groupby("l_suppkey", observed=True)["revenue"]
        .sum()
        .reset_index(name="total_revenue")
    )
    best = revenue["total_revenue"].max()
    top = revenue[revenue["total_revenue"] == best]
    out = t["supplier"].merge(top, left_on="s_suppkey", right_on="l_suppkey")
    return out[["s_suppkey", "s_name", "s_address", "s_phone", "total_revenue"]].sort_values(
        "s_suppkey"
    )


def q16(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Parts/Supplier Relationship.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    supplier = t["supplier"]
    complained = set(
        supplier[supplier["s_comment"].str.contains("Customer.*Complaints", regex=True)][
            "s_suppkey"
        ]
        .to_numpy()
        .tolist()
    )
    part = t["part"]
    wanted = part[
        (part["p_brand"] != "Brand#45")
        & ~part["p_type"].str.startswith("MEDIUM POLISHED")
        & part["p_size"].isin([49, 14, 23, 45, 19, 3, 36, 9])
    ][["p_partkey", "p_brand", "p_type", "p_size"]]
    frame = wanted.merge(
        t["partsupp"][["ps_partkey", "ps_suppkey"]],
        left_on="p_partkey",
        right_on="ps_partkey",
    )
    frame = frame[~frame["ps_suppkey"].isin(complained)]
    out = (
        frame.groupby(["p_brand", "p_type", "p_size"], observed=True)["ps_suppkey"]
        .nunique()
        .reset_index(name="supplier_cnt")
    )
    return out.sort_values(
        ["supplier_cnt", "p_brand", "p_type", "p_size"],
        ascending=[False, True, True, True],
    )


def q17(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Small-Quantity-Order Revenue.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    part = t["part"]
    wanted = part[(part["p_brand"] == "Brand#23") & (part["p_container"] == "MED BOX")][
        ["p_partkey"]
    ]
    lines = t["lineitem"][["l_partkey", "l_quantity", "l_extendedprice"]].merge(
        wanted, left_on="l_partkey", right_on="p_partkey"
    )
    threshold = 0.2 * lines.groupby("l_partkey", observed=True)["l_quantity"].mean()
    selected = lines[lines["l_quantity"] < lines["l_partkey"].map(threshold)]
    value = float(selected["l_extendedprice"].sum()) / 7.0
    return pd.DataFrame({"avg_yearly": [value]})


def q18(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Large Volume Customer.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"][["l_orderkey", "l_quantity"]]
    totals = lineitem.groupby("l_orderkey", observed=True)["l_quantity"].sum()
    heavy = totals[totals > 300].index
    frame = (
        t["orders"][["o_orderkey", "o_custkey", "o_orderdate", "o_totalprice"]][
            t["orders"]["o_orderkey"].isin(heavy)
        ]
        .merge(
            t["customer"][["c_custkey", "c_name"]],
            left_on="o_custkey",
            right_on="c_custkey",
        )
        .merge(lineitem, left_on="o_orderkey", right_on="l_orderkey")
    )
    out = (
        frame.groupby(
            ["c_name", "c_custkey", "o_orderkey", "o_orderdate", "o_totalprice"],
            observed=True,
        )["l_quantity"]
        .sum()
        # No alias in the specification, so the column is named for the
        # expression, matching DuckDB running the official text.
        .reset_index(name="sum(l_quantity)")
    )
    return out.sort_values(["o_totalprice", "o_orderdate"], ascending=[False, True]).head(100)


def q19(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Discounted Revenue.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"]
    narrowed = lineitem[
        lineitem["l_shipmode"].isin(["AIR", "AIR REG"])
        & (lineitem["l_shipinstruct"] == "DELIVER IN PERSON")
    ][["l_partkey", "l_quantity", "l_extendedprice", "l_discount"]]
    frame = narrowed.merge(
        t["part"][["p_partkey", "p_brand", "p_container", "p_size"]],
        left_on="l_partkey",
        right_on="p_partkey",
    )
    first = (
        (frame["p_brand"] == "Brand#12")
        & frame["p_container"].isin(["SM CASE", "SM BOX", "SM PACK", "SM PKG"])
        & (frame["l_quantity"] >= 1)
        & (frame["l_quantity"] <= 11)
        & (frame["p_size"] >= 1)
        & (frame["p_size"] <= 5)
    )
    second = (
        (frame["p_brand"] == "Brand#23")
        & frame["p_container"].isin(["MED BAG", "MED BOX", "MED PKG", "MED PACK"])
        & (frame["l_quantity"] >= 10)
        & (frame["l_quantity"] <= 20)
        & (frame["p_size"] >= 1)
        & (frame["p_size"] <= 10)
    )
    third = (
        (frame["p_brand"] == "Brand#34")
        & frame["p_container"].isin(["LG CASE", "LG BOX", "LG PACK", "LG PKG"])
        & (frame["l_quantity"] >= 20)
        & (frame["l_quantity"] <= 30)
        & (frame["p_size"] >= 1)
        & (frame["p_size"] <= 15)
    )
    selected = frame[first | second | third]
    revenue = (selected["l_extendedprice"] * (1 - selected["l_discount"])).sum()
    return pd.DataFrame({"revenue": [float(revenue)]})


def q20(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Potential Part Promotion.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    part = t["part"]
    forest = part[part["p_name"].str.startswith("forest")][["p_partkey"]]
    lineitem = t["lineitem"]
    window = lineitem[
        (lineitem["l_shipdate"] >= D("1994-01-01")) & (lineitem["l_shipdate"] < D("1995-01-01"))
    ][["l_partkey", "l_suppkey", "l_quantity"]]
    shipped = (
        window.groupby(["l_partkey", "l_suppkey"], observed=True)["l_quantity"].sum().reset_index()
    )
    shipped["threshold"] = 0.5 * shipped["l_quantity"]

    candidates = (
        t["partsupp"][["ps_partkey", "ps_suppkey", "ps_availqty"]]
        .merge(forest, left_on="ps_partkey", right_on="p_partkey")
        .merge(
            shipped,
            left_on=["ps_partkey", "ps_suppkey"],
            right_on=["l_partkey", "l_suppkey"],
        )
    )
    candidates = candidates[candidates["ps_availqty"] > candidates["threshold"]]
    keys = set(candidates["ps_suppkey"].to_numpy().tolist())

    nation = t["nation"]
    canada = nation[nation["n_name"] == "CANADA"][["n_nationkey"]]
    supplier = t["supplier"].merge(canada, left_on="s_nationkey", right_on="n_nationkey")
    out = supplier[supplier["s_suppkey"].isin(keys)][["s_name", "s_address"]]
    return out.sort_values("s_name")


def q21(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Suppliers Who Kept Orders Waiting.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    lineitem = t["lineitem"][["l_orderkey", "l_suppkey", "l_receiptdate", "l_commitdate"]]
    per_order = (
        lineitem.groupby("l_orderkey", observed=True)["l_suppkey"]
        .nunique()
        .reset_index(name="distinct_suppliers")
    )
    late = lineitem[lineitem["l_receiptdate"] > lineitem["l_commitdate"]]
    late_per_order = (
        late.groupby("l_orderkey", observed=True)["l_suppkey"]
        .nunique()
        .reset_index(name="distinct_late_suppliers")
    )
    frame = late.merge(per_order, on="l_orderkey").merge(late_per_order, on="l_orderkey")
    frame = frame[(frame["distinct_suppliers"] > 1) & (frame["distinct_late_suppliers"] == 1)]
    orders = t["orders"]
    frame = frame.merge(
        orders[orders["o_orderstatus"] == "F"][["o_orderkey"]],
        left_on="l_orderkey",
        right_on="o_orderkey",
    )
    nation = t["nation"]
    saudi = nation[nation["n_name"] == "SAUDI ARABIA"][["n_nationkey"]]
    supplier = t["supplier"][["s_suppkey", "s_name", "s_nationkey"]].merge(
        saudi, left_on="s_nationkey", right_on="n_nationkey"
    )
    frame = frame.merge(supplier, left_on="l_suppkey", right_on="s_suppkey")
    out = frame.groupby("s_name", observed=True).size().reset_index(name="numwait")
    return out.sort_values(["numwait", "s_name"], ascending=[False, True]).head(100)


def q22(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Global Sales Opportunity.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    codes = ["13", "31", "23", "29", "30", "18", "17"]
    customer = t["customer"][["c_custkey", "c_phone", "c_acctbal"]].copy()
    customer["cntrycode"] = customer["c_phone"].str.slice(0, 2)
    selected = customer[customer["cntrycode"].isin(codes)]
    average = float(selected[selected["c_acctbal"] > 0.0]["c_acctbal"].mean())
    rich = selected[selected["c_acctbal"] > average]
    with_orders = set(t["orders"]["o_custkey"].to_numpy().tolist())
    without = rich[~rich["c_custkey"].isin(with_orders)]
    out = (
        without.groupby("cntrycode", observed=True, sort=True)
        .agg(numcust=("c_custkey", "size"), totacctbal=("c_acctbal", "sum"))
        .reset_index()
    )
    return out


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
