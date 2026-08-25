<h1>firepanda-bench</h1>

<p>
  <a href="https://github.com/tamnd/firepanda-bench/actions/workflows/bench.yml"><img alt="Benchmarks" src="https://github.com/tamnd/firepanda-bench/actions/workflows/bench.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Status" src="https://img.shields.io/badge/status-harness%20only-orange">
</p>

The performance comparison for [firepanda](https://github.com/tamnd/firepanda),
against pandas, Polars, DuckDB, cuDF and MojoFrame.

> **Status: harness only, no results yet.** firepanda is at specification stage, so
> there is nothing to benchmark. This repository exists now rather than later on
> purpose — see below.

## Why this exists before there is anything to measure

Benchmarks that run once, at the end, before an announcement, are marketing.
Benchmarks that run continuously, from the first milestone, are engineering. The
difference is that the second kind tells you which commit made things slow while you
still remember what you were doing.

So the harness stands up at M1, when it can only compare CSV reading and a handful of
aggregations. That is fine. By the time the optimization milestone lands there will be
a year of history to check the claims against, which is the entire point.

## Why it is a separate repository

Two reasons.

**Comparing against pandas, Polars, DuckDB and cuDF means installing them, and that
must never become a condition of building firepanda.** The library's environment stays
minimal; this repository carries the Python, the Docker images, the datasets and the
result history.

**Cadence.** The library changes when someone writes code. The benchmarks need to
re-run when pandas releases, when Polars releases, when Mojo releases, when a new
machine type appears, and on a schedule regardless. Tying those rhythms together makes
both worse.

The *correctness* comparison against pandas is not here. It lives in the main
repository, because Mojo imports pandas in the same process and there is no reason to
move it.

## The rule

**We publish the results we lose.**

A suite that only shows wins is not information, and anyone experienced reads it as an
advertisement and discounts everything in it. Where pandas, Polars, DuckDB or cuDF is
faster, the number goes in the table with a note about why, and if the reason is "not
optimized yet" it says that rather than being omitted.

This is not modesty. For a project whose entire pitch is performance, in a language
whose marketing has been criticized for exactly this, the credibility of the numbers
is the asset.

## What we are measured against

| | Why it is in the table |
|---|---|
| **pandas 3.0.5** | The API being replaced and the audience being addressed. 3.0 is faster than the pandas people remember, so this is not a free win. |
| **Polars 1.43** | The performance bar, and the reference implementation of this design. |
| **DuckDB** | Correctness oracle as well as competitor, and the thing that wins TPC-H. |
| **MojoFrame** | The academic prior art in the same language. A production library that cannot beat a research prototype has not justified itself. |
| **cuDF** | The GPU comparison, from M9. Mature, and the honest baseline for any GPU claim. |

MojoFrame is in that table as a discipline rather than a courtesy. It supports all 22
TPC-H queries and its authors published where it falls behind — high-cardinality
aggregation, which they diagnosed as Mojo's dictionary. firepanda claims to have fixed
that with its own hash table. The claim is only meaningful if we measure the query they
lost on, which is **TPC-H Q18**.

## The suites

| | |
|---|---|
| [`suites/db-benchmark`](suites/db-benchmark) | Ten group-by and five join queries at 0.5 GB, 5 GB and 50 GB. |
| [`suites/tpch`](suites/tpch) | All 22 queries at SF1 and SF10. Stresses the optimizer rather than the kernels. |
| [`suites/ingestion`](suites/ingestion) | CSV and Parquet read throughput, cold and warm cache. |
| [`suites/udf`](suites/udf) | Ours to define, because no standard suite measures the thing this project is about. |

Microbenchmarks are **not** here. They live in the main repository, because they run on
every pull request and must not require Python or any competing engine.

## Two methodology notes that decide whether the numbers mean anything

**Lazy engines must be forced to materialize.** ClickHouse and DuckDB use
`CREATE TABLE ans AS SELECT` for this reason and our harness does the same. This is not
theoretical for firepanda: after M4 the *eager* API is lazy underneath, so a benchmark
that calls `df.groupby(...).sum()` and never looks at the answer measures plan
construction and nothing else.

**The Mojo toolchain version is part of every result.** The Mojo ABI is not stable and
the codegen changes release to release, so a result that does not say which compiler
produced it is not reproducible. It is recorded in the results file alongside the
engine versions.

Beyond that: fixed instance types recorded in the results; the same machine for every
engine in a given run, always; ten runs with the median reported and the interquartile
range published, because a single number with no spread is not a measurement; cold and
warm cache reported separately for anything touching a file; Docker for the comparison
engines.

## Running it

```sh
curl -fsSL https://pixi.sh/install.sh | bash
pixi install

pixi run data --suite db-benchmark --size 0.5GB   # generate or fetch datasets
pixi run bench --suite db-benchmark --engines all
pixi run report results/
```

`pixi run repro <results-file>` re-runs exactly the configuration that produced a given
result file, including engine versions and dataset sizes. Anyone should be able to
check any number we publish.

## What not to do

**Do not benchmark against pandas 2.x.** It is what most people are running and it is
not what we are competing with. Using it would inflate every number in a way that is
indefensible the moment somebody notices.

**Do not benchmark the eager API without forcing materialization.** See above.

**Do not report a GPU number against a CPU baseline without also reporting cuDF.** The
interesting comparison for a GPU dataframe is another GPU dataframe.

**Do not quote Mojo's marketing numbers.** Whatever the language's headline figures
are, ours are the ones we measured on this library. Borrowing someone else's is how a
project loses the benefit of the doubt on all of its own numbers.

## License

Apache-2.0. See [LICENSE](LICENSE).

Third-party suites keep their own licensing: db-benchmark is
[duckdblabs/db-benchmark](https://github.com/duckdblabs/db-benchmark), the revival of
the h2oai suite. The frozen `h2oai.github.io` results page is from 2021 and must never
be used for comparison.
