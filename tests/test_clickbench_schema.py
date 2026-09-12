"""Tests that the hits table arrives in every engine at the types it should.

The eight row fixture the port tests run on carries 25 of the 105 columns and is
written by hand, so it says nothing about the eighty it leaves out and nothing
about what the real file declares. This checks the other direction: the file on
disk is read for what it says each column is, the three conversions ClickBench's
own loader makes are applied to that, and each engine is asked what it ended up
holding. Nothing here is a transcription of the schema, because a transcription
is a second copy and the copy is what goes stale.

It matters most for the 28 text columns. The file stores them as BYTE_ARRAY with
no logical type on them, nine of the 43 queries read one, and an engine that
leaves them as bytes does not fail: it matches nothing, returns an empty answer
and reports it as a result. `check_text` guards that at load time for the three
Python engines. This is the same guard for firepanda, which is a separate binary
and cannot call it, plus the eighty columns `check_text` has no opinion about.

Everything here skips without the dataset, since the file is 122 megabytes at the
smallest size and is not in the repository.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import clickbench  # noqa: E402

pytest.importorskip("pyarrow")

# The smallest downloaded size. Every partition of every size has the same schema,
# so the one column question this asks is answered as well by a million rows as by
# a hundred million, and the small one is the one a laptop has.
PARTITION = ROOT / "data" / "clickbench" / "1M" / "hits_0.parquet"

# What firepanda's `LogicalType` prints for each Arrow type the published schema
# uses. It spells the timestamp and the date pandas' way and Arrow's way
# respectively, which is `dtype` behaviour rather than anything about this file,
# so the two vocabularies are matched up here and not argued with.
FIREPANDA_NAMES = {
    "string": "string",
    "date32[day]": "date32[day]",
    "timestamp[us]": "datetime64[us]",
}


def partition() -> Path:
    """Returns the partition to read, or skips.

    Returns:
        The file.
    """
    if not PARTITION.exists():
        pytest.skip(f"no hits partition at {PARTITION}, run: pixi run data --suite clickbench")
    return PARTITION


def driver_schema(path: Path) -> dict:
    """Asks the firepanda driver what it holds after loading the table.

    Args:
        path: The partition.

    Returns:
        Column name to the type firepanda prints for it, in the order the frame
        has them.
    """
    from engines import firepanda_engine

    try:
        binary = firepanda_engine.build()
    except SystemExit as reason:
        pytest.skip(f"cannot build the firepanda driver: {reason}")
    finished = subprocess.run(
        [str(binary), "--schema=clickbench", f"--path={path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        pytest.fail(f"the driver exited {finished.returncode}: {finished.stderr.strip()}")
    answer = json.loads(finished.stdout.strip().splitlines()[-1])
    assert answer["ok"], answer
    return {column["name"]: column["type"] for column in answer["columns"]}


def test_the_published_schema_is_read_out_of_the_file():
    # The premise the rest of this file rests on. If the footer ever stops
    # carrying 105 columns then every comparison below is comparing against
    # something else and should say so here rather than passing quietly.
    wanted = clickbench.published_schema(partition())
    assert len(wanted) == 105
    assert wanted["WatchID"] == __import__("pyarrow").int64()


def test_every_text_column_the_file_stores_as_bytes_is_published_as_text():
    import pyarrow as pa
    import pyarrow.parquet as pq

    declared = {field.name: field.type for field in pq.read_schema(partition())}
    wanted = clickbench.published_schema(partition())
    binary = [name for name, type_ in declared.items() if pa.types.is_binary(type_)]
    assert len(binary) == 28
    for name in binary:
        assert wanted[name] == pa.string(), name
    # And the other direction, so a rule that converted everything would fail.
    assert wanted["WatchID"] == pa.int64()


def test_firepanda_loads_all_105_columns_at_the_published_types():
    wanted = clickbench.published_schema(partition())
    got = driver_schema(partition())

    assert list(got) == list(wanted), "the column order changed on the way in"
    wrong = {}
    for name, type_ in wanted.items():
        spelled = FIREPANDA_NAMES.get(str(type_), str(type_))
        if got[name] != spelled:
            wrong[name] = (spelled, got[name])
    assert not wrong, f"{len(wrong)} columns arrived at the wrong type: {wrong}"


def test_firepanda_holds_the_28_text_columns_as_text():
    # The same thing the assertion above covers, said on its own because this is
    # the one that turns into wrong answers rather than into an error. The other
    # three engines get this from `check_text` at load time.
    got = driver_schema(partition())
    wrong = [name for name in clickbench.TEXT_COLUMNS if got.get(name) != "string"]
    assert not wrong, f"{len(wrong)} text columns are not text: {wrong[:5]}"
