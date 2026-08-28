# Changelog

Versions here track the harness, not the engines it measures and not firepanda itself. A change that alters what a published number means gets a minor bump, because a reader comparing two result files needs to know whether the measurement changed under them.

## Unreleased

### The timed region stops paying for the previous run's teardown

Every timed run in the harness was shaped `answer = run()`, and the rebinding is what releases the answer the run before it produced. That release happens inside the timed region, so run two was charged for freeing run one's result, run three for run two's, and so on. On the ingestion suite, where an answer is a whole ten million row frame, that is gigabytes of free attributed to the wrong thing. The previous answer is dropped before the clock starts now, in `metrics.measure` for the three Python engines and in the firepanda driver's loop for the same reason.

It was charged to all four engines equally, so no published comparison was ever tilted by it, but it was a constant added to every number, and a constant added to everything compresses the differences the suite exists to show. It also added most of the run to run spread: the interquartile range on `csv_narrow` fell from 80 ms to 20 ms once it was gone.

Every ingestion number moves, and every engine's moves down. On an i9-13900K at ten million rows: pandas 0.111 to 0.063, Polars 0.049 to 0.036, DuckDB 0.197 to 0.142, firepanda 0.263 to 0.152. Reported peak RSS falls too, because the process is no longer holding two frames at once at the moment of measurement.

### The firepanda driver stops copying the file for `csv_narrow_typed`

firepanda gained `read_csv_as`, a read of a path with a declared schema, so the driver no longer has to open the file and read the bytes itself. That mattered more than it sounds: doing its own IO gave up firepanda's memory mapping, so the one query in this suite that skips inference was also the only one paying to copy the whole file first. The comment in the driver saying no such overload existed is gone with it.

This changes a published number, and in firepanda's favour, so it is worth being plain about what it is not. The other three engines were already reading the file the way their own users would. This is the firepanda driver catching up to that, not a new allowance.

## v0.3.0

A minor bump, and the reason is the rule at the top of this file. The harness measures something it did not measure before, and the numbers it publishes for the engines it already measured are unaffected, but a suite is a new claim about all four of them and it belongs in a version a reader can name.

### The ingestion suite runs

`read_csv` is the first line of code almost every user writes, and until now the harness had nothing to say about it. It has five queries over four generated files: a four column file with types inferred, the same file with types declared, a fifty column file at a tenth of the rows so the bytes stay level and what moves is the per field cost, a file whose quoted text fields carry delimiters and line feeds and doubled quotes, and a file that is nine tenths empty fields.

It is also the first suite where firepanda is handed the same file as everybody else. db-benchmark regenerates its data from the same seed because firepanda has no Parquet reader, and that regeneration is a claim the agreement check tests rather than a fact. Here there is nothing to regenerate. All four engines opened one file and agreed on all five queries, including the quoted one, which is a stronger statement about firepanda's CSV reader than any timing in the table.

The suite runs in `scan` mode only and `run.py` refuses `--io memory` for it with the reason, because reading the file before timing the read is not a mode, it is a mistake.

### The timed region is the read, and only the read

Every other suite has its engines return an Arrow table, which is how the harness compares answers. Doing that here would have timed the conversion as part of the read, and the conversion is neither small nor the same size for everyone: on the fifty column file it cost Polars about seventy times what the read did, because Polars stores text as views into a buffer and Arrow wants it offset encoded. The first run of this suite reported Polars as the slowest CSV reader in the table when it is the fastest. Each engine now returns its own frame and the harness converts afterwards, outside the timing.

### pandas cannot read the quoted file with the pyarrow engine

pandas gets the pyarrow engine everywhere in this harness, because it is multithreaded, most people do not know it is there, and benchmarking against the slower configuration of a competitor is the kind of thing that gets noticed. On a file with a line feed inside a quoted field it fails outright with a parse error: pyarrow's reader has `newlines_in_values` off by default and pandas does not expose it. The harness falls back to the default C engine, which reads the file correctly and takes about sixty times longer, and records the fallback in a note the report prints beside the number. A measurement taken under a different configuration is still a measurement, but it is not the same one as its neighbours in the row.

