# Result history

Result files land here when you run the harness and they are not committed. Every scheduled run uploads them as a workflow artifact, kept for ninety days, and the published site is drawn from them. Download the artifact for the run you care about, drop the files in this directory, and `pixi run report <file>` and `pixi run repro <file>` work on them exactly as they do on a local run.

Files are named `<date>-<host>-<suite>-<size>-<io>.json`. The io mode is in the name because a scan run and a memory run of the same suite at the same size are two different measurements, and the host is in the name because two machines running the same thing on the same day would otherwise produce the same file name and the second one copied here would silently replace the first. `env.json` is the machine description, written by `pixi run env-report`, and it is not a result file.

## Required keys

`pixi run validate-results` rejects a file missing any of these, and CI runs it:

| Key | Why |
|---|---|
| `suite` | Which suite. |
| `size` | At which size or scale factor. |
| `io` | Whether every engine was handed the same in-memory table, or each read the Parquet itself. These are not comparable. |
| `engines` | Every engine with its exact version. A comparison against an unnamed version of anything is not a comparison. |
| `mojo_version` | The Mojo ABI is not stable and the codegen changes release to release. A result that does not say which compiler produced it is not reproducible. |
| `firepanda_ref` | The commit, so a regression is attributable. |
| `machine` | Host name, CPU model, physical core count, RAM, governor, transparent huge pages, turbo. The same machine ran every engine in this file. |
| `runs` | How many runs the median was taken over. |
| `results` | Per query and engine: median, interquartile range, peak resident set, cache state, and a `note` on anything that did not run. |
| `agreement` | Whether the engines produced the same answers, per query, by fingerprint. |

A pairing that did not run has to carry a reason. "firepanda cannot read Parquet" is a result; a missing row is not.

## Reading these

A single number with no spread is not a measurement, which is why the interquartile range is stored rather than only the median.

Timings across files are only comparable when `machine` matches and `io` matches. Different machines are not normalized and will not be, because a normalized cross-machine comparison is a model rather than a measurement.

A query whose `agreement` entry says the engines disagreed has no comparable timing, whatever the numbers next to it say. Check that column before quoting a row.

## Losses

They are in here. Where an engine beats firepanda the row carries a `note` saying why, and where the reason is "not optimized yet" it says exactly that rather than being omitted.

`pixi run check-sweep` flags four things: an engine that raised, a speedup large enough to be a measurement error rather than a result, a run covering too little of the suite to quote, and a clean sweep. The last three go to a human, because each of them might be true. The first one fails CI, because an engine that raised is a hole in the table rather than a result, and `--crashes-only` is the form the pipeline runs.
