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
queries span a right side of a thousandth, a hundredth and the whole of the left,
in both the inner and the outer form.

j1 through j5 are upstream db-benchmark's five, in its order and on its key types:
small inner on an integer, medium inner on an integer, medium outer on an integer,
medium inner on a character key, big inner on an integer. j6 is one extra, a big
outer, which upstream does not have and which is kept because it is the only place
an outer join meets a build side too large for any cache.

j4 used to be the big inner and j5 the big outer, and neither of them nor anything
else in the set joined on text at all. That was wrong about the suite it claims to
run, and correcting it means numbers for j4 and j5 from before the change are not
comparable with numbers from after it.

TPC-H is the other suite, and it is here because db-benchmark cannot fail an
optimizer. Every db-benchmark query is one group by or one join over one table, so
an engine with no planner at all can win the whole suite. TPC-H queries read up to
six tables through five joins under a filter, and the difference between engines is
mostly join order, projection pushdown and predicate pushdown rather than kernel
speed. The query text is not written here: it is read out of DuckDB's `tpch`
extension, which carries the official statements, so what the SQL engine runs is
the specification rather than someone's transcription of it.

Ingestion is the third suite and the smallest, and it is the only one where the
thing being measured is not a query at all. Every entry in it reads one CSV file
and materializes it, because `read_csv` is the first line of code almost every
user writes and a library that is quick at group by and slow to open a file gets
judged on the second thing. The five files are different shapes rather than five
sizes of one shape: narrow, narrow with the types declared instead of inferred,
wide, quoted, and nine tenths empty.

ClickBench is the fourth suite and it is here for the reason the other three
between them cannot cover: all of their data is generated. db-benchmark keys come
out of a counter stream, TPC-H comes out of `dbgen`, the ingestion files come out
of the same generator as db-benchmark, and every one of them is uniform by
construction. The hits table is a dump of what a real product recorded, so the
cardinalities are skewed, a handful of client addresses account for a large share
of the rows, empty strings stand in for nulls, and there are 105 columns of which
most queries touch three. Everything in this library has been tuned against
uniform generated keys and this is the first data here that has none.

The 43 are ClickBench's own, in its order and under its names, which start at
zero. That order is not a progression the way the group by set is: the suite was
assembled from a production query log, so it opens with a row count and then stops
being a sequence. Around half of it is the same shape with a different key, which
is the point, because it means the key is the variable and everything else is held
still.

