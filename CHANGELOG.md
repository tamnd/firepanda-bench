# Changelog

Versions here track the harness, not the engines it measures and not firepanda itself. A change that alters what a published number means gets a minor bump, because a reader comparing two result files needs to know whether the measurement changed under them.

## Unreleased

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
