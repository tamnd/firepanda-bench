# TPC-H

All 22 queries at scale factor 1 and scale factor 10.

## What this suite is for

Where db-benchmark stresses group by and join in isolation, TPC-H stresses the optimizer. The wins here come from projection pushdown, predicate pushdown, join ordering and partition pruning rather than from kernel speed.

This is the suite that catches optimizer regressions, and it is the one that will hurt early, because DuckDB and Polars have had years on exactly these queries.

## Where the queries come from

The SQL is not ours. It is read out of DuckDB's `tpch` extension with `tpch_queries()`, which is the specification's own text, and DuckDB runs it unmodified. The pandas and Polars versions are ports, written once against the same text, and a port is where a benchmark goes wrong: a subquery turned into a join that drops rows, a filter applied after an aggregate instead of before it, a left join written as an inner one. Each of those produces a plausible table quickly, and a fast wrong answer is worse than no answer.

So every port is checked against `tpch_answers()`, the validation output the TPC publishes with the specification, cell by cell to a relative tolerance of a part in a million, including column names. All 66 implementations, three engines times twenty two queries, reproduce it at SF1. `pixi run validate-tpch` is what CI runs and a query that does not reproduce the published answer does not go in the results table.

Two things that check turned up, kept here because they will come back:

**Q18's column has no alias.** The select list says `sum(l_quantity)` and the published answer header abbreviates it to `sum`, so DuckDB running the specification's own text disagrees with the specification's own answer file. The select list is the better authority, both ports were renamed to match it, and the header check accepts a produced name that begins with the published one.

**Polars gives a decimal quotient the numerator's scale.** Q8's `mkt_share` came back as `0.03` rather than `0.0344358904066548`. Both operands are cast to Float64 in that one query.

## Which engines run it

pandas, Polars and DuckDB run all 22. firepanda runs none of them, and the report says so on every row rather than leaving the column out. Three things are missing: the twenty two queries in its driver, an ordering comparison on strings, and a way to load the dbgen output. The third is the awkward one. firepanda can open a Parquet file, but it does so by handing it to DuckDB, and this suite's data cannot be regenerated from a seed the way db-benchmark's can, so there is no load path here that does not run through an engine in the table.

pandas gets one concession that is worth stating plainly. Arrow-backed pandas cannot do the decimal arithmetic Q1 needs, because the intermediate wants precision 61 and Arrow's limit is 38, and there is no way to ask for a narrower one. Decimal columns are cast to double at load. Double arithmetic is faster than exact decimal arithmetic, so the bias runs in pandas' favour.

## Two queries get called out by name

**Q18** is the MojoFrame comparison. MojoFrame supports all 22 queries and reports up to 4.60x over dataframe libraries in other languages, and its authors published where it falls behind, which is high-cardinality aggregation, diagnosed as Mojo's dictionary. firepanda claims to have fixed that by writing its own hash table. Q18 is where that claim is either true or it is not.

**Q13** is UDF-heavy, and it is where the language argument should show up as a number. The entire premise of writing this library in Mojo is that the user's own function compiles into the pipeline instead of being called through an interpreter. If that is worth anything, Q13 is one of the places it becomes visible in a standard suite rather than in one we designed ourselves.

## Data

`dbgen` from DuckDB's `tpch` extension, written out as Parquet. SF1 is 8 tables, 392 MB uncompressed, with 6,001,215 line items, and takes about eleven seconds to generate. SF10 is ten times that. Generated once and cached under `data/tpch/<size>/`, not regenerated per run.

Parquet rather than CSV because these queries are about the optimizer, and Parquet is what lets projection pushdown and row-group skipping show up at all. That only applies in `--io scan`; the default `memory` mode hands every engine the same Arrow table so nobody is timed on their Parquet reader.

## Reported

Per-query timing with the full spread over ten runs, peak resident set, user and system CPU, and page faults. For firepanda, once it can run this suite, the number of columns decoded and row groups read as well, taken from the instrumented counters rather than inferred from the timing. Those counters are how a pushdown regression is distinguished from a machine having a bad day.

## One thing still to check

Q11 filters on a fraction of total value, and the specification scales that fraction by the scale factor. DuckDB's text does whatever it does; the two dataframe ports hardcode 0.0001. Both reproduce the SF1 answer, so it does not matter at SF1, and it needs checking before SF10 is published.

## Trademark

TPC-H is a trademark of the Transaction Processing Performance Council. These are unaudited runs of the specification's queries and not a published TPC-H benchmark result.
