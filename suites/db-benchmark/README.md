# db-benchmark

Ten group-by queries and five join queries. The standard comparison for this class of library.

## Which version

[`duckdblabs/db-benchmark`](https://github.com/duckdblabs/db-benchmark), which is the DuckDB Labs revival and the version everyone now cites. h2oai stopped maintaining the original in 2021.

The frozen `h2oai.github.io` results page is from 2021 and must never be used for comparison. Every engine in it has had several years of work since, and quoting it would be quoting a competitor's worst version.

## Sizes

| Size | Rows | Where it runs |
|---|---|---|
| 0.5 GB | 10 million | CI, every scheduled run |
| 5 GB | 100 million | CI, every scheduled run |
| 50 GB | 1 billion | On demand, because it needs a machine most runners are not |

The 50 GB size is also the streaming milestone's exit criterion: it must complete within a memory budget smaller than the dataset.

## The methodology note that decides whether this means anything

ClickHouse and DuckDB use `CREATE TABLE ans AS SELECT` so that lazy engines are forced to materialize their results. This harness does the same.

This is not a theoretical concern for firepanda. After M4 the eager API is lazy underneath, so a benchmark that calls `df.groupby(...).sum()` and never looks at the answer measures plan construction and nothing else. Every query in this suite ends by consuming the result.

## Query groups

**Group by, ten queries.** Low cardinality through high cardinality, single and compound keys, sum, mean, median and count distinct, and the advanced queries that combine several aggregations over a large key space.

The high-cardinality queries are the ones to watch. They are where MojoFrame's authors diagnosed Mojo's `Dict` as their bottleneck, and they are the justification for firepanda writing its own open-addressing hash table. If we do not win them, that decision was wrong and the table should say so.

**Join, five queries.** Small, medium and large right-hand sides, plus the join on a key with skew.

## Which engines run it

All four engines run all 15. q8 was the last gap, and it closed when firepanda gained a per group top-n kernel.

## One thing about firepanda that has to be said out loud

firepanda's driver does not read the dataset. It regenerates it, from the same splitmix64 counter stream the generator used, before the timer starts. The reason is not that it cannot open a Parquet file, because it can, but that the way it opens one is to hand it to DuckDB and take the vectors back as Arrow, and DuckDB is one of the four engines in this table.

That is a claim, not a fact, and the cross-engine fingerprint is what makes it checkable. Every engine's answer is reduced to a row count, per-column sums and per-column text digests keyed by column name, and if firepanda's regenerated table differed from the Parquet one by a single row the fingerprints would part company and the report would name the query. This arrangement goes away the moment firepanda decodes Parquet itself.

## Reported alongside every timing

Peak resident set. A library that is 2x faster and uses 4x the memory has not won, and the table should be able to say that. Also user and system CPU, which is what tells a genuinely faster kernel apart from one that simply used more cores.

Resident set is measured on a compiled binary, not under `mojo run`. The JIT carries about 322 MB of baseline `VmHWM`, which is larger than most of these answers, so a memory number taken under it would be measuring the compiler.
