# Result history

Every scheduled run commits its result files here. They are the record, and they are
what `pixi run repro <file>` replays.

## Required keys

CI rejects a result file missing any of these:

| Key | Why |
|---|---|
| `suite` | Which suite, at which size or scale factor. |
| `engines` | Every engine with its exact version. A comparison against an unnamed version of anything is not a comparison. |
| `mojo_version` | The Mojo ABI is not stable and the codegen changes release to release. A result that does not say which compiler produced it is not reproducible. |
| `firepanda_ref` | The commit, so a regression is attributable. |
| `machine` | Instance type, physical core count, RAM. The same machine ran every engine in this file. |
| `runs` | How many. Ten, unless the file says otherwise and says why. |
| `results` | Per query: median, interquartile range, peak RSS, and whether the cache was cold or warm. |

## Reading these

A single number with no spread is not a measurement, which is why the interquartile
range is stored rather than only the median.

Timings across files are only comparable when `machine` matches. Different instance
types are not normalized and will not be; a normalized cross-machine comparison is a
model, not a measurement.

## Losses

They are in here. Where an engine beats firepanda the row carries a `note` saying why,
and where the reason is "not optimized yet" it says exactly that rather than being
omitted.

A run in which firepanda wins every single row is flagged by CI for a human to look at
before it is published. That check exists because a clean sweep is more often a broken
harness than a triumph.
