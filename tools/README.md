# Harness

Not implemented yet. firepanda is at specification stage, so there is nothing to
benchmark; this directory holds the shape the harness will take, and the pixi tasks in
`pixi.toml` name the entry points.

| File | Task | What it does |
|---|---|---|
| `data.py` | `pixi run data` | Generate or fetch datasets. db-benchmark data is generated; TPC-H uses `dbgen`. Cached, not regenerated per run. |
| `run.py` | `pixi run bench` | Run one suite against one or more engines, ten times, and write a result file. |
| `repro.py` | `pixi run repro` | Re-run exactly the configuration that produced a given result file, including engine versions. |
| `report.py` | `pixi run report` | Render result files as a markdown or terminal table. |
| `site.py` | `pixi run site` | Build the static site, with a time series per query so a regression is visible as a step in a graph. |
| `env_report.py` | `pixi run env-report` | Capture engine versions, Mojo toolchain version, instance type, core count and RAM into `env.json`. |
| `check_sweep.py` | `pixi run check-sweep` | Flag a result file in which firepanda wins every row, for a human to look at before publishing. |

## Two rules the harness has to enforce, not just document

**Force materialization.** Every query ends by consuming its result. A lazy engine
that is never asked for an answer has not done any work, and after M4 firepanda's
*eager* API is lazy underneath too — so this applies to the eager path as well as the
obvious one.

**Same machine, same run.** Every engine in a given result file ran on the same
machine in the same invocation. The harness refuses to merge result files from
different machines into one table.

## The reason `check_sweep.py` exists

A published comparison in which firepanda wins every single row is more often a broken
harness than a triumph — a forgotten `.collect()`, a warm cache on one side, a dataset
that fell back to a smaller size. Flagging it costs nothing and catches the class of
error that is most embarrassing to publish.
