# TPC-H

All 22 queries at scale factor 1 and scale factor 10.

## What this suite is for

Where db-benchmark stresses group by and join in isolation, TPC-H stresses the
**optimizer**. The wins here come from projection pushdown, predicate pushdown, join
ordering and partition pruning rather than from kernel speed.

This is the suite that catches optimizer regressions, and it is the one that will hurt
early: DuckDB and Polars have had years on exactly these queries.

## Two queries get called out by name

**Q18** is the MojoFrame comparison. MojoFrame supports all 22 queries and reports up
to 4.60x over dataframe libraries in other languages, and its authors published where
it falls behind — high-cardinality aggregation, diagnosed as Mojo's dictionary.
firepanda claims to have fixed that by writing its own hash table. Q18 is where that
claim is either true or it is not.

**Q13** is UDF-heavy, and it is where the language argument should show up as a number.
The entire premise of writing this library in Mojo is that the user's own function
compiles into the pipeline instead of being called through an interpreter. If that is
worth anything, Q13 is one of the places it becomes visible in a standard suite rather
than in one we designed ourselves.

## Data

`dbgen` from the TPC-H toolkit, converted to Parquet. SF1 is about 1 GB; SF10 is about
10 GB. Generated once and cached, not regenerated per run.

Parquet rather than CSV because these queries are about the optimizer, and Parquet is
what lets projection pushdown and row-group skipping show up at all.

## Reported

Per-query timing with the interquartile range over ten runs, peak RSS, and — for
firepanda — the number of columns decoded and row groups read, taken from the
instrumented counters rather than inferred from the timing.

Those counters are how a pushdown regression is distinguished from a machine having a
bad day.