### Cold runs are actually cold

The first run of every ingestion measurement is taken after the file has been dropped from the page cache with `posix_fadvise`, which needs no privilege and no separate command, and it is reported separately from the warm median. That the drop took is checked rather than asserted: the block reads recorded against the cold sample come to the size of the file, and on a machine where the kernel refuses, every measurement in the run carries a note saying the cold column is warm.

### Reading a whole file needed a cheaper agreement check

The existing text digest is a 64 bit FNV over every value computed in Python, which is right for a group by answer with as many rows as there are groups and wrong for an answer with as many rows as the file. Ingestion answers are compared on the row count, the null count of every column including the numeric ones, the sum of every numeric column and the total byte length of every text column. That is weaker and the weakness is worth naming: it does not catch two values swapped between rows. What it does catch is the class of mistake a CSV reader actually makes, which is losing a row, splitting a quoted field on the delimiter inside it, dropping the escape from a doubled quote, or reading an empty field as an empty string instead of a null.

## v0.2.0

A minor bump, and the reason is the rule at the top of this file. Two changes here alter what a published number means. firepanda's peak resident memory on the group by queries was measured against a narrower table than the other three engines were given, and it now is not, so those numbers moved and a v0.1.0 memory figure is not comparable with a v0.2.0 one. And a result file now names the build that produced it, which every file before this one either failed to do or did wrongly.

### A result file names the build that produced it

The commit and the toolchain were read out of an `env.json` and out of nothing else, and `pixi run bench` does not write one, so every file produced outside CI carried an empty `firepanda_ref`. Six did. Four others carried a commit from a checkout that had been replaced two releases earlier, which is worse, because it attributes the numbers to the wrong build and says so with the same confidence as a correct one. The run probes the machine it is running on, and an `env.json` is now only consulted for the fields the probe could not fill.

`validate_results.py` rejects an empty `firepanda_ref` or `mojo_version` on any file where firepanda ran. It only asks when firepanda ran, because a pandas against Polars run on a machine with no Mojo installed owes neither field.

### firepanda runs thirteen of the fifteen db-benchmark queries

q1, q2, q3, q7 and q10 were reported as unsupported because they group by a string column and firepanda could not hold one in a `DataFrame`. It can now, so the driver implements them and the five empty cells are filled with measurements. What is left is q8, which needs a top-k per group, and q9, which needs a correlation. Both say so in the table.

The driver builds id1, id2 and id3 the way `tools/data.py` does, which is `"id"` and the draw with no one added, while the integer keys beside them are one based. That asymmetry is in the h2oai generator and reproducing it is the whole point: a text column that differs from the Parquet file by one would fail the agreement check for a reason nobody would guess at.

### The driver digests text columns

An answer with a string key was previously fingerprinted on its row count and its numeric sums alone, because the Mojo driver sent no text digests and the harness will not compare what one side did not send. The driver now computes the same summed 64 bit FNV-1a the harness computes, so all four engines are compared on the group keys as well as on the totals. At ten million rows, in both io modes, all four agree on all thirteen.

### firepanda holds the whole table now, and its memory numbers moved

The driver used to generate only the columns a query reads, so q4 was measured against a four column table while pandas, Polars and DuckDB were handed the whole nine column one. That flattered firepanda's peak resident memory on every group by query. It now builds all nine columns for every group by query, which is what the other three engines are given, and its memory numbers on q4, q5 and q6 are correspondingly higher than in v0.1.0. They are comparable now and they were not before.

### Latency, beyond the median

Every measurement carries `p90_s` and `p99_s` alongside the median and the interquartile range, and the report has two new tables: the ninety ninth percentile with its ratio to the median, and CPU seconds per run with how many cores that came to. An engine four times faster on sixteen cores and one four times faster on one core are not the same result and wall clock cannot tell them apart. At five runs a p99 is the slowest warm run, and `_percentile` says so rather than interpolating a value between two runs that were never observed.

