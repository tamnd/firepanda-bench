#!/usr/bin/env python3
"""TPC-H data, queries and answers, taken from the specification rather than retyped.

The data comes from `dbgen`, and the copy of `dbgen` used here is the one inside
DuckDB's `tpch` extension. That is a deliberate choice over building the TPC
toolkit from source. The extension's generator is a port of the reference `dbgen`
that produces the same rows for a given scale factor, it is the generator Polars,
DuckDB and most published TPC-H comparisons already use, and it needs no C
compiler on the machine running the benchmark. The scale factor and the DuckDB
version that generated the data both go in the manifest, so anyone can rebuild the
same bytes.

The query text comes from the same extension, through `tpch_queries()`. Nobody
types TPC-H SQL by hand into a benchmark: the substitution parameters are part of
the specification, and a query with the wrong date literal is a different query
with the same name. The dataframe engines cannot run SQL, so their versions are
written out in `polars_engine.py` and `pandas_engine.py`, and those are checked
against `tpch_answers()`, which is the specification's validation output. A
dataframe query that does not reproduce the official answer is not in the table.

Usage:
    python tools/tpch.py --size sf1
"""

from __future__ import annotations

import json
import time
from pathlib import Path

TABLES = (
    "customer",
    "lineitem",
    "nation",
    "orders",
    "part",
    "partsupp",
    "region",
    "supplier",
)

# The scale factors, by the name the result files use. A scale factor of one is
# about a gigabyte of CSV and about a quarter of that as uncompressed Parquet.
SCALES = {
    "sf1": 1.0,
    "sf10": 10.0,
    "sf100": 100.0,
}


def connect():
    """Opens a DuckDB connection with the TPC-H extension loaded.

    Returns:
        The connection.

    Raises:
        SystemExit: If the extension cannot be installed, which is almost always
            a machine with no network on its first run.
    """
    import duckdb

    connection = duckdb.connect()
    try:
        connection.execute("INSTALL tpch")
        connection.execute("LOAD tpch")
    except Exception as exc:
        raise SystemExit(
            "cannot load DuckDB's tpch extension, which is where the data "
            f"generator and the official query text come from: {exc}"
        ) from exc
    return connection


def official_queries() -> dict[str, str]:
    """Returns the twenty two official statements, keyed q1 through q22.

    Returns:
        A mapping from query name to SQL.
    """
    connection = connect()
    rows = connection.execute(
        "SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr"
    ).fetchall()
    return {f"q{int(number)}": text for number, text in rows}


def official_answers(scale: float) -> dict[str, str]:
    """Returns the specification's validation answers for a scale factor.

    DuckDB ships answers for the scale factors the TPC publishes them for. A scale
    factor with no published answer returns an empty mapping, and the harness then
    falls back to cross engine agreement, which is weaker and is reported as such.

    Args:
        scale: The scale factor.

    Returns:
        A mapping from query name to the answer as pipe separated text.
    """
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT query_nr, answer FROM tpch_answers() WHERE scale_factor = ? ORDER BY query_nr",
            [scale],
        ).fetchall()
    except Exception:
        return {}
    return {f"q{int(number)}": text for number, text in rows}


def generate(size: str, out: Path, force: bool) -> dict:
    """Runs dbgen at a scale factor and writes each table as Parquet.

    Args:
        size: The scale factor name.
        out: The directory to write into.
        force: Whether to regenerate tables that already exist.

    Returns:
        The files section of the manifest.

    Raises:
        SystemExit: If the size is not one the harness knows.
    """
    if size not in SCALES:
        raise SystemExit(f"unknown TPC-H size '{size}'. Known: {', '.join(SCALES)}")
    scale = SCALES[size]
    out.mkdir(parents=True, exist_ok=True)

    existing = [name for name in TABLES if (out / f"{name}.parquet").exists()]
    if len(existing) == len(TABLES) and not force:
        print(f"{out} already holds all eight tables, reusing. Pass --force to redo.")
        return _describe(out)

    connection = connect()
    print(f"running dbgen at scale factor {scale:g}")
    started = time.perf_counter()
    connection.execute(f"CALL dbgen(sf={scale})")
    print(f"  generated in {time.perf_counter() - started:.1f} s")

    files = {}
    for name in TABLES:
        path = out / f"{name}.parquet"
        began = time.perf_counter()
        # Uncompressed, matching the db-benchmark data, because a decompressor is
        # a different measurement wearing the same name.
        connection.execute(f"COPY {name} TO '{path}' (FORMAT PARQUET, COMPRESSION UNCOMPRESSED)")
        rows = connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"  {name:<9} {rows:>12,} rows  {path.stat().st_size / 1e6:8.1f} MB")
        files[name] = {
            "parquet": {
                "path": str(path),
                "bytes": path.stat().st_size,
                "rows": rows,
                "write_s": round(time.perf_counter() - began, 3),
            }
        }
    return files


def _describe(out: Path) -> dict:
    """Describes tables that are already on disk.

    Args:
        out: The directory holding them.

    Returns:
        The files section of the manifest.
    """
    files = {}
    for name in TABLES:
        path = out / f"{name}.parquet"
        files[name] = {"parquet": {"path": str(path), "bytes": path.stat().st_size}}
    return files


def build(size: str, root: Path, force: bool) -> Path:
    """Generates the TPC-H dataset and writes a manifest beside it.

    Args:
        size: The scale factor name.
        root: The data root.
        force: Whether to regenerate.

    Returns:
        The manifest path.
    """
    import duckdb

    out = root / "tpch" / size
    files = generate(size, out, force)
    manifest = {
        "suite": "tpch",
        "size": size,
        "scale_factor": SCALES[size],
        "generator": f"DuckDB {duckdb.__version__} tpch extension dbgen",
        "query_source": "DuckDB tpch_queries(), which carries the official statements",
        "answers_available": bool(official_answers(SCALES[size])),
        "files": files,
    }
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {path}")
    return path
