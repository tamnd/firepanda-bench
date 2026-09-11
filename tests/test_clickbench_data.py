#!/usr/bin/env python3
"""Tests for the hits download.

None of these go near the network. What they check is the part that decides
whether a wrong file is noticed: the size table, the disk check that runs before
the first byte, and the comparison between what is on disk and what the manifest
says. A truncated download that is never noticed is a wrong answer rather than an
error, and that comparison is the only thing standing between the two.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import clickbench


def test_the_sizes_are_partition_counts_from_the_front():
    assert clickbench.SIZES == {"1M": 1, "10M": 10, "100M": 100}


def test_only_the_full_size_claims_to_be_clickbench():
    # A number from a partial size is not comparable to a published one, and the
    # manifest is where the report reads that from.
    assert clickbench.SIZES["100M"] == 100
    assert clickbench.FULL_ROWS == 99_997_497


def test_the_partition_url_is_the_hits_compatible_form():
    # The ClickHouse native form is a different schema with different column
    # types, and reading it instead would be a different benchmark.
    assert clickbench.partition_url(0).endswith("athena_partitioned/hits_0.parquet")
    assert "hits_compatible" in clickbench.partition_url(0)
    assert clickbench.partition_url(99).endswith("hits_99.parquet")


def test_there_is_a_user_agent_and_it_says_who_we_are():
    # Not cosmetic. The bucket is behind Cloudflare and Cloudflare answers 403 to
    # urllib's default user agent, so dropping this header breaks every download
    # with an error that looks like the file is gone.
    assert "firepanda-bench" in clickbench.USER_AGENT
    assert "urllib" not in clickbench.USER_AGENT


def test_an_unknown_size_is_refused_by_name():
    with pytest.raises(SystemExit) as caught:
        clickbench.build("50M", Path("/tmp"), force=False)
    assert "50M" in str(caught.value)


def test_the_disk_check_refuses_before_anything_is_downloaded(tmp_path):
    with pytest.raises(SystemExit) as caught:
        clickbench.check_room(1 << 60, tmp_path)
    assert "free" in str(caught.value)


def test_the_disk_check_passes_when_there_is_room(tmp_path):
    clickbench.check_room(0, tmp_path)


def test_a_missing_file_fails_verification(tmp_path):
    recorded = {"hits_0": {"path": str(tmp_path / "hits_0.parquet"), "bytes": 10, "sha256": "x"}}
    assert not clickbench.verify({}, recorded)


def test_a_file_of_the_wrong_length_fails_verification(tmp_path):
    path = tmp_path / "hits_0.parquet"
    path.write_bytes(b"short")
    recorded = {"hits_0": {"path": str(path), "bytes": 10, "sha256": "x"}}
    assert not clickbench.verify({}, recorded)


def test_a_file_of_the_right_length_and_wrong_digest_fails_verification(tmp_path):
    path = tmp_path / "hits_0.parquet"
    path.write_bytes(b"0123456789")
    recorded = {"hits_0": {"path": str(path), "bytes": 10, "sha256": "not-the-digest"}}
    assert not clickbench.verify({"hits_0": clickbench.digest(path)}, recorded)


def test_a_file_that_matches_passes_verification(tmp_path):
    path = tmp_path / "hits_0.parquet"
    path.write_bytes(b"0123456789")
    digest = clickbench.digest(path)
    recorded = {"hits_0": {"path": str(path), "bytes": 10, "sha256": digest}}
    assert clickbench.verify({"hits_0": digest}, recorded)


def test_a_truncated_parquet_is_an_error_and_not_an_empty_table(tmp_path):
    # This is what half a download looks like to the next thing that opens it.
    path = tmp_path / "hits_0.parquet"
    path.write_bytes(b"PAR1" + b"\x00" * 128)
    with pytest.raises(SystemExit) as caught:
        clickbench.describe(path)
    assert "truncated" in str(caught.value)


def test_the_manifest_records_what_a_reader_needs(tmp_path):
    # The report reads the row count and whether this is the published size off
    # the manifest, so those two have to be there whatever else changes.
    manifest = {
        "suite": "clickbench",
        "size": "1M",
        "rows": 1_000_000,
        "is_published_size": False,
        "generator": "none, this dataset is downloaded and cannot be regenerated",
        "files": {},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    loaded = json.loads(path.read_text())
    assert loaded["is_published_size"] is False
    assert "cannot be regenerated" in loaded["generator"]