Query names collide between the suites, since db-benchmark, TPC-H and ClickBench
all call something q1, so nothing here is looked up by bare name. The registry is
keyed by suite.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Query:
    """One query, and what makes it worth running."""

    name: str
    """The identifier used in result files and tables."""

    group: str
    """Which suite group it belongs to: groupby, join, tpch or csv."""

    description: str
    """What it computes, in words."""

    why: str
    """What it is here to expose."""

    keys: tuple[str, ...]
    """The grouping or join key columns, for reference. Empty for a read."""

    needs: tuple[str, ...]
    """Which generated tables it reads."""

    suite: str = "db-benchmark"
    """Which suite the query belongs to."""

    undetermined: str = ""
    """Why the statement does not determine which rows come back, or empty when it
    does. Set for thirteen ClickBench queries and for nothing else."""


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
        "inner join against the medium right table on id5",
        "The same shape as j2 with a character key instead of an integer one. "
        "This is the one join upstream db-benchmark runs on text, and the pair "
        "with j2 is a measurement of what a text key costs and of nothing else, "
        "because id5 is id2 written out and the two pair the same rows.",
        ("id5",),
        ("left", "right_medium"),
    ),
    Query(
        "j5",
        "join",
        "inner join against the big right table on id3",
        "A right side the size of the left, every key distinct. Nothing fits "
        "anywhere and the join is bound by memory rather than by arithmetic.",
        ("id3",),
        ("left", "right_big"),
    ),
    Query(
        "j6",
        "join",
        "left outer join against the big right table on id3",
        "The largest and least forgiving of the six, and the one where a "
        "materialized result is large enough that producing it is part of the "
        "measurement. Upstream stops at five and this is the extra one, kept "
        "because it is the only place an outer join meets a build side too big "
        "for any cache.",
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


def _csv(name: str, table: str, description: str, why: str) -> Query:
    """Builds one ingestion entry.

    Args:
        name: The query name.
        table: Which generated file it reads.
        description: What it does, in words.
        why: What it is here to expose.

    Returns:
        The query.
    """
    return Query(name, "csv", description, why, (), (table,), "ingestion")


INGESTION = (
    _csv(
        "csv_narrow",
        "narrow",
        "read a four column CSV, types inferred",
        "The floor, and the line of code that makes the first impression. Two "
        "integers, a float and a short string, with the types worked out from the "
        "file rather than declared, which is what a user who has just been handed "
        "a file actually writes.",
    ),
    _csv(
        "csv_narrow_typed",
        "narrow",
        "read the same four column CSV, types declared",
        "The same bytes with inference switched off. The gap between this and "
        "csv_narrow is what inference costs and nothing else, which is worth "
        "separating because in most readers it is a whole extra pass over the file "
        "and in some it is nearly free.",
    ),
    _csv(
        "csv_wide",
        "wide",
        "read a fifty column CSV, types inferred",
        "Per field cost rather than per byte cost. The file is about the size of "
        "the narrow one and has twelve times as many fields in it, so a reader "
        "that decides what to do once per field instead of once per column pays "
        "here and nowhere else in the suite.",
    ),
    _csv(
        "csv_quoted",
        "quoted",
        "read a CSV whose text fields carry delimiters, newlines and quotes",
        "The case that stops a reader from splitting the file on newlines and "
        "going home. Every note in the file contains a comma, an embedded line "
        "feed and a pair of quotes, so a reader that gets the quoting wrong "
        "returns a different number of rows and the agreement check catches it "
        "rather than the timing flattering it.",
    ),
    _csv(
        "csv_nulls",
        "nulls",
        "read a CSV that is nine tenths empty fields",
        "Null handling, which is where a reader that allocates or branches per "
        "value shows it. Every sparse column is numeric on purpose: engines "
        "disagree about whether an empty text field is a null or an empty string, "
        "both answers are defensible, and a file that asked the question would "
        "report a difference in semantics as a difference in the answer.",
    ),
)

# The types `csv_narrow_typed` declares, in the file's column order and in nobody's
# type system. Every engine maps this to its own names, and it lives here rather
# than in four engine modules so that "declared" means the same thing in each of
# them: an engine that quietly declared a narrower integer would be reading a
# different file from everyone else.
NARROW_SCHEMA = (
    ("id", "int64"),
    ("pair", "int64"),
    ("score", "float64"),
    ("label", "string"),
)


# The queries whose answers the published statements do not determine, and how
# many rows tie at the boundary the limit cuts on, measured at 1M.
#
# This is the fourth of the five traps in `suites/clickbench/README.md` and it is
# the one that cannot be fixed by making the ports agree, because agreeing would
# mean adding an order by the query does not have and then measuring a different
# query. Every one of these ends `ORDER BY <something> LIMIT 10`, except q17 which
# never sorts at all, and the answer is determined only when the ordering
# expression can tell the last included row apart from the first excluded one. On
# this data it often cannot: `WatchID` is nearly unique so almost every group in
# q31 has a count of one, and q32 takes any ten rows of the table.
#
# The counts come from ranking the whole answer by its own ordering expression and
# counting the rows sharing the boundary rank, which is a fact about the data and
# so has to be measured rather than reasoned about. They were measured at 1M. The
# set is a function of the size, since which rows tie at row ten depends on how
# many rows there are, and #47 is where it gets computed against whichever dataset
# a run actually used. Until then this list is the 1M answer and a lower bound
# everywhere else, which is the right way round: a query that is treated as
# determined when it is not produces a spurious disagreement, and one treated as
# undetermined when it is determined loses a check on ten rows.
#
# What stays checkable in all thirteen is the row count, the column set and the
# types. An engine that answered with five rows, or with the wrong columns, is
# still caught. That is weaker than the check the other thirty get and a great
# deal stronger than skipping them.
CLICKBENCH_UNDETERMINED = {
    "q11": "3 groups tie at the row the limit cuts on",
    "q17": "no ORDER BY at all, so any ten groups are a correct answer",
    "q18": "5 groups tie at the row the limit cuts on",
    "q22": "4 groups tie at the row the limit cuts on",
    "q24": "4 rows tie at the row the limit cuts on",
    "q25": "19 rows tie at the row the limit cuts on",
    "q26": "2 rows tie at the row the limit cuts on",
    "q30": "5 groups tie at the row the limit cuts on",
    "q31": "69,354 groups tie, since WatchID is nearly unique and almost every count is one",
    "q32": "every row ties, so the answer is any ten rows of the table",
    "q38": "651 groups tie at the row the limit cuts on",
    "q39": "177 groups tie at the row the limit cuts on",
    "q40": "10 groups tie at the row the limit cuts on",
}


def _cb(name: str, description: str, why: str, keys: tuple[str, ...] = ()) -> Query:
    """Builds one ClickBench entry.

    Args:
        name: The query name, q0 through q42, matching ClickBench's numbering.
        description: What it computes, in words.
        why: What it is here to expose.
        keys: The grouping key columns. Empty for a query that does not group.

    Returns:
        The query.
    """
    return Query(
        name,
        "clickbench",
        description,
        why,
        keys,
        ("hits",),
        suite="clickbench",
        undetermined=CLICKBENCH_UNDETERMINED.get(name, ""),
    )


# ClickBench's 43, in ClickBench's order and under ClickBench's names.
#
# The names start at zero because ClickBench's do. Its own page pads the checkbox
# labels so that `Q0..Q9` line up with the rest, every published result array is
# indexed from zero, and anybody reading our q22 next to a published q22 has to be
# looking at the same query. Starting at one would be tidier here and wrong
# everywhere it matters.
#
# The order is the published order and it is not a progression. db-benchmark walks
# from low cardinality to high and TPC-H walks through the specification, but this
# list was assembled from a production query log, so q0 is a row count and q1 is a
# filtered row count and then it stops being a sequence. Reading it as one leads to
# picking the wrong query to investigate.
#
# What the set actually contains, said once here rather than repeated in 43 `why`
# fields: seven scalar aggregates, two global distinct counts, a couple of dozen
# group bys almost all of which sort and take the top ten, five string queries,
# five that take their ten rows at an offset, and one that reads every column.
# Around half the suite is the same shape with a different key, which is the point:
# the keys are the variable and everything else is held still.
CLICKBENCH = (
    _cb(
        "q0",
        "count every row",
        "The floor, and not a trivial one on this suite. There is no column in it "
        "at all, so what it measures is whether an engine answers from Parquet "
        "metadata or reads something. In scan mode over twelve gigabytes the gap "
        "between those two is the whole query.",
    ),
    _cb(
        "q1",
        "count the rows where AdvEngineID is not zero",
        "The same count with one predicate on a narrow integer column. The gap "
        "against q0 is the cost of touching one column out of 105, which is the "
        "cleanest projection measurement in the suite.",
    ),
    _cb(
        "q2",
        "sum, count and average over two columns in one pass",
        "Three aggregates over two columns with no grouping and no filter. An "
        "engine that walks the table once per aggregate does three times the work "
        "for the same answer, and there is nothing else in the query to hide it.",
    ),
    _cb(
        "q3",
        "average UserID",
        "One average over a 64 bit column whose values are enormous. This is where "
        "an engine that accumulates into the input type rather than a wider one "
        "produces a different answer from everybody else, which the agreement "
        "check sees before the timing does.",
    ),
    _cb(
        "q4",
        "count the distinct values of UserID",
        "Exact distinct counting over a large integer domain, and the first of the "
        "two queries in the suite where an engine can be fast by being wrong. "
        "ClickBench permits an approximate answer here and several published "
        "systems give one. We do not, and the report says so rather than putting "
        "an approximate number next to an exact one.",
    ),
    _cb(
        "q5",
        "count the distinct values of SearchPhrase",
        "The text twin of q4. The gap between them is what distinct counting costs "
        "when the value is a variable length string rather than a machine word, "
        "and that gap is the reason both are in the suite instead of one.",
    ),
    _cb(
        "q6",
        "minimum and maximum EventDate",
        "Two reductions over a date column in one pass. Small, and worth having "
        "because it is the only place the date type is measured on its own, "
        "without a group by or a filter on top of it.",
    ),
    _cb(
        "q7",
        "count rows per AdvEngineID, ordered by the count",
        "The smallest group by in the suite. There are fewer than a dozen groups "
        "behind a filter that discards most of the table, so the result fits in a "
        "register file and this is the group by overhead floor rather than a "
        "measurement of grouping.",
        ("AdvEngineID",),
    ),
    _cb(
        "q8",
        "distinct users per RegionID, top ten",
        "Distinct counting per group rather than globally, which is a different "
        "problem: one set per group instead of one set. An engine that reaches for "
        "the same code path as q4 and runs it per group is doing something the "
        "shape of the data does not support.",
        ("RegionID",),
    ),
    _cb(
        "q9",
        "four aggregates including a distinct count per RegionID, top ten",
        "q8 with three ordinary aggregates alongside the distinct count. The gap "
        "between them says whether an engine can carry a distinct count in the "
        "same pass as a sum and an average, or whether it splits the query.",
        ("RegionID",),
    ),
    _cb(
        "q10",
        "distinct users per MobilePhoneModel, top ten",
        "The first group by on a text key, behind a filter that keeps the rows "
        "where the model is not the empty string. Empty string here means missing, "
        "and it is not a null, which is the trap this query sets for a port that "
        "reaches for a null check.",
        ("MobilePhoneModel",),
    ),
    _cb(
        "q11",
        "distinct users per phone and model pair, top ten",
        "q10 with a second key column in front of the first, and the two are "
        "strongly correlated because a model implies its make. A grouping "
        "implementation that treats the pair as two independent keys does more "
        "hashing than the data justifies and this is where that shows.",
        ("MobilePhone", "MobilePhoneModel"),
    ),
    _cb(
        "q12",
        "row count per SearchPhrase, top ten",
        "A group by on a high cardinality text key, counting rows. Pairs with q13, "
        "which is the same filter and the same key counting something else.",
        ("SearchPhrase",),
    ),
    _cb(
        "q13",
        "distinct users per SearchPhrase, top ten",
        "q12 with the count replaced by a distinct count. Same rows in, same "
        "groups, same sort, same ten out. The difference between the two numbers "
        "is the cost of distinct counting per group and of nothing else, which is "
        "why the pair is worth more than either half.",
        ("SearchPhrase",),
    ),
    _cb(
        "q14",
        "row count per engine and phrase pair, top ten",
        "q12 with an integer key added in front of the text one. A mixed width "
        "grouping key, which is the case a fixed width hash table has to widen for "
        "and a row layout has to pad.",
        ("SearchEngineID", "SearchPhrase"),
    ),
    _cb(
        "q15",
        "row count per UserID, top ten",
        "A group by on a 64 bit key with very high cardinality and no filter, so "
        "every row reaches the hash table. The distribution is real rather than "
        "generated: a few users account for a large share of the rows and most "
        "appear a handful of times, which is a probe length distribution nothing "
        "else in this repository produces.",
        ("UserID",),
    ),
    _cb(
        "q16",
        "row count per user and phrase pair, ordered by the count, top ten",
        "q15 with a text column added to the key, so the grouping key is now a 64 "
        "bit integer and a variable length string together and the hash table has "
        "to carry both. Pairs with q17, which is this query with the ORDER BY "
        "taken away and nothing else changed.",
        ("UserID", "SearchPhrase"),
    ),
    _cb(
        "q17",
        "row count per user and phrase pair, any ten",
        "q16 with no ORDER BY at all, which makes it the one query in the suite "
        "that catches an engine sorting because it always sorts. The answer is "
        "underdetermined by design, so this is also the query the agreement check "
        "has to be told about rather than the one it can check.",
        ("UserID", "SearchPhrase"),
    ),
    _cb(
        "q18",
        "row count per user, minute of EventTime and phrase, top ten",
        "A three column grouping key where one column is computed. The minute has "
        "to be extracted from a timestamp for every row before anything can be "
        "grouped, so this measures a scalar function feeding a hash table rather "
        "than either on its own.",
        ("UserID", "EventTime minute", "SearchPhrase"),
    ),
    _cb(
        "q19",
        "select UserID where UserID equals one specific value",
        "A point lookup with no aggregate and no index. Every engine here scans "
        "for it, so what this measures is how quickly a hundred million "
        "comparisons can be made and how little can be materialized on the way to "
        "the handful of rows that match.",
    ),
    _cb(
        "q20",
        "count the rows whose URL contains google",
        "A substring search over the widest text column in the table, run against "
        "every row. No grouping and no sorting, which makes it the clean read on "
        "matching throughput that q21 and q22 then build on.",
    ),
    _cb(
        "q21",
        "smallest URL and row count per SearchPhrase behind a substring filter, top ten",
        "The first aggregate in the suite that reduces text rather than numbers. A "
        "minimum over strings is a comparison per row that cannot be done in a "
        "register, and an engine without one has to sort or materialize to get it.",
        ("SearchPhrase",),
    ),
    _cb(
        "q22",
        "two text minima, a count and a distinct count per phrase, top ten",
        "The heaviest of the string queries. Three predicates including a negated "
        "substring match, two text minima and a distinct count, all per group. It "
        "is also the query where predicate order matters most among the string "
        "queries, since the phrase filter is far more selective than either "
        "substring match and costs far less to evaluate.",
        ("SearchPhrase",),
    ),
    _cb(
        "q23",
        "every column, filtered on a substring, sorted by time, top ten",
        "The only query in the suite that selects all 105 columns. It is here to "
        "catch an engine that materializes columns before it knows which rows "
        "survive, because the filter keeps a small fraction of the table and the "
        "limit keeps ten rows of that. Done well this touches two columns and then "
        "gathers ten rows of the other 103. Done badly it is the whole dataset.",
    ),
    _cb(
        "q24",
        "ten search phrases, earliest first",
        "A top ten by a sort key that is not the selected column. Small output, "
        "and the first of three that differ only in what they sort by.",
    ),
    _cb(
        "q25",
        "ten search phrases in alphabetical order",
        "q24 sorted by the text column instead of the timestamp. The gap between "
        "them is the cost of ordering by a variable length value rather than a "
        "fixed width one.",
    ),
    _cb(
        "q26",
        "ten search phrases by time then phrase",
        "q24 and q25 combined into a two column sort. Worth having as a separate "
        "query because a top n implementation that is fast on one key often "
        "degrades to a full sort on two.",
    ),
    _cb(
        "q27",
        "average URL length per CounterID, big groups only, top twenty five",
        "String length per row feeding an average per group, with a HAVING clause "
        "over the group output. Two traps in one query. The length is a byte count "
        "in the published SQL and a character count in pandas, and the URL column "
        "is not ASCII, so a port that uses the obvious call gets a different "
        "answer. And the HAVING is a filter over the small output rather than a "
        "reason to do anything to the input.",
        ("CounterID",),
    ),
    _cb(
        "q28",
        "average referer length per extracted host, big groups only, top twenty five",
        "The hardest query in the suite to port and the only one that needs a "
        "regular expression with a capture group. The grouping key is the host "
        "pulled out of a URL by a substitution applied to every row, so the "
        "expression runs a hundred million times before any grouping starts, and "
        "it carries the same byte against character length trap as q27.",
        ("extracted host",),
    ),
    _cb(
        "q29",
        "ninety sums over one column in one pass",
        "Ninety aggregates over the same column with a different constant added to "
        "each. Ninety intermediate arrays is the obvious answer and is what this "
        "query exists to catch: done properly the column is read once and ninety "
        "accumulators are updated per value, and the difference is not a "
        "percentage.",
    ),
    _cb(
        "q30",
        "count, sum and average per engine and client address, top ten",
        "A group by on a two column key where the second is an IP address, behind "
        "a filter. Addresses in real traffic have a heavy head and a very long "
        "tail, which is a load factor and probe length problem rather than a "
        "cardinality one, and it is not a distribution any generator here "
        "produces.",
        ("SearchEngineID", "ClientIP"),
    ),
    _cb(
        "q31",
        "count, sum and average per watch and client address, filtered, top ten",
        "q30 with WatchID in place of the engine, which is close to unique per "
        "row. Behind the phrase filter, so the group count is large but not the "
        "whole table. Pairs with q32, which drops the filter.",
        ("WatchID", "ClientIP"),
    ),
    _cb(
        "q32",
        "count, sum and average per watch and client address, unfiltered, top ten",
        "q31 with no filter, which makes the answer nearly as large as the input: "
        "a hundred million rows in and close to that many groups out. This is the "
        "query the hash table was written for and the one where the output build "
        "stops being an afterthought and becomes most of the work.",
        ("WatchID", "ClientIP"),
    ),
    _cb(
        "q33",
        "row count per URL, top ten",
        "A group by on the widest text column in the table with no filter at all, "
        "so every one of a hundred million URLs is hashed. The heaviest single "
        "text key in the suite.",
        ("URL",),
    ),
    _cb(
        "q34",
        "row count per constant and URL, top ten",
        "q33 with a literal added as a grouping key. Every row has the same value "
        "for it, so it adds nothing to the answer, and a planner that notices runs "
        "this at the price of q33. The pair is a one line test of whether "
        "expression simplification looks at group keys or only at filters.",
        ("1", "URL"),
    ),
    _cb(
        "q35",
        "row count per client address and three expressions over it, top ten",
        "A four column grouping key where three columns are arithmetic on the "
        "first. Perfectly correlated by construction, so the number of groups is "
        "exactly the number q30 would give on that column alone, and everything "
        "beyond the first column is work the answer does not need. A naive tuple "
        "hash pays for all four.",
        ("ClientIP", "ClientIP - 1", "ClientIP - 2", "ClientIP - 3"),
    ),
    _cb(
        "q36",
        "page views per URL over one month, top ten",
        "The first of the seven that filter hard before grouping. Five predicates, "
        "and they are wildly different in selectivity: the counter equality keeps a "
        "small fraction of the table and the refresh flag keeps most of it. "
        "Evaluating them in written order costs several passes over columns the "
        "first predicate already ruled out.",
        ("URL",),
    ),
    _cb(
        "q37",
        "page views per Title over one month, top ten",
        "q36 with Title as the key instead of URL. Same filter, same shape, a "
        "different text column with a different length distribution, so the pair "
        "separates the cost of the key from the cost of the filter.",
        ("Title",),
    ),
    _cb(
        "q38",
        "page views per URL over one month, ten rows starting at a thousand",
        "The first query in the suite with an OFFSET, which nothing else in this "
        "repository has. It is not a top ten with a different starting point: the "
        "engine has to produce a thousand and ten rows in order and then discard a "
        "thousand, so a top n structure sized for the limit is the wrong "
        "structure.",
        ("URL",),
    ),
    _cb(
        "q39",
        "page views per traffic source, referer and URL, ten rows starting at a thousand",
        "A five column grouping key where one column is a conditional expression "
        "over two others, so a branch per row feeds the hash table. It is also the "
        "only place in the suite where a text column and a literal empty string "
        "meet in the same expression, which forces a decision about what type that "
        "expression has.",
        ("TraficSourceID", "SearchEngineID", "AdvEngineID", "Src", "Dst"),
    ),
    _cb(
        "q40",
        "page views per URL hash and date for one referer, ten rows starting at a hundred",
        "Seven predicates including an equality against a single 64 bit hash out "
        "of a hundred million rows. If that predicate runs first the query touches "
        "almost nothing and if it runs last the query touches everything several "
        "times, which makes this the most extreme predicate ordering case in the "
        "suite.",
        ("URLHash", "EventDate"),
    ),
    _cb(
        "q41",
        "page views per window size for one URL, ten rows starting at ten thousand",
        "q40's shape with the largest offset in the suite. Ten thousand rows "
        "discarded to return ten, behind a filter that leaves very few rows to "
        "begin with, so an engine that gets the predicate order right may not have "
        "ten thousand rows to skip at all.",
        ("WindowClientWidth", "WindowClientHeight"),
    ),
    _cb(
        "q42",
        "page views per minute over two days, ten rows starting at a thousand",
        "The last query, and the only one that groups by a truncated timestamp. "
        "Truncating to the minute is a scalar function over a date type feeding a "
        "grouping key, the filter narrows to two days, and the sort is on the "
        "computed key rather than on the count, which is the one place in the "
        "suite where that is true.",
        ("EventTime truncated to the minute",),
    ),
)

# Every suite the harness knows how to run, and the queries in each. The names
# collide across suites on purpose, because renaming TPC-H's q1 would make the
# result file harder to check against a published one.
SUITES = {
    "db-benchmark": DB_BENCHMARK,
    "tpch": TPCH,
    "ingestion": INGESTION,
    "clickbench": CLICKBENCH,
}

# The groups a `--queries` argument may name, per suite.
#
# ClickBench gets one group with the suite's own name rather than four categories,
# because the queries do not partition. Almost every one of them filters, groups
# and sorts at the same time, so any taxonomy would either put most of the suite in
# one bucket or need a query to be in three of them. A reader who wants a subset
# names the queries.
GROUPS = {
    "db-benchmark": ("groupby", "join"),
    "tpch": ("tpch",),
    "ingestion": ("csv",),
    "clickbench": ("clickbench",),
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


def undetermined(suite: str, name: str) -> str:
    """Returns why a query's answer is not determined by its statement, if it is not.

    A lookup rather than a dictionary the callers reach into, because the check
    that has to honour this lives in three files and a suite that never has an
    undetermined query should not have to know the registry exists.

    Args:
        suite: The suite name.
        name: The query name.

    Returns:
        The reason, or empty for a query whose answer is determined and for a name
        this registry has never heard of.
    """
    if suite not in SUITES:
        return ""
    for query in SUITES[suite]:
        if query.name == name:
            return query.undetermined
    return ""


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
