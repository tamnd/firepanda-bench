#!/usr/bin/env python3
"""Builds the static page, with a history per query so a regression is a step in a line.

A table of today's numbers tells you where you are. It does not tell you that a
group by got forty percent slower three weeks ago, which is the thing a benchmark
is actually for. So the page carries one chart per query, over every result file
in the repository, and a regression shows up as a step rather than as a number
somebody has to remember.

Timings compare within a machine, a suite, a size and an io mode, and not across
any of them. A line that silently mixes two machines, or a scan run with a memory
run, is worse than no line. So the page draws one chart per combination and says
which one it is, rather than one chart with a footnote asking the reader to hold
four variables in their head.

Usage:
    pixi run site
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report as report_tool

ROOT = Path(__file__).resolve().parent.parent

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>firepanda benchmarks</title>
<style>
  body {{ font: 15px/1.6 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0 auto;
         max-width: 60rem; padding: 2rem 1rem; color: #1a1a1a; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 14px; }}
  th, td {{ border-bottom: 1px solid #e2e2e2; padding: 0.4rem 0.6rem; text-align: left; }}
  td:not(:first-child), th:not(:first-child):not(:nth-child(2)) {{ text-align: right; }}
  code {{ background: #f5f5f5; padding: 0.1rem 0.3rem; border-radius: 3px; }}
  .chart {{ margin: 2rem 0; }}
  h2 {{ margin-top: 2.5rem; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/vega@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6"></script>
</head>
<body>
{body}
<h2>History</h2>
<p>One line per engine, two charts per machine, suite, size and io mode: wall clock
and peak resident memory, because the claim is about both. Nothing is drawn across
two of those four, because nothing is comparable across them. A regression is a
step. Where a suite ran in both io modes there is a third chart over the pair, one
engine's scan time divided by its own memory time, which is how much of a scan run
went on opening the Parquet rather than on answering the query.</p>
<div id="history"></div>
<script>
const charts = {specs};
const host = document.getElementById('history');
charts.forEach((chart, index) => {{
  const heading = document.createElement('h3');
  heading.textContent = chart.title;
  host.appendChild(heading);
  const holder = document.createElement('div');
  holder.className = 'chart';
  holder.id = 'chart-' + index;
  host.appendChild(holder);
  vegaEmbed('#' + holder.id, chart.spec, {{actions: false}});
}});
</script>
</body>
</html>
"""


