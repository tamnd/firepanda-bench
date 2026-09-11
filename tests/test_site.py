"""Tests for the history charts.

The page draws one chart per comparable group, and the thing worth pinning is what
counts as a group. A line that silently mixes two machines, or a scan run with a
memory run, is worse than no line, and it is worse in a way nobody notices because
the chart still looks like a chart.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

# Loaded by path rather than by name. `site` is a standard library module that the
# interpreter has already imported by the time any test runs, so `import site` gets
# that one and never ours.
_spec = importlib.util.spec_from_file_location("bench_site", TOOLS / "site.py")
site_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site_tool)


def _row(**overrides) -> dict:
    row = {
        "date": "2026-08-28",
        "suite": "db-benchmark",
        "size": "0.5GB",
        "io": "memory",
        "query": "q1",
        "engine": "pandas",
        "seconds": 2.0,
        "peak_mb": 200.0,
        "machine": "gamingpc",
    }
    row.update(overrides)
    return row


def test_each_group_is_two_charts():
    """Wall clock and peak memory, because the claim is about both."""
    charts = site_tool.chart_specs([_row()])
    assert [chart["title"].rsplit(": ", 1)[1] for chart in charts] == ["seconds", "peak MB"]


def test_the_memory_chart_plots_the_memory_field():
    charts = site_tool.chart_specs([_row()])
    fields = [chart["spec"]["encoding"]["y"]["field"] for chart in charts]
    assert fields == ["seconds", "peak_mb"]


def test_a_run_with_no_memory_sample_draws_no_memory_chart():
    """An older result file has timings and nothing else. One chart is the honest
    answer there, and an empty second chart is not."""
    charts = site_tool.chart_specs([_row(peak_mb=0)])
    assert len(charts) == 1
    assert charts[0]["title"].endswith("seconds")


def test_two_machines_are_two_groups():
    """Nothing is drawn across a machine boundary, because nothing is comparable
    across one."""
    charts = site_tool.chart_specs([_row(), _row(machine="vmi3391933")])
    assert len({chart["title"] for chart in charts}) == 4


def test_a_scan_run_is_not_drawn_with_a_memory_run():
    """Four timing charts, two per mode, and no line crossing between them. The
    fifth chart is the comparison between the modes, which is the only thing on the
    page that is allowed to have both in it."""
    charts = site_tool.chart_specs([_row(), _row(io="scan")])
    timings = [chart for chart in charts if "over memory" not in chart["title"]]
    assert len({chart["title"] for chart in timings}) == 4
    for chart in timings:
        modes = {row["io"] for row in chart["spec"]["data"]["values"]}
        assert len(modes) == 1


def test_both_io_modes_draw_the_gap_chart():
    """What reading the Parquet inside the timed region cost. It needs two result
    files to exist, which is why it is a chart and not a column in a table."""
    charts = site_tool.chart_specs([_row(seconds=2.0), _row(io="scan", seconds=5.0)])
    gap = [chart for chart in charts if "over memory" in chart["title"]]
    assert len(gap) == 1
    assert gap[0]["spec"]["data"]["values"][0]["gap"] == 2.5


def test_one_io_mode_draws_no_gap_chart():
    """A ratio needs both halves. Half of one is not a point on a chart."""
    charts = site_tool.chart_specs([_row()])
    assert not [chart for chart in charts if "over memory" in chart["title"]]


def test_the_gap_is_one_engine_against_itself():
    """Polars in scan mode against Polars in memory mode. Against pandas in memory
    mode it would be a speed comparison wearing a reader's name."""
    rows = [
        _row(engine="polars", seconds=1.0),
        _row(engine="polars", io="scan", seconds=4.0),
        _row(engine="pandas", seconds=10.0),
        _row(engine="pandas", io="scan", seconds=20.0),
    ]
    by_engine = {row["engine"]: row["gap"] for row in site_tool.gaps(rows)}
    assert by_engine == {"polars": 4.0, "pandas": 2.0}


def test_the_gap_does_not_cross_a_machine_or_a_size():
    """The same four things every other chart refuses to mix, minus the io mode,
    which is the one this chart exists to compare."""
    rows = [_row(seconds=1.0), _row(io="scan", seconds=4.0, machine="vmi3391933")]
    assert site_tool.gaps(rows) == []


def test_a_band_heading_is_a_heading_and_not_a_paragraph_of_hashes():
    """The ClickBench report splits its table into blocks under level four
    headings, and a renderer that only knows three levels prints the hash."""
    assert site_tool.markdown_to_html("#### groupby") == "<h4>groupby</h4>"
