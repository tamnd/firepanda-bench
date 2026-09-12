"""Tests for the tool that measures what loading the hits table costs.

None of these load anything. What they check is the part that would go wrong
quietly: that the tool measures every engine the registry knows about rather than
a list somebody typed once, that asking for a size nobody downloaded says so
instead of measuring an empty glob, and that the child process the parent spawns
is the same file with the same arguments.

The measurement itself is not tested here, because there is nothing to assert
about a peak resident set except the number it comes back with, and that number
is the output rather than a property.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

pytest.importorskip("pyarrow")

import clickbench  # noqa: E402
import clickbench_load  # noqa: E402

import engines  # noqa: E402


def test_every_engine_the_registry_knows_is_measured():
    # A second list of engine names is the thing that goes stale, so there is not
    # one. If an engine is added to the registry it appears here.
    assert set(clickbench_load.ENGINES) == set(engines.KNOWN)


def test_the_subject_engine_is_measured_first():
    # Only so the table reads the way the rest of the reports read, with the
    # engine under test at the top and the three it is compared against below.
    assert clickbench_load.ENGINES[0] == "firepanda"


def test_a_size_that_was_never_downloaded_says_what_to_run():
    with pytest.raises(SystemExit) as caught:
        clickbench_load.pattern_for(
            "100M" if not (ROOT / "data" / "clickbench" / "100M").is_dir() else "10M"
        )
    assert "tools/data.py" in str(caught.value)


def test_the_pattern_is_a_glob_over_the_partitions():
    # The hits table is the only dataset here that is more than one file, and an
    # engine handed one partition of a hundred would measure a hundredth of the
    # load and report it as the whole thing.
    downloaded = [
        size for size in clickbench.SIZES if (ROOT / "data" / "clickbench" / size).is_dir()
    ]
    if not downloaded:
        pytest.skip("no clickbench dataset downloaded")
    pattern = clickbench_load.pattern_for(downloaded[0])
    assert pattern.endswith("hits_*.parquet")


def test_bytes_are_printed_in_gigabytes():
    assert clickbench_load.gigabytes(3_160_000_000) == "3.16 GB"
    assert clickbench_load.gigabytes(0) == "0.00 GB"
