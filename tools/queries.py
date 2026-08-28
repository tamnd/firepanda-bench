#!/usr/bin/env python3
"""What the queries are, in one place, in words rather than in any engine's API.

Every engine implements this list. Keeping the list separate from the
implementations is what makes it possible to say that two engines ran the same
query, and the answer digest is what makes it possible to prove it.

The group by set is the h2oai G1 design that db-benchmark still uses. The order is
not arbitrary. It walks from the lowest cardinality to the highest, and the last
few are the ones worth watching, because a hundred groups fit in cache and ten
million do not. MojoFrame's authors lost on exactly those and diagnosed Mojo's
dictionary as the reason, which is why firepanda wrote its own hash table, and the
claim is only worth anything measured here.

The join set is smaller than the group by set on purpose. What a join costs is
decided by the shape of the right hand side far more than by anything else, so the
five queries span a right side of a thousandth, a hundredth and the whole of the
left, in both the inner and the outer form.

TPC-H is the other suite, and it is here because db-benchmark cannot fail an
optimizer. Every db-benchmark query is one group by or one join over one table, so
an engine with no planner at all can win the whole suite. TPC-H queries read up to
six tables through five joins under a filter, and the difference between engines is
mostly join order, projection pushdown and predicate pushdown rather than kernel
speed. The query text is not written here: it is read out of DuckDB's `tpch`
extension, which carries the official statements, so what the SQL engine runs is
the specification rather than someone's transcription of it.

Query names collide between the suites, since both call their first query q1, so
nothing here is looked up by bare name. The registry is keyed by suite.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Query:
    """One query, and what makes it worth running."""

    name: str
    """The identifier used in result files and tables."""

    group: str
    """Which suite group it belongs to: groupby or join."""

    description: str
    """What it computes, in words."""

    why: str
    """What it is here to expose."""

    keys: tuple[str, ...]
    """The grouping or join key columns, for reference."""

    needs: tuple[str, ...]
    """Which generated tables it reads."""

    suite: str = "db-benchmark"
    """Which suite the query belongs to."""


GROUPBY = (
    Query(
        "q1",
        "groupby",
        "sum of v1 by id1",
        "The floor. A hundred groups over a string key, all of it in cache. Any "
        "engine that is slow here is slow for a reason that has nothing to do "
        "with grouping.",
        ("id1",),
        ("groupby",),
    ),
    Query(
        "q2",
        "groupby",
        "sum of v1 by id1 and id2",
        "Ten thousand groups over two string keys. The first query where the key "
        "has to be combined rather than used directly.",
        ("id1", "id2"),
        ("groupby",),
    ),
    Query(
        "q3",
        "groupby",
        "sum of v1 and mean of v3 by id3",
        "High cardinality over a string key, and two different reductions in one "
        "pass. This is the shape that punishes a per group allocation.",
        ("id3",),
        ("groupby",),
    ),
    Query(
        "q4",
        "groupby",
        "mean of v1, v2 and v3 by id4",
        "The integer twin of q1. The gap between them is the cost of the string "
        "machinery and nothing else, which is the number firepanda most needs.",
        ("id4",),
        ("groupby",),
    ),
    Query(
        "q5",
        "groupby",
        "sum of v1, v2 and v3 by id6",
        "High cardinality over an integer key. The hash table with no string handling in the way.",
        ("id6",),
        ("groupby",),
    ),
    Query(
        "q6",
        "groupby",
        "median and standard deviation of v3 by id4 and id6",
        "A reduction that cannot be done in one pass with a running accumulator. "
        "Median needs the values kept, which is where per group memory shows up.",
        ("id4", "id6"),
        ("groupby",),
    ),
    Query(
        "q7",
        "groupby",
        "max of v1 minus min of v2 by id3",
        "Two extremes and an expression over them, at high cardinality. Cheap "
        "arithmetic, expensive grouping.",
        ("id3",),
        ("groupby",),
    ),
    Query(
        "q8",
        "groupby",
        "the two largest v3 per id6",
        "An order statistic per group rather than a reduction. The query that "
        "separates an engine with a real group by from one that fakes it with a "
        "sort.",
        ("id6",),
        ("groupby",),
    ),
    Query(
        "q9",
        "groupby",
        "squared correlation of v1 and v2 by id2 and id4",
        "Several accumulators per group at once. The one place where the cost of "
        "the reduction itself is comparable to the cost of the grouping.",
        ("id2", "id4"),
        ("groupby",),
    ),
    Query(
        "q10",
        "groupby",
        "sum of v3 and a count by all six key columns",
        "Almost every row is its own group. The worst case for a hash table, and "
        "the case a sort based group by wins if the table is bad.",
        ("id1", "id2", "id3", "id4", "id5", "id6"),
        ("groupby",),
    ),
)

JOIN = (
    Query(
        "j1",
        "join",
        "inner join against the small right table on id1",
        "A right side of a thousandth of the left. Small enough to stay in cache, "
        "so this measures the probe loop and nothing else.",
        ("id1",),
        ("left", "right_small"),
    ),
    Query(
        "j2",
        "join",
        "inner join against the medium right table on id2",
        "A right side of a hundredth. The build side no longer fits in L2, which "
        "is where a bad hash table starts to show.",
        ("id2",),
        ("left", "right_medium"),
    ),
    Query(
        "j3",
        "join",
        "left outer join against the medium right table on id2",
        "The same shape with unmatched rows kept, so the null handling is on the "
        "critical path rather than off it.",
        ("id2",),
        ("left", "right_medium"),
    ),
    Query(
        "j4",
        "join",
        "inner join against the big right table on id3",
        "A right side the size of the left, every key distinct. Nothing fits "
        "anywhere and the join is bound by memory rather than by arithmetic.",
        ("id3",),
        ("left", "right_big"),
    ),
    Query(
        "j5",
        "join",
        "left outer join against the big right table on id3",
        "The largest and least forgiving of the five, and the one where a "
        "materialized result is large enough that producing it is part of the "
        "measurement.",
        ("id3",),
        ("left", "right_big"),
    ),
)

# The eight TPC-H tables, in the order the specification lists them.
TPCH_TABLES = (
    "customer",
    "lineitem",
    "nation",
    "orders",
    "part",
    "partsupp",
    "region",
    "supplier",
)


def _tpch(name: str, title: str, why: str, needs: tuple[str, ...]) -> Query:
    """Builds one TPC-H query entry.

    Args:
        name: The query name, q1 through q22.
        title: The name the specification gives it.
        why: What it is here to expose.
        needs: Which tables it reads.

    Returns:
        The query.
    """
    return Query(name, "tpch", title, why, (), needs, suite="tpch")


TPCH = (
    _tpch(
        "q1",
        "Pricing Summary Report",
        "One table, one filter, one group by over four distinct keys, eight "
        "aggregates. No join and no planning to speak of, so this is the closest "
        "TPC-H comes to a raw kernel measurement and it is the query where a "
        "vectorized engine should be uncatchable.",
        ("lineitem",),
    ),
    _tpch(
        "q2",
        "Minimum Cost Supplier",
        "A correlated subquery over five tables. An engine that cannot decorrelate "
        "runs the inner query once per outer row and loses by orders of magnitude "
        "rather than by percentages.",
        ("part", "supplier", "partsupp", "nation", "region"),
    ),
    _tpch(
        "q3",
        "Shipping Priority",
        "Three tables, two filters on dates, a group by and a top ten. The "
        "canonical test of whether filters run before joins or after them.",
        ("customer", "orders", "lineitem"),
    ),
    _tpch(
        "q4",
        "Order Priority Checking",
        "An existence semi join. An engine that materializes it as a full join and "
        "then deduplicates does far more work than the query asks for.",
        ("orders", "lineitem"),
    ),
    _tpch(
        "q5",
        "Local Supplier Volume",
        "Six tables in one join chain with a single selective filter at the far "
        "end. Join order is the whole query.",
        ("customer", "orders", "lineitem", "supplier", "nation", "region"),
    ),
    _tpch(
        "q6",
        "Forecasting Revenue Change",
        "One table, three range predicates, one scalar sum. Nothing but scan and "
        "filter throughput, which makes it the cleanest read on how much of the "
        "column an engine avoided touching.",
        ("lineitem",),
    ),
    _tpch(
        "q7",
        "Volume Shipping",
        "A join chain with a disjunctive nation pair predicate, which most "
        "planners cannot push down and have to apply after the join.",
        ("supplier", "lineitem", "orders", "customer", "nation"),
    ),
    _tpch(
        "q8",
        "National Market Share",
        "Eight tables, and a ratio of two conditional sums over the result. The "
        "widest join graph in the suite.",
        ("part", "supplier", "lineitem", "orders", "customer", "nation", "region"),
    ),
    _tpch(
        "q9",
        "Product Type Profit Measure",
        "Six tables with a substring predicate on part name that no index helps "
        "with, so the join is run against an unselective filter. Usually the "
        "slowest query in the suite.",
        ("part", "supplier", "lineitem", "partsupp", "orders", "nation"),
    ),
    _tpch(
        "q10",
        "Returned Item Reporting",
        "Four tables, a group by on eight columns and a top twenty. A wide "
        "grouping key over a large intermediate.",
        ("customer", "orders", "lineitem", "nation"),
    ),
    _tpch(
        "q11",
        "Important Stock Identification",
        "A group by whose HAVING clause compares against a scalar subquery over "
        "the same tables. The join has to run twice unless the engine notices.",
        ("partsupp", "supplier", "nation"),
    ),
    _tpch(
        "q12",
        "Shipping Modes and Order Priority",
        "A join under conditional aggregation with three date predicates that must "
        "be evaluated in order. Cheap if pushed down and expensive if not.",
        ("orders", "lineitem"),
    ),
    _tpch(
        "q13",
        "Customer Distribution",
        "A left outer join with a NOT LIKE predicate on the join condition itself, "
        "then a group by of a group by. The UDF-shaped query, and the one where "
        "compiling the user's predicate into the pipeline instead of calling it "
        "through an interpreter is worth something measurable.",
        ("customer", "orders"),
    ),
    _tpch(
        "q14",
        "Promotion Effect",
        "Two tables and a ratio of conditional sums. Short, and a good check that "
        "an engine is not paying for columns the query never reads.",
        ("lineitem", "part"),
    ),
    _tpch(
        "q15",
        "Top Supplier",
        "A view, or a common table expression, referenced twice. An engine that "
        "does not reuse it computes the revenue aggregate twice.",
        ("supplier", "lineitem"),
    ),
    _tpch(
        "q16",
        "Parts/Supplier Relationship",
        "An anti join followed by a count of distinct values per group. Two "
        "operations that both want a hash table, back to back.",
        ("partsupp", "part", "supplier"),
    ),
    _tpch(
        "q17",
        "Small-Quantity-Order Revenue",
        "A correlated subquery computing a per part average. The second "
        "decorrelation test, and a harder one than q2 because the correlation is "
        "on the aggregate rather than on the filter.",
        ("lineitem", "part"),
    ),
    _tpch(
        "q18",
        "Large Volume Customer",
        "A group by over the whole of lineitem with a HAVING clause, feeding a "
        "join back into the same table. High cardinality aggregation is the "
        "explicit reason firepanda wrote its own hash table instead of using "
        "Mojo's dictionary, and this is the standard query that decides whether "
        "that was worth doing.",
        ("customer", "orders", "lineitem"),
    ),
    _tpch(
        "q19",
        "Discounted Revenue",
        "Three alternative conjunctions joined by OR, each with six predicates. "
        "A planner either turns this into one pass or into three.",
        ("lineitem", "part"),
    ),
    _tpch(
        "q20",
        "Potential Part Promotion",
        "Nested subqueries three deep, with a LIKE predicate at the top. The "
        "deepest nesting in the suite.",
        ("supplier", "nation", "partsupp", "part", "lineitem"),
    ),
    _tpch(
        "q21",
        "Suppliers Who Kept Orders Waiting",
        "A self join of lineitem against itself twice, once as an existence check "
        "and once as an absence check. The most expensive intermediate in the "
        "suite and the query most likely to run a machine out of memory.",
        ("supplier", "lineitem", "orders", "nation"),
    ),
    _tpch(
        "q22",
        "Global Sales Opportunity",
        "A substring predicate on a key column, an average over a filtered "
        "subquery, and an anti join. Closes the suite with the one string "
        "operation in it that is not an equality.",
        ("customer", "orders"),
    ),
)

DB_BENCHMARK = GROUPBY + JOIN

# Every suite the harness knows how to run, and the queries in each. The names
# collide across suites on purpose, because renaming TPC-H's q1 would make the
# result file harder to check against a published one.
SUITES = {
    "db-benchmark": DB_BENCHMARK,
    "tpch": TPCH,
}

# The groups a `--queries` argument may name, per suite.
GROUPS = {
    "db-benchmark": ("groupby", "join"),
    "tpch": ("tpch",),
}


def for_suite(suite: str) -> tuple[Query, ...]:
    """Returns every query in a suite.

    Args:
        suite: The suite name.

    Returns:
        The queries, in order.

    Raises:
        SystemExit: If the suite is not one the harness runs.
    """
    if suite not in SUITES:
        raise SystemExit(f"unknown suite '{suite}'. Known: {', '.join(SUITES)}")
    return SUITES[suite]


def lookup(suite: str, name: str) -> Query:
    """Finds one query by suite and name.

    Args:
        suite: The suite name.
        name: The query name.

    Returns:
        The query.

    Raises:
        SystemExit: If either name is unknown.
    """
    for query in for_suite(suite):
        if query.name == name:
            return query
    raise SystemExit(f"{suite} has no query '{name}'")


def select(names: str, suite: str = "db-benchmark") -> list[Query]:
    """Resolves a query selection from the command line.

    Args:
        names: A comma separated list of query names or group names, or "all".
        suite: Which suite the names belong to.

    Returns:
        The queries, in the order they are defined.

    Raises:
        SystemExit: If a name matches nothing in the suite.
    """
    available = for_suite(suite)
    if names in ("all", ""):
        return list(available)
    by_name = {q.name: q for q in available}
    wanted: set[str] = set()
    for token in names.split(","):
        token = token.strip()
        if token in GROUPS[suite]:
            wanted.update(q.name for q in available if q.group == token)
        elif token in by_name:
            wanted.add(token)
        else:
            raise SystemExit(
                f"'{token}' is not a query in {suite}. Known: "
                f"{', '.join(by_name)}, {', '.join(GROUPS[suite])}, all"
            )
    return [q for q in available if q.name in wanted]
