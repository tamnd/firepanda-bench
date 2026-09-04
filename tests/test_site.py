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
    charts = site_tool.chart_specs([_row(), _row(io="scan")])
    assert len({chart["title"] for chart in charts}) == 4
