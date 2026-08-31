"""Tests that the firepanda driver is rebuilt when the library it links changes.

This is here because it was not, and the cost was a published number that was
wrong. firepanda 0.6.19 made the string factorize choose a worker count from a
cost model, and db-benchmark q3 and q7 were reported as unmoved by it, inside
their own run to run spread. They had in fact halved. The benchmark machine gets
the firepanda checkout as a tarball, tar restores the modification times from the
checkout it was made from, and the sources therefore landed looking older than the
binary the previous commit had built there. The harness compared those two
timestamps, kept the old binary, and measured the old library under the new
commit's name.

So the two properties are pinned: a library edit invalidates the binary even when
the edited file is backdated, and an untouched checkout does not trigger a
rebuild, because a fingerprint that always misses is a fifty second recompile in
front of every query.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from engines import firepanda_engine


def checkout(root: Path) -> Path:
    """Builds the smallest tree `firepanda_home` will accept.

    Args:
        root: The directory to build it in.

    Returns:
        The checkout path.
    """
    library = root / "firepanda"
    (library / "hash").mkdir(parents=True)
    (library / "__init__.mojo").write_text("")
    (library / "hash" / "factorize.mojo").write_text("comptime SPLIT_MARGIN = 1.25\n")
    return root


def test_a_backdated_library_edit_still_invalidates_the_binary(tmp_path):
    """The tarball case, with the modification time moved back a year."""
    home = checkout(tmp_path / "home")
    before = firepanda_engine.source_fingerprint(home)

    edited = home / "firepanda" / "hash" / "factorize.mojo"
    original = edited.stat()
    edited.write_text("comptime SPLIT_MARGIN = 2.0\n")
    os.utime(edited, (original.st_atime - 31_536_000, original.st_mtime - 31_536_000))

    assert firepanda_engine.source_fingerprint(home) != before


def test_an_untouched_checkout_keeps_its_fingerprint(tmp_path):
    """Otherwise every query pays for a compile it does not need."""
    home = checkout(tmp_path / "home")
    first = firepanda_engine.source_fingerprint(home)
    os.utime(home / "firepanda" / "hash" / "factorize.mojo", None)
    assert firepanda_engine.source_fingerprint(home) == first


def test_a_renamed_file_counts_as_a_change(tmp_path):
    """A module moved between packages compiles to different code."""
    home = checkout(tmp_path / "home")
    before = firepanda_engine.source_fingerprint(home)
    moved = home / "firepanda" / "hash" / "factorize.mojo"
    moved.rename(moved.with_name("group.mojo"))
    assert firepanda_engine.source_fingerprint(home) != before


def test_a_failed_build_leaves_no_stamp_to_trust(tmp_path, monkeypatch):
    """A compile error must not bless whatever binary is sitting there."""
    home = checkout(tmp_path / "home")
    root = tmp_path / "bench"
    (root / "engines" / "firepanda").mkdir(parents=True)
    binary = root / "engines" / "firepanda" / "firepanda-driver"
    monkeypatch.setattr(firepanda_engine, "ROOT", root)
    monkeypatch.setattr(firepanda_engine, "firepanda_home", lambda: home)

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "error: cannot parse"

    monkeypatch.setattr(firepanda_engine.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(SystemExit):
        firepanda_engine.build()
    assert not binary.with_suffix(".sources").exists()