### Engine versions

pandas 3.0.5, Polars 1.44.1, DuckDB 1.5.5, pyarrow 25.0.0. `pixi update` moves none of them: those are the newest builds in the channel as of this entry.

### Result files are no longer kept in the repository

Result files are no longer kept in the repository. The eight files that were committed for v0.1.0 have been removed from the history, `results/*.json` is ignored, and the benchmark workflow no longer pushes a results commit back to the branch. A run's files reach the site through the workflow artifact instead, and the artifact is the copy to download if you want to replay a published number with `pixi run repro`.

The publish job lost `contents: write` and its push token along with the commit step. It now only downloads the artifacts, builds the site and deploys it.

One thing did get worse and it is worth naming. The history chart on the site covered every run that had ever been committed, and it now covers what the current run produced. Restoring it means keeping the files somewhere that is not the source tree, an orphan branch or a release asset, and that is not done here.

## v0.1.0

The first version where both public suites run end to end with every engine present.

### Suites

db-benchmark and TPC-H both complete. Every query in both suites runs against pandas, Polars, DuckDB and firepanda, in two io modes: `memory` hands every engine the same Arrow table, and `scan` lets an engine that can push a projection into the Parquet file do it. Those two are separate measurements and the harness will not mix them.

TPC-H data and query text come from DuckDB's `tpch` extension, and every implementation is checked against `tpch_answers()` before any timing happens. `pixi run validate-tpch` passes 66 of 66 at SF1.

### Correctness

Answers are compared across engines by row count, per column sums and per text column digests. Numbers compare at a relative tolerance of 1e-7, which is loose enough for the float noise that cannot be avoided here and tight enough to catch a dropped group. Text columns carry a summed 64 bit FNV-1a and compare exactly, because a relative tolerance on a value near two to the sixty four is the same as not comparing at all.

`pixi run check-sweep --crashes-only` is a hard gate in CI. An engine that raised is a hole in the table rather than a slow result, and twice now a harness bug has been published as a column of skips that read like a missing feature.

### Result files

Named `<date>-<host>-<suite>-<size>-<io>.json`. The host is in the name because two machines running the same thing on the same day used to produce the same file name, and the second one copied into `results/` replaced the first with nothing noticing.

Every file names every engine version, the Mojo toolchain and the firepanda commit. `firepanda_ref` is read from the `FIREPANDA_REF` environment variable or a `GIT_REF` file, so it is populated on the benchmark machines, which receive the checkout as a tarball with `.git` excluded.

Eight files were produced for this version: db-benchmark at 0.5GB and TPC-H at SF1, each in both io modes, on an AMD EPYC VPS and on an i9-13900K. Zero disagreements and zero crashes in all eight. They were committed here originally and are not in the tree any more, for the reason under Unreleased.

### Site

One history chart per machine, suite, size and io mode. Nothing is drawn across two of those, because nothing is comparable across them.

### Fixed

DuckDB failed all five db-benchmark joins. The tables are called `left` and `right_small` and LEFT is a reserved word, so the parser error pointed at the alias rather than at the table.

DuckDB failed every query in both suites in `scan` mode. The Parquet view was created with a bound parameter, and DuckDB refuses to prepare a DDL statement carrying one.

TPC-H q20 failed on all three engines, on a guard that refused to fingerprint an answer with no numeric column. Q20 legitimately answers with two string columns.

TPC-H q3, q7, q15 and q19 were reported as disagreements on a run that had just matched the published answers. Agreement was hash equality and the hash rounds to nine significant figures before hashing, while pandas has to carry money in float64 because Arrow refuses the precision the exact arithmetic needs.

String key columns were absent from the fingerprint entirely, so two engines that grouped by different keys and landed on the same group count and the same totals were recorded as agreeing.

### CI

Every action is pinned to a commit hash. Every checkout sets `persist-credentials: false`, and the one job that pushes gets the token on the single command that needs it. Every dispatch input reaches a shell as an environment variable rather than as a template expansion. `zizmor --persona regular` is clean.
