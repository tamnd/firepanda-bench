#!/usr/bin/env python3
"""Generates the datasets, deterministically, so every engine gets the same bytes.

The db-benchmark group by dataset is the h2oai G1 design: three string key columns
and three integer ones, spanning cardinalities from a hundred distinct values to
one per hundred rows, plus two small integer measures and one float. The queries
are chosen so that between them they hit every cardinality, and the interesting
ones are the high cardinality ones, because that is where a hash table either
works or does not.

The generator is splitmix64 rather than numpy's own, and that is a deliberate
choice with a specific payoff. firepanda cannot read a file yet, so the only way
to benchmark it against pandas on the same data today is for it to generate the
same data. splitmix64 over a counter is four lines in any language, it is what
`firepanda/testing/rng.mojo` already implements, and a Mojo engine seeded the same
way produces the same column byte for byte. The harness verifies that by comparing
answers rather than trusting it.

Usage:
    python tools/data.py --suite db-benchmark --size 0.5GB [--formats parquet,csv]
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

# The size to build when none is named, per suite.
DEFAULT_SIZE = {"db-benchmark": "0.5GB", "tpch": "sf1"}

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

    if suite != "db-benchmark":
        raise SystemExit(f"unknown suite '{suite}'. Known: db-benchmark, tpch.")
    if size not in SIZES:
        raise SystemExit(f"unknown size '{size}'. Known: {', '.join(SIZES)}")

    rows = SIZES[size]
    out = DATA_ROOT / suite / size
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"

    if manifest_path.exists() and not force:
        print(f"{manifest_path} exists, reusing. Pass --force to regenerate.")
        return manifest_path

    print(f"generating {rows:,} rows for {suite} at {size}")
    started = time.perf_counter()
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
        "cardinality_factor": K,
        "generator": "splitmix64 counter stream, matching firepanda/testing/rng.mojo",
        "seed": "0x243F6A8885A308D3",
        "generated_s": round(generated_s, 3),
        "files": files,
    }
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
    parser.add_argument("--suite", default="db-benchmark", choices=("db-benchmark", "tpch"))
    parser.add_argument(
        "--size",
        default="",
        help="0.5GB, 5GB or 50GB for db-benchmark; sf1, sf10 or sf100 for tpch",
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
