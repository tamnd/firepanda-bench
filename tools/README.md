# Harness

Every file here is an entry point named by a task in `pixi.toml`, and every one of them runs today.

| File | Task | What it does |
|---|---|---|
| `data.py` | `pixi run data` | Generate or fetch datasets. db-benchmark data is generated from a splitmix64 counter stream; TPC-H comes from DuckDB's `tpch` extension `dbgen`. Cached under `data/`, not regenerated per run. |
| `run.py` | `pixi run bench` | Run one suite against one or more engines and write a result file. Spawns one worker process per engine and query. |
| `worker.py` | | Not called directly. Loads one engine, runs one query N times, prints one JSON sample set on stdout. |
| `validate_tpch.py` | `pixi run validate-tpch` | Compare every engine's TPC-H answer against the specification's published validation output, cell by cell. |
| `validate_results.py` | `pixi run validate-results` | Check a result file has every field the report and the site need, and that every failure carries a reason. |
| `repro.py` | `pixi run repro` | Re-run exactly the configuration that produced a given result file and report any median that drifted by more than a quarter. |
| `report.py` | `pixi run report` | Render result files as markdown: timings, peak resident set, a geometric mean scorecard, what did not run and why, and which queries the engines disagreed on. |
| `site.py` | `pixi run site` | Build the static site, with a time series per query so a regression is visible as a step in a graph. |
| `env_report.py` | `pixi run env-report` | Capture engine versions, the Mojo toolchain version, CPU model, core count, RAM, governor, transparent huge pages, turbo and swappiness into `results/env.json`. |
| `check_sweep.py` | `pixi run check-sweep` | Flag a result file with an implausible speedup, with too little coverage to quote, or in which firepanda wins every row. |

`engines/` holds one module per engine, each exposing `load(paths, suite, io)` and a `q<n>` function per query it implements. `engines/firepanda_engine.py` is the odd one out: it shells out to a compiled Mojo driver and parses its JSON, because the engine under test is not importable from Python.

## What the worker measures

One process per pairing, because peak resident set is a per-process high water mark and two engines in one interpreter cannot both be measured. Per run it records wall clock, resident set at the end, peak resident set, user CPU, system CPU, minor and major page faults, and block IO in and out. The Mojo driver reports the first three plus CPU from `/proc/self/stat`, and leaves faults and block IO at zero rather than guessing at them.

## Three rules the harness enforces, not just documents

**Force materialization.** Every query ends by consuming its result. A lazy engine that is never asked for an answer has not done any work, and after M4 firepanda's eager API is lazy underneath too, so this applies to the eager path as well as the obvious one.

**Same machine, same run.** Every engine in a given result file ran on the same machine in the same invocation. The harness refuses to merge result files from different machines into one table.

**Same answer, or say so.** Every engine's answer is reduced to a row count, a sum of every numeric column and an order independent FNV-1a digest of every text column, all keyed by column name, and the report names any query the engines did not agree on instead of ranking them anyway.

That fingerprint has been wrong three times, and each one is worth keeping written down because none of them looked like what it was.

It treated Arrow decimals as non-numeric and silently dropped every money column, which made TPC-H Q6 and Q19 hash identically and reported fourteen queries as disagreements when all three engines matched the published answers. The fix is a `pa.types.is_decimal` branch and a check that raises when a table has columns but nothing in them can be fingerprinted, so an empty fingerprint can never look like agreement.

It decided agreement by comparing the hash, and the hash rounds to nine significant figures before hashing. pandas carries TPC-H money in float64 and its totals land a few parts in a billion from DuckDB's, which straddles a rounding boundary often enough that four queries were reported as disagreements on a run where `validate-tpch` had just passed all sixty six implementations. Agreement is now decided on the numbers themselves with a relative tolerance of 1e-7, and the hash is only what a report prints.

It ignored text entirely, so a query answering with strings was fingerprinted on its row count alone. db-benchmark Q1 groups by a string key and sums one column, and two engines that grouped by different keys but landed on the same number of groups and the same total would have been recorded as agreeing. TPC-H Q20 answers with two strings and no numbers at all, and the guard above fired on it and failed the query on every engine identically, which is a shape that reads as a hard query rather than as a harness bug. Text columns now carry a summed 64 bit FNV-1a compared exactly, and the sum rather than a sorted hash is again so the Mojo driver can compute it.

## The reason `check_sweep.py` exists

A published comparison in which firepanda wins every single row is more often a broken harness than a triumph: a forgotten `.collect()`, a warm cache on one side, a dataset that fell back to a smaller size. Flagging it costs nothing and catches the class of error that is most embarrassing to publish.
