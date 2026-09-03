# Ingestion

CSV read throughput, cold and warm cache, across four files that are hard in four different ways.

Status: the CSV half runs, with all four engines. The Parquet half waits on firepanda having a Parquet reader, and the shape it will take is at the bottom of this file.

```
python tools/data.py --suite ingestion --size 10M
python tools/run.py --suite ingestion --size 10M --engines all --runs 7
```

## Why this suite matters more than its size suggests

`read_csv` is the first line of code almost every user writes. First impressions are made here, before anyone has run a single aggregation, and a library that is fast at group by and slow to load a file will be judged on the second thing.

It is also the only suite where firepanda is handed the same file as everybody else. db-benchmark and TPC-H are Parquet, firepanda reads Parquet by handing the file to DuckDB, which is not something to time against DuckDB, and for db-benchmark it regenerates the data from the same seed instead. Here there is nothing to regenerate and nothing to take on trust: four engines open one file and the harness checks they got the same rows out of it.

## What is in the four files

`narrow` is four columns, an integer key, a second integer, a float and a short string. It is the ordinary case and the one most readers are tuned for.

`wide` is fifty columns of mixed type at a tenth of the rows, so the file stays about the same size and what changes is the per field cost rather than the per byte cost. A reader whose overhead is per column rather than per byte shows it here and nowhere else.

`quoted` has delimiters, line feeds and doubled quotes inside quoted text fields. This is the file that cannot be split naively on commas or on newlines, and it is where a reader that shortcuts either loses correctness rather than speed.

`nulls` is nine tenths empty fields across eight numeric columns. Every column is numeric on purpose: engines legitimately disagree about whether an empty text field is a null or an empty string, and a file that asked the question would report a difference of opinion as a wrong answer.

## How the comparison is kept fair

pandas gets the pyarrow engine, not the default one. It is multithreaded, most people do not know it is there, and comparing against the slower configuration of a competitor is the kind of thing that gets noticed. The exception is the quoted file, which the pyarrow engine cannot read at all: pyarrow's parser has `newlines_in_values` off by default and pandas does not expose it, so the read fails outright and the harness falls back to the default C engine and records that it did. The report prints that note next to the number.

The timed region is the read and nothing else. Every engine returns its own frame, not an Arrow table, because the conversion is a second and quite separate piece of work and it is not the same size for everyone: on the wide file, converting cost Polars about seventy times what the read did, and timing it would have published the fastest reader here as the slowest. The harness converts afterwards, outside the timing, to check the engines agree.

Agreement is checked on the row count, the null count of every column, the sum of every numeric column and the total byte length of every text column. Not a hash of every value, which is a Python loop over as many values as the file has and takes longer than every read in the suite put together. What the cheaper check catches is the class of mistake a CSV reader actually makes: losing a row, splitting a quoted field on the delimiter inside it, dropping the escape from a doubled quote, or reading an empty field as an empty string instead of a null.

The suite runs in `scan` mode only. A `memory` mode for a suite that measures the reader would mean reading the file before timing the read, and `run.py` refuses it rather than quietly reinterpreting it.

## Cold and warm

The first run of every measurement is taken after the file has been dropped from the page cache with `posix_fadvise`, which needs no privilege and no separate command. It is reported separately from the warm median, always, because a warm number measures the parser and a cold one measures the parser and the disk together, and conflating them makes both meaningless.

That the drop actually happened is checked rather than asserted. The block reads recorded against the cold sample come to the size of the file when it worked, and on a machine where the kernel refused, every measurement carries a note saying the cold column is warm.

## Parquet

Measured twice after M8, once through the Arrow C++ binding from M2 and once through the native Mojo reader.

That is the point of running it twice. The native reader is a substantial piece of work undertaken on the assumption that a bound reader leaves performance on the table, and if the two numbers come out the same then it was not worth writing. This table is how we find that out rather than assuming it.

Projection and predicate pushdown are exercised explicitly: reading two columns out of fifty, and reading a file where a predicate eliminates most row groups. The instrumented counters for columns decoded and row groups read are reported next to the timing, because pushdown that works should be visible as a count and not only as a duration.
