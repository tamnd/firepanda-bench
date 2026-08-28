# Ingestion

CSV and Parquet read throughput, cold and warm cache.

Status: not wired up. `tools/queries.py` knows about db-benchmark and TPC-H only, and this suite waits on firepanda having a CSV reader and a Parquet reader to measure. What follows is the shape it will take.

## Why this suite matters more than its size suggests

`read_csv` is the first line of code almost every user writes. First impressions are made here, before anyone has run a single aggregation, and a library that is fast at group by and slow to load a file will be judged on the second thing.

## What is compared

pandas with the pyarrow engine, not the default engine, because comparing against a slower configuration of a competitor is the kind of thing that gets noticed, plus Polars and DuckDB.

This is also the one suite where the harness's default `memory` io mode makes no sense, because the reader is the thing under test. It runs in `scan` mode only.

## CSV

Schema inference on and off. Wide frames and narrow ones. Quoted fields, embedded newlines, and a file that is mostly nulls, because inference and null handling are where naive readers lose their throughput.

Cold and warm cache reported separately, always. A warm-cache CSV number is a measurement of the parser; a cold-cache one is a measurement of the parser and the disk together, and conflating them makes both meaningless.

## Parquet

Measured twice after M8, once through the Arrow C++ binding from M2 and once through the native Mojo reader.

That is the point of running it twice. The native reader is a substantial piece of work undertaken on the assumption that a bound reader leaves performance on the table, and if the two numbers come out the same then it was not worth writing. This table is how we find that out rather than assuming it.

Projection and predicate pushdown are exercised explicitly: reading two columns out of fifty, and reading a file where a predicate eliminates most row groups. The instrumented counters for columns decoded and row groups read are reported next to the timing, because pushdown that works should be visible as a count and not only as a duration.
