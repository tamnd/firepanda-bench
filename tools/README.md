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
| `operations.py` | `pixi run operations` | List what each query is made of, in pandas names, and which of those operations the firepanda-compat cost matrix has a row for. |
| `verify.py` | `pixi run verify` | Compare the answer files a run wrote with `--verify exact`, row by row and value by value, through the firepanda-compat comparison layer. |

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

## The fourth thing the fingerprint cannot do

The three above are bugs that got fixed. This one is not a bug, it is what the fingerprint is, and no amount of fixing changes it.

Every part of the fingerprint reduces a column on its own. It knows the multiset of values in each column and it knows nothing whatsoever about which row each value sits on, so two answers that contain the same values paired up differently are identical to it. A join on the wrong key produces exactly that: the same group labels, the same totals, the wrong labels against the wrong totals. The fingerprint calls it agreement and always will, because a per column reduction cannot see a permutation across columns and that is the property that makes it cheap enough to leave on the timed path and computable by the Mojo driver without an Arrow sort.

So `verify.py` exists alongside it. Run a suite with `--verify exact` and every engine writes its answer to Arrow IPC after the last timed run, never inside one, and the check reads them back and hands each pair to `fpcompat.compare`, which sorts both sides by every column and then compares them row by row and value by value. That is the comparison layer the conformance oracle uses, imported out of a firepanda-compat checkout rather than copied into this repository, because a second copy of the rules about what makes two answers the same answer is a second definition of correctness and the two would not stay the same for long. The commit it came from goes into the verdict.

It is off by default and it should stay off by default. It costs the disk the answers take, which for the ingestion suite is the size of the dataset, and it costs a sort of every answer on both sides. Once per query per release is enough to catch a collision that would otherwise be invisible forever, and the release workflow runs it over the smallest size of all three suites.

Two places it is deliberately not exact, both closed lists with a paragraph each in `verify.py`. Float comparison uses the tolerance classes the compat layer defines, because every answer here is a sum or a mean over millions of rows and a parallel sum that reproduced a serial one bit for bit would not be a parallel sum. And db-benchmark Q9's `r2` column has a floor of 1e-12 below which a value is compared as zero, because a squared correlation computed from moments subtracts quantities near 1e16 and a true zero comes back as 0.0 from pandas and 1e-35 from Polars, which a relative tolerance calls completely different and should.

It has already found one thing nothing else here could. Polars rounds a decimal product back to the scale of its operands, so its TPC-H revenue columns differ from pandas and DuckDB at about five parts in a billion, which is a hundred times smaller than the fingerprint's 1e-7 and a thousand times smaller than the tolerance `validate_tpch.py` checks the published answers at. All three engines are right in the sense that matters, and the difference is now written down in the known difference registry in `verify.py` with the reason and the tolerance class it needs, so a larger difference arriving later is still a failure.

## The reason `check_sweep.py` exists

A published comparison in which firepanda wins every single row is more often a broken harness than a triumph: a forgotten `.collect()`, a warm cache on one side, a dataset that fell back to a smaller size. Flagging it costs nothing and catches the class of error that is most embarrassing to publish.
