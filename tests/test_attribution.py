"""Tests that a result file can say which build produced it.

Six files were written on two machines carrying an empty `firepanda_ref` and an
empty `mojo_version`, and four more carried a commit from a checkout that had been
replaced two releases earlier. Neither is visible in the report: the numbers
render the same either way, and the field is only ever read by whoever comes back
to a regression months later and wants to know what changed. So the two paths that
can produce that state are pinned here, one where the run does not probe the
machine it is running on and one where the validator lets the empty field through.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

pytest.importorskip("pyarrow")

import run
import validate_results


def test_environment_is_probed_when_no_file_is_given():
    """`pixi run bench` passes no env.json, and that is the common path."""
    described = run.describe_environment(None)
    assert "machine" in described
    assert "engines" in described


def test_a_stored_field_does_not_override_a_probed_one(tmp_path, monkeypatch):
    """The stale file is the dangerous case, so the probe has to win."""
    monkeypatch.setattr(
        run.env_report, "describe", lambda: {"firepanda_ref": "436e480", "machine": {}}
    )
    stored = tmp_path / "env.json"
    stored.write_text(json.dumps({"firepanda_ref": "39f7801", "mojo_version": "25.7"}))

    described = run.describe_environment(stored)

    assert described["firepanda_ref"] == "436e480"
    # A field the probe could not fill is still worth taking from the file.
    assert described["mojo_version"] == "25.7"


def document(**overrides):
    """Builds a result document that validates, so a test can break one field.

    Args:
        **overrides: Fields to replace.

    Returns:
        The document.
    """
    doc = {
        "suite": "db-benchmark",
        "size": "0.5GB",
        "io": "memory",
        "engines": {"pandas": "3.0.5", "firepanda": "0.6.7"},
        "mojo_version": "25.7.0",
        "firepanda_ref": "436e480",
        "machine": {"host": "gamingpc"},
        "runs": 5,
        "results": {},
        "agreement": {},
    }
    doc.update(overrides)
    return doc


@pytest.mark.parametrize("field", ["firepanda_ref", "mojo_version"])
def test_an_empty_attribution_field_is_rejected(tmp_path, field):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(document(**{field: ""})))

    problems = validate_results.check(path)

    assert any(field in problem for problem in problems)


def test_a_run_without_firepanda_owes_no_toolchain(tmp_path):
    """Two Python engines on a machine with no Mojo on it is a valid run."""
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            document(
                engines={"pandas": "3.0.5", "polars": "1.44.1"},
                mojo_version="",
                firepanda_ref="",
            )
        )
    )

    assert validate_results.check(path) == []
