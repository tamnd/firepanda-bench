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


def test_a_machine_with_no_firepanda_checkout_can_still_be_described(monkeypatch):
    """CI is that machine, and so is any box running two Python engines.

    An engine reports a missing install by raising SystemExit, which reads fine
    from a command line and is not caught by `except Exception`. Probing on the
    common path made that reachable from `pixi run bench`, where before it only
    ever ran under `env-report`.
    """
    import engines.firepanda_engine as firepanda_engine

    def missing():
        raise SystemExit("cannot find a firepanda checkout")

    monkeypatch.delenv("FIREPANDA_REF", raising=False)
    monkeypatch.setattr(firepanda_engine, "firepanda_home", missing)

    described = run.describe_environment(None)

    assert described["firepanda_ref"] == ""
    assert described["engines"]["firepanda"] == ""


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


def test_a_result_that_ran_and_reports_no_peak_memory_is_rejected(tmp_path):
    """The shape of a file the firepanda driver used to write on a Mac.

    It read peak memory out of `/proc`, found no `/proc`, reported zero and said
    `ok`, so the subject engine's whole memory column was zeros that read as
    measurements. A process that ran has a non zero high water mark, so there is
    no query for which this is the truth.
    """
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            document(
                results={
                    "q1/firepanda": {
                        "median_s": 0.01,
                        "iqr_s": 0.0,
                        "peak_rss_bytes": 0,
                        "cache": "warm",
                        "ok": True,
                    }
                }
            )
        )
    )

    problems = validate_results.check(path)

    assert any("peak memory" in problem for problem in problems)


def test_a_pairing_that_did_not_run_owes_no_peak_memory(tmp_path):
    # A refusal is allowed to carry nothing but a reason, which is the whole
    # point of publishing refusals rather than dropping the row.
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            document(results={"q1/firepanda": {"ok": False, "note": "no window functions yet"}})
        )
    )

    assert validate_results.check(path) == []


def test_a_file_with_no_exact_check_in_it_is_still_a_result(tmp_path):
    # The exact check is off by default, so most files will never carry one.
    path = tmp_path / "r.json"
    path.write_text(json.dumps(document()))

    assert validate_results.check(path) == []


def test_an_exact_check_has_to_name_the_comparison_layer_it_used(tmp_path):
    # A verdict is a claim about two answers under one definition of sameness, and
    # a file that claims it verified something without saying which definition it
    # verified it against cannot be read a year later.
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            document(
                verification={
                    "check": "exact",
                    "ran": True,
                    "agreed": True,
                    "queries": {"db-benchmark/q1": {"agreed": True}},
                    "compat": {"path": "/x", "revision": ""},
                }
            )
        )
    )

    problems = validate_results.check(path)

    assert any("compat revision" in problem for problem in problems)


def test_an_exact_check_that_could_not_run_only_owes_a_reason(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            document(
                verification={
                    "check": "exact",
                    "ran": False,
                    "agreed": True,
                    "note": "no firepanda-compat checkout on this machine",
                }
            )
        )
    )

    assert validate_results.check(path) == []


def test_an_exact_check_that_could_not_run_and_will_not_say_why_is_rejected(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(document(verification={"check": "exact", "ran": False, "agreed": True}))
    )

    problems = validate_results.check(path)

    assert any("does not say why" in problem for problem in problems)


def test_a_complete_exact_check_validates(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            document(
                verification={
                    "check": "exact",
                    "ran": True,
                    "agreed": True,
                    "queries": {"db-benchmark/q1": {"agreed": True}},
                    "compat": {"path": "/x", "revision": "b79f16c"},
                }
            )
        )
    )

    assert validate_results.check(path) == []
