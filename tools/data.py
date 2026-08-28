#!/usr/bin/env python3
"""Generates the datasets, deterministically, so every engine gets the same bytes.

The db-benchmark group by dataset is the h2oai G1 design: three string key columns
and three integer ones, spanning cardinalities from a hundred distinct values to
one per hundred rows, plus two small integer measures and one float. The queries
are chosen so that between them they hit every cardinality, and the interesting
ones are the high cardinality ones, because that is where a hash table either
works or does not.

The generator is splitmix64 rather than numpy's own, and that is a deliberate
choice with a specific payoff. firepanda cannot read a Parquet file yet, so the
only way to benchmark it against pandas on the same data today is for it to
generate the same data. splitmix64 over a counter is four lines in any language,
it is what `firepanda/testing/rng.mojo` already implements, and a Mojo engine
seeded the same way produces the same column byte for byte. The harness verifies
that by comparing answers rather than trusting it.

The ingestion suite is the exception, and it is the reason that suite exists. Its
data is written as CSV and every engine including firepanda reads the same file,
so nothing there depends on two generators agreeing. The four files it generates
are deliberately different from each other rather than four sizes of the same
thing: a narrow one that is what most files look like, a wide one that trades
bytes for fields, a quoted one that no reader can shortcut, and one that is nine
tenths empty.

Usage:
    python tools/data.py --suite db-benchmark --size 0.5GB [--formats parquet,csv]
    python tools/data.py --suite ingestion --size 10M
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"

# The db-benchmark sizes, by their published names. The byte figures are what the
# CSV comes to, which is where the names come from and is not what the Parquet or
# the in-memory form costs.
SIZES = {
    # Not a published size. One million rows is what the M1 exit criterion asks
    # for alongside ten million, and it is small enough that CI can check every
    # engine agrees on every query without spending forty minutes doing it. Any
    # timing taken here is a smoke test rather than a result: at this size the
    # whole dataset fits in cache on some of these machines and the ranking says
    # more about startup cost than about the engines.
    "0.05GB": 1_000_000,
    "0.5GB": 10_000_000,
    "5GB": 100_000_000,
    "50GB": 1_000_000_000,
}

# The ingestion sizes, named by the row count of the narrow file rather than by
# bytes, because the four files in this suite are different shapes on purpose and
# a single byte figure would describe none of them.
INGESTION_SIZES = {"1M": 1_000_000, "10M": 10_000_000, "100M": 100_000_000}

# The size to build when none is named, per suite.
DEFAULT_SIZE = {"db-benchmark": "0.5GB", "tpch": "sf1", "ingestion": "10M"}

# How many columns the wide ingestion file has.
WIDE_COLUMNS = 50

# The wide file gets a tenth of the rows the others do. Fifty columns at the same
# row count is a ten times larger file, and what the wide case is here to measure
# is the cost of a field rather than the cost of a byte, so holding the file size
# roughly level and moving the fields is the comparison that isolates it.
WIDE_DIVISOR = 10

# How many columns the mostly-null file has beside its dense key.
NULL_COLUMNS = 8

# One field in ten survives in the mostly-null file. Nine tenths empty is past the
# point where a reader that branches per value can hide it.
NULL_KEEP = 10

# The h2oai G1 cardinality factor. id1 and id2 take K distinct values, id3 takes
# N/K, and that ratio is the whole point of the design.
K = 100

GOLDEN = np.uint64(0x9E3779B97F4A7C15)
MIX_A = np.uint64(0xBF58476D1CE4E5B9)
MIX_B = np.uint64(0x94D049BB133111EB)


def splitmix64(seed: int, count: int, skip: int = 0) -> np.ndarray:
    """Returns `count` words of the splitmix64 stream for a seed.

    This is the counter form of the generator, which produces exactly the same
    sequence as calling `next_u64` repeatedly but computes any slice of it without
    computing the slices before it. That is what makes it vectorizable here and
    reproducible in Mojo.

    Args:
        seed: The generator seed.
        count: How many words to produce.
        skip: How many words of the stream to pass over first.

    Returns:
        An array of unsigned 64-bit words.
    """
    with np.errstate(over="ignore"):
        steps = np.arange(skip + 1, skip + count + 1, dtype=np.uint64)
        z = np.uint64(seed) + steps * GOLDEN
        z = (z ^ (z >> np.uint64(30))) * MIX_A
        z = (z ^ (z >> np.uint64(27))) * MIX_B
        return z ^ (z >> np.uint64(31))


def _below(words: np.ndarray, bound: int) -> np.ndarray:
    """Reduces random words to `[0, bound)`.

    A remainder, matching `Rng.next_below`, bias and all. The bias is on the order
    of two to the minus forty for these bounds and does not move any query.

    Args:
        words: The random words.
        bound: The exclusive upper bound.

    Returns:
        Values in the half-open range, as int64.
    """
    return (words % np.uint64(bound)).astype(np.int64)


def generate_groupby(rows: int, seed: int = 0x243F6A8885A308D3) -> pa.Table:
    """Builds the db-benchmark group by table.

    Args:
        rows: How many rows.
        seed: The generator seed, recorded in the manifest.

    Returns:
        The table, with the h2oai G1 schema.
    """
    high = max(rows // K, 1)

    # One stream per column, each offset by a whole column's worth, so adding a
    # column later does not change the values in the columns before it.
    def stream(index: int) -> np.ndarray:
        return splitmix64(seed, rows, skip=index * rows)

    id4 = _below(stream(3), K) + 1
    id5 = _below(stream(4), K) + 1
    id6 = _below(stream(5), high) + 1

    # The string keys are the same draws as id4, id5 and id6 rendered as text, so
    # a string keyed query and its integer twin group the same rows. That makes
    # the pair a measurement of the string machinery and nothing else.
    id1 = pa.array(np.char.add("id", _below(stream(0), K).astype(str)))
    id2 = pa.array(np.char.add("id", _below(stream(1), K).astype(str)))
    id3 = pa.array(np.char.add("id", _below(stream(2), high).astype(str)))

    v1 = _below(stream(6), 5) + 1
    v2 = _below(stream(7), 15) + 1
    # Six decimal places, matching the R generator, so a sum of floats is a sum of
    # numbers that actually round trip through text.
    v3 = np.round((stream(8) >> np.uint64(11)).astype(np.float64) / float(1 << 53) * 100.0, 6)

    return pa.table(
        {
            "id1": id1,
            "id2": id2,
            "id3": id3,
            "id4": pa.array(id4, type=pa.int32()),
            "id5": pa.array(id5, type=pa.int32()),
            "id6": pa.array(id6, type=pa.int32()),
            "v1": pa.array(v1, type=pa.int32()),
            "v2": pa.array(v2, type=pa.int32()),
            "v3": pa.array(v3, type=pa.float64()),
        }
    )


def generate_join_tables(rows: int, seed: int = 0x243F6A8885A308D3) -> dict[str, pa.Table]:
    """Builds the db-benchmark join tables.

    The design is one large left table and three right tables at a thousandth, a
    hundredth and the full size of it, because what a join costs depends far more
    on the shape of the right side than on the left.

    Args:
        rows: How many rows in the left table.
        seed: The generator seed.

    Returns:
        A mapping from table name to table.
    """
    small = max(rows // 1_000, 1)
    medium = max(rows // 100, 1)

    def stream(index: int) -> np.ndarray:
        return splitmix64(seed ^ 0x5DEECE66D, rows, skip=index * rows)

    left = pa.table(
        {
            "id1": pa.array(_below(stream(0), small) + 1, type=pa.int32()),
            "id2": pa.array(_below(stream(1), medium) + 1, type=pa.int32()),
            "id3": pa.array(_below(stream(2), rows) + 1, type=pa.int32()),
            "v1": pa.array(
                (stream(3) >> np.uint64(11)).astype(np.float64) / float(1 << 53) * 100.0,
                type=pa.float64(),
            ),
        }
    )

    def right(index: int, count: int, key: str) -> pa.Table:
        # An explicit stream index rather than a hash of the name. Python's string
        # hash is salted per process, so a name derived offset would generate a
        # different dataset on every run and nothing would be reproducible.
        words = splitmix64(seed ^ 0x9E3779B9, count, skip=index * count)
        return pa.table(
            {
                key: pa.array(np.arange(1, count + 1, dtype=np.int32)),
                "v2": pa.array(
                    (words >> np.uint64(11)).astype(np.float64) / float(1 << 53) * 100.0,
                    type=pa.float64(),
                ),
            }
        )

    return {
        "left": left,
        "right_small": right(0, small, "id1"),
        "right_medium": right(1, medium, "id2"),
        "right_big": right(2, rows, "id3"),
    }


def _digits(count: int) -> np.ndarray:
    """Returns `0` through `count - 1` as text, without a Python loop.

    Args:
        count: How many.

    Returns:
        An array of strings.
    """
    return np.arange(count).astype(str)


def generate_ingestion(rows: int, seed: int = 0x243F6A8885A308D3) -> dict[str, pa.Table]:
    """Builds the four ingestion tables.

    Args:
        rows: How many rows in the narrow, quoted and mostly-null files. The wide
            file gets a tenth of that, for the reason `WIDE_DIVISOR` gives.
        seed: The generator seed, recorded in the manifest.

    Returns:
        A mapping from table name to table.
    """
    tables = {}

    def stream(salt: int, count: int, index: int) -> np.ndarray:
        return splitmix64(seed ^ salt, count, skip=index * count)

    # Narrow: two integers, a float and a short string. This is the shape of the
    # file in the tutorial, and the one a first impression is made on.
    index = np.arange(rows, dtype=np.int64)
    tables["narrow"] = pa.table(
        {
            "id": pa.array(index, type=pa.int64()),
            "pair": pa.array(_below(stream(0x11, rows, 0), 1_000_000), type=pa.int64()),
            # Six decimal places, so the value that is written is a value that
            # round trips through text rather than one the writer had to round.
            "score": pa.array(
                np.round(
                    (stream(0x11, rows, 1) >> np.uint64(11)).astype(np.float64)
                    / float(1 << 53)
                    * 100.0,
                    6,
                ),
                type=pa.float64(),
            ),
            "label": pa.array(np.char.add("row", _digits(rows))),
        }
    )

    # Wide: fifty columns of three types in a repeating pattern, so a reader that
    # dispatches per field rather than per column pays fifty times here.
    wide_rows = max(rows // WIDE_DIVISOR, 1)
    wide = {}
    for column in range(WIDE_COLUMNS):
        words = stream(0x22, wide_rows, column)
        name = f"c{column:02d}"
        if column % 5 < 2:
            wide[name] = pa.array(_below(words, 1_000_000), type=pa.int64())
        elif column % 5 < 4:
            wide[name] = pa.array(
                np.round((words >> np.uint64(11)).astype(np.float64) / float(1 << 53) * 100.0, 6),
                type=pa.float64(),
            )
        else:
            wide[name] = pa.array(np.char.add("s", _below(words, 1_000).astype(str)))
    tables["wide"] = pa.table(wide)

    # Quoted: every note carries a delimiter, an embedded line feed and a pair of
    # quotes, so the writer has to quote every one of them and no reader can find
    # a row boundary by looking for a newline.
    text = _digits(rows)
    note = np.char.add("line one,", text)
    note = np.char.add(note, '\nline two "')
    note = np.char.add(note, text)
    note = np.char.add(note, '"')
    tables["quoted"] = pa.table(
        {
            "id": pa.array(index, type=pa.int64()),
            "note": pa.array(note),
            "label": pa.array(np.char.add("row", text)),
        }
    )

    # Mostly null: a dense key and eight columns that are nine tenths empty.
    #
    # Every one of the eight is numeric, and that is not an oversight. Engines
    # disagree about whether an empty text field is a null or an empty string,
    # pandas and Polars answer differently by default, and both answers are
    # defensible. A file that asked the question would report a disagreement
    # about semantics as a disagreement about the answer.
    sparse = {"id": pa.array(index, type=pa.int64())}
    for column in range(NULL_COLUMNS):
        words = stream(0x33, rows, column)
        keep = (words % np.uint64(NULL_KEEP)) == np.uint64(0)
        if column % 2 == 0:
            values = _below(words, 1_000_000)
            sparse[f"n{column}"] = pa.array(values, type=pa.int64(), mask=~keep)
        else:
            values = np.round(
                (words >> np.uint64(11)).astype(np.float64) / float(1 << 53) * 100.0, 6
            )
            sparse[f"n{column}"] = pa.array(values, type=pa.float64(), mask=~keep)
    tables["nulls"] = pa.table(sparse)

    return tables


def _write(table: pa.Table, base: Path, formats: list[str]) -> dict:
    """Writes a table in each requested format and digests it.

    Args:
        table: The table.
        base: The path without an extension.
        formats: Which formats to write.

    Returns:
        A mapping from format to path, byte size and digest.
    """
    written = {}
    for fmt in formats:
        path = base.with_suffix("." + fmt)
        started = time.perf_counter()
        if fmt == "parquet":
            # No compression: the point is to measure engines, and a decompressor
            # is a different measurement wearing the same name.
            pq.write_table(table, path, compression="none")
        elif fmt == "csv":
            pacsv.write_csv(table, path)
        else:
            raise SystemExit(f"unknown format: {fmt}")
        written[fmt] = {
            "path": str(path.relative_to(DATA_ROOT.parent)),
            "bytes": path.stat().st_size,
            "sha256": _digest(path),
            "write_s": round(time.perf_counter() - started, 3),
        }
    return written


def _digest(path: Path) -> str:
    """Digests a file, so a rerun can prove it used the same bytes.

    Args:
        path: The file.

    Returns:
        The hex sha256.
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build(suite: str, size: str, formats: list[str], force: bool) -> Path:
    """Generates a suite's datasets and writes a manifest beside them.

    Args:
        suite: The suite name.
        size: The size name.
        formats: Which file formats to write.
        force: Whether to regenerate files that already exist.

    Returns:
        The path to the manifest.

    Raises:
        SystemExit: If the suite or size is not one this generates.
    """
    if suite == "tpch":
        # TPC-H is not generated here. Its data comes from dbgen and its queries
        # come from the specification, both by way of DuckDB's tpch extension,
        # which is the point of having it: a suite nobody in this repository
        # wrote is a suite nobody in this repository can have tuned for.
        import tpch

        return tpch.build(size, DATA_ROOT, force)

    if suite not in ("db-benchmark", "ingestion"):
        raise SystemExit(f"unknown suite '{suite}'. Known: db-benchmark, ingestion, tpch.")

    known = INGESTION_SIZES if suite == "ingestion" else SIZES
    if size not in known:
        raise SystemExit(f"unknown size '{size}' for {suite}. Known: {', '.join(known)}")

    rows = known[size]
    out = DATA_ROOT / suite / size
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"

    if manifest_path.exists() and not force:
        print(f"{manifest_path} exists, reusing. Pass --force to regenerate.")
        return manifest_path

    if suite == "ingestion":
        # The reader is the thing under test, so there is nothing to write but
        # CSV and asking for Parquet here would be asking for a file no query in
        # the suite opens.
        formats = ["csv"]

    print(f"generating {rows:,} rows for {suite} at {size}")
    started = time.perf_counter()
    if suite == "ingestion":
        tables = generate_ingestion(rows)
    else:
        tables = {"groupby": generate_groupby(rows)}
        tables.update(generate_join_tables(rows))
    generated_s = time.perf_counter() - started

    files = {}
    for name, table in tables.items():
        print(f"  writing {name} ({table.num_rows:,} rows)")
        files[name] = _write(table, out / name, formats)

    manifest = {
        "suite": suite,
        "size": size,
        "rows": rows,
        # Per table, because the ingestion tables are not all the same height and
        # a reader working out throughput from the suite's row count would be
        # wrong about the wide file by a factor of ten.
        "table_rows": {name: table.num_rows for name, table in tables.items()},
        "generator": "splitmix64 counter stream, matching firepanda/testing/rng.mojo",
        "seed": "0x243F6A8885A308D3",
        "generated_s": round(generated_s, 3),
        "files": files,
    }
    if suite == "db-benchmark":
        manifest["cardinality_factor"] = K
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {manifest_path}")
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    """Runs the generator from the command line.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite", default="db-benchmark", choices=("db-benchmark", "tpch", "ingestion")
    )
    parser.add_argument(
        "--size",
        default="",
        help="0.5GB, 5GB or 50GB for db-benchmark; sf1, sf10 or sf100 for tpch; "
        "1M, 10M or 100M for ingestion",
    )
    parser.add_argument(
        "--formats",
        default="parquet",
        help="comma separated. CSV is large and only the ingestion suite needs it.",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    size = args.size or DEFAULT_SIZE[args.suite]
    build(args.suite, size, args.formats.split(","), args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