def markdown_to_html(text: str) -> str:
    """Renders the report's markdown well enough for a single page.

    A full markdown library would be another dependency for the four constructs
    this actually uses, which are headings, paragraphs, list items and pipe
    tables.

    Args:
        text: The markdown.

    Returns:
        The HTML body.
    """
    out: list[str] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
                continue
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if not stripped:
            continue
        # Longest prefix first. A `#### band` tested against `### ` matches, and the
        # band headings the ClickBench report is split into would come out as a
        # level three heading with a stray hash in front of them.
        if stripped.startswith("#### "):
            out.append(f"<h4>{stripped[5:]}</h4>")
        elif stripped.startswith("### "):
            out.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h1>{stripped[2:]}</h1>")
        elif stripped.startswith("- "):
            out.append(f"<p>&bull; {stripped[2:]}</p>")
        else:
            out.append(f"<p>{stripped}</p>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def history(paths: list[Path]) -> list[dict]:
    """Collects every median timing across every result file.

    Args:
        paths: The result files.

    Returns:
        One record per query, engine and file, carrying the host so the chart can
        refuse to mix machines.
    """
    rows = []
    for path in paths:
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if "results" not in document:
            continue
        # The date is the leading part of the file name, which is how the runner
        # writes it, and the file has no clock of its own.
        stamp = path.stem.split("-")
        date = "-".join(stamp[:3]) if len(stamp) >= 3 else path.stem
        machine = document.get("machine", {})
        host = machine.get("host") or machine.get("cpu_model") or "unknown machine"
        for key, entry in document["results"].items():
            if not entry.get("ok"):
                continue
            query, engine = key.split("/")
            rows.append(
                {
                    "date": date,
                    "suite": document["suite"],
                    "size": document["size"],
                    "io": document.get("io", "memory"),
                    "query": query,
                    "engine": engine,
                    "seconds": entry["median_s"],
                    # Zero rather than absent when a run has no memory sample, so
                    # the chart drops the point instead of the whole series.
                    "peak_mb": (entry.get("peak_rss_bytes") or 0) / (1 << 20),
                    "machine": host,
                }
            )
    return rows


def gaps(rows: list[dict]) -> list[dict]:
    """Pairs each query's scan timing with its memory timing from the same run.

    This is the one measurement in the repository that needs two result files to
    exist, which is why it is a chart of its own rather than a column in a table.
    In memory mode every engine is handed the same Arrow table and the read is not
    in the timed region. In scan mode each engine opens the Parquet itself, and
    Polars and DuckDB push the projection into the file so a query naming two of
    the 105 columns reads two of them. ClickBench is where that shows, because the
    hits table is the widest one in the repository by a factor of ten.

    The number is per engine and it is a ratio rather than a difference, so what it
    says is how much of an engine's scan mode time went on opening the file. Near
    one means the read cost nothing next to the computation. Far above one means
    the engine computes the answer so fast that reading is the whole query, which
    is where Polars ends up on the narrow ones and is a statement about how quick
    its group by is as much as about its reader. Two engines' ratios are therefore
    not comparable to each other the way two of the same engine's are, and neither
    is any conclusion about which reader is faster: that lives in the scan mode
    table in the report, where the seconds are.

    Below one happens and is not a bug. Memory mode holds the whole table resident
    for the length of the query and scan mode does not, and on a table this wide
    that is a gigabyte of pressure the scan run never pays.

    Args:
        rows: The history records, both io modes together.

    Returns:
        One record per query, engine, date and machine where both modes ran.
    """
    seen: dict[tuple, dict[str, float]] = {}
    for row in rows:
        if not row["seconds"]:
            continue
        key = (row["date"], row["machine"], row["suite"], row["size"], row["query"], row["engine"])
        seen.setdefault(key, {})[row["io"]] = row["seconds"]

    paired = []
    for (date, machine, suite, size, query, engine), modes in sorted(seen.items()):
        if "scan" not in modes or "memory" not in modes:
            continue
        paired.append(
            {
                "date": date,
                "machine": machine,
                "suite": suite,
                "size": size,
                "query": query,
                "engine": engine,
                "gap": modes["scan"] / modes["memory"],
            }
        )
    return paired


def chart_specs(rows: list[dict]) -> list[dict]:
    """Splits the history into one chart per comparable group.

    The grouping is machine, suite, size and io mode, which is exactly the set of
    things a timing is not comparable across. Drawing them on one chart and
    warning about it in the corner was the earlier arrangement, and a warning on
    stderr does not reach anyone reading the page.

    Args:
        rows: The history records.

    Returns:
        One `{title, spec}` per group, ordered by title.
    """
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["machine"], row["suite"], row["size"], row["io"])
        grouped.setdefault(key, []).append(row)

    charts = []
    for (machine, suite, size, io), group in sorted(grouped.items()):
        where = f"{suite} at {size}, io {io}, on {machine}"
        charts.append({"title": f"{where}: seconds", "spec": chart_spec(group, "seconds")})
        memory = [row for row in group if row.get("peak_mb")]
        if memory:
            charts.append({"title": f"{where}: peak MB", "spec": chart_spec(memory, "peak_mb")})

    # Grouped without the io mode, since this chart is the comparison between the
    # two modes and a group per mode would have nothing to compare.
    together: dict[tuple, list[dict]] = {}
    for row in gaps(rows):
        together.setdefault((row["machine"], row["suite"], row["size"]), []).append(row)
    for (machine, suite, size), group in sorted(together.items()):
        charts.append(
            {
                "title": f"{suite} at {size} on {machine}: scan time over memory "
                f"time, per engine, which is how much of a scan run was the read",
                "spec": chart_spec(group, "gap"),
            }
        )
    return charts


def chart_spec(rows: list[dict], field: str = "seconds") -> dict:
    """Builds the Vega-Lite specification for one history chart.

    Args:
        rows: The history records for a single comparable group.
        field: Which measurement to plot, `seconds` or `peak_mb`. Two charts rather
            than two axes on one, because a shared x axis with two scales is read
            wrong by about half of the people who look at it.

    Returns:
        The specification, with the data inline so the page needs no server.
    """
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": rows},
        "mark": {"type": "line", "point": True},
        "width": 260,
        "height": 160,
        "encoding": {
            "x": {"field": "date", "type": "ordinal", "title": None},
            "y": {"field": field, "type": "quantitative", "scale": {"type": "log"}},
            "color": {"field": "engine", "type": "nominal"},
            "facet": {"field": "query", "type": "nominal", "columns": 3},
        },
        "resolve": {"scale": {"y": "independent"}},
    }


def main(argv: list[str] | None = None) -> int:
    """Builds the page.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    parser.add_argument("--out", type=Path, default=ROOT / "site" / "index.html")
    args = parser.parse_args(argv)

    paths = [p for p in sorted(args.results.glob("*.json")) if p.name != "env.json"]
    if not paths:
        raise SystemExit(f"no result files under {args.results}")

    sections = ["# firepanda benchmarks", ""]
    for path in paths:
        sections.append(report_tool.render(report_tool.load_result(path), path))
    body = markdown_to_html("\n".join(sections))

    charts = chart_specs(history(paths))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(PAGE.format(body=body, specs=json.dumps(charts)))
    print(f"wrote {args.out} with {len(charts)} history charts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
