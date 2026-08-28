"""The engines, and what an engine has to provide to be in the table.

An engine is a module with a name, a version, a loader and a mapping from query
name to a function that runs it. The function must return a materialized answer,
because a lazy engine that is never asked for a result has not done any work, and
the harness digests that answer so two engines that disagree cannot be reported as
having run the same query.

An engine may implement one suite and not another, and may implement part of a
suite. Both are reported rather than hidden: a query an engine cannot run appears
in the table with the reason it could not, which is the only way a reader can tell
a hard query from a missing feature.

An engine that is not a Python library sets `EXTERNAL = True` and provides
`measure` instead of `load` and a query map. The firepanda engine is the only one
of those today, because it is written in Mojo and runs as its own process.

Adding an engine means adding a module here and nothing else.
"""

from __future__ import annotations

import hashlib
import importlib
import json

import pyarrow as pa
import pyarrow.compute as pc

KNOWN = ("pandas", "polars", "duckdb", "firepanda")

# How the tables reach an engine.
#
# `memory` reads every table into memory before the timed region, for every
# engine, so what is measured is query execution over identical data. That is the
# comparison this harness reports by default, because it is the one where a
# difference is attributable to the engine rather than to which of them happens to
# own a Parquet reader with projection pushdown.
#
# `scan` lets an engine that can push a projection into the file do it, which is
# the end to end number and the one published TPC-H comparisons use. Polars and
# DuckDB gain from it, pandas cannot and reads eagerly either way, and the report
# says so rather than letting the reader assume the modes are comparable.
IO_MODES = ("memory", "scan")


def query_map(module, suite: str) -> dict:
    """Returns an engine's callables for one suite.

    Args:
        module: The engine module.
        suite: The suite name.

    Returns:
        A mapping from query name to a callable taking the loaded context. Empty
        if the engine does not implement the suite at all.
    """
    if suite == "tpch":
        return getattr(module, "TPCH_QUERIES", {})
    return getattr(module, "QUERIES", {})


def load_engine(name: str):
    """Imports an engine module by name.

    Args:
        name: The engine name.

    Returns:
        The module.

    Raises:
        SystemExit: If the name is not a known engine.
    """
    if name not in KNOWN:
        raise SystemExit(f"unknown engine '{name}'. Known: {', '.join(KNOWN)}")
    return importlib.import_module(f"engines.{name}_engine")


def digest(answer) -> tuple[int, int, str, dict[str, float], dict[str, int]]:
    """Reduces an answer to a shape, a fingerprint and the numbers behind it.

    Two engines that ran the same query on the same data must produce the same
    answer. Getting there means throwing away everything that is a presentation
    difference rather than a difference in the answer: row order, column order,
    integer width, and the last few bits of a float, which no two engines
    accumulate in the same order and none of them are wrong to.

    What comes back is a row count, the sum of every numeric column and an order
    independent digest of every text column. That is deliberately weak, and on
    purpose: sums and summed hashes rather than a hash of the sorted rows, because
    a Mojo engine has to be able to compute the same thing and reimplementing
    Arrow's sort is not a reasonable price of entry. What they still catch is an
    engine that grouped differently, dropped rows, kept nulls it should have
    dropped or read the wrong file, which is the class of mistake that actually
    happens.

    The hex digest is those numbers rounded to nine significant figures and
    hashed, and it exists so a report can print one short string. Agreement is not
    decided on it. A hash has no tolerance, and rounding before hashing turns a
    difference in the tenth digit into a disagreement whenever it happens to
    straddle a boundary, which it does often enough to matter: pandas has to carry
    TPC-H money in float64 and its sums land a few parts in a billion from the
    exact decimal ones. `run.py` compares the sums themselves, with a relative
    tolerance, and that is the check.

    Args:
        answer: A pyarrow Table, or anything with `to_arrow` or `arrow` on it.

    Returns:
        The row count, the column count, a hex digest, the per column sums and the
        per column text digests.

    Raises:
        ValueError: If the answer has columns and none of them could be reduced to
            either a sum or a digest, since a fingerprint over nothing is a row
            count wearing a hash. This guard is what caught the decimal bug below,
            which is why it is still here now that it covers text as well.
    """
    table = as_arrow(answer)
    sums = column_sums(table)
    hashes = column_hashes(table)
    if table.num_columns and not sums and not hashes:
        raise ValueError(
            f"nothing in the answer can be fingerprinted: "
            f"{', '.join(f'{n} {table.schema.field(n).type}' for n in table.column_names)}"
        )
    return (
        table.num_rows,
        table.num_columns,
        fingerprint(table.num_rows, sums, hashes),
        sums,
        hashes,
    )


def column_sums(table: pa.Table) -> dict[str, float]:
    """Sums every numeric column of an answer.

    Decimal counts as numeric here, and getting that wrong is worth a paragraph
    because it hid itself so well. TPC-H money columns are DECIMAL(15,2), which
    is what the specification says they are and what DuckDB and Polars carry
    through a whole query. Arrow's `is_floating` and `is_integer` are both false
    for a decimal, so the first version of this quietly skipped every money
    column in the suite. The failure did not look like a missing column, it
    looked like an engine disagreement: fourteen of twenty two queries were
    reported as engines computing different answers, on a run where all three had
    just reproduced the specification's published output exactly.

    Worse, it made two different answers hash the same. Both q6 and q19 return
    one row of one decimal revenue column, so with the decimals dropped both
    fingerprinted as one row and no numeric columns, and an engine that returned
    q6's revenue for q19 would have passed.

    Booleans and dates are numeric here too, through a canonical integer. A
    boolean is nought or one, and a date or a timestamp is microseconds since the
    epoch, taken after a cast to `timestamp[us]` so that an engine answering with
    a date and one answering with a midnight timestamp are not made to disagree
    about a column they both got right.

    Args:
        table: The answer.

    Returns:
        A mapping from column name to sum. Text columns are left out; they are
        covered by `column_hashes`.
    """
    sums = {}
    for name in sorted(table.column_names):
        column = table.column(name)
        if (
            pa.types.is_floating(column.type)
            or pa.types.is_integer(column.type)
            or pa.types.is_decimal(column.type)
            or pa.types.is_boolean(column.type)
        ):
            # Through float64 whatever it started as, because two engines that
            # answer with a decimal and a double are not in disagreement about
            # the answer.
            sums[name] = float(pc.sum(pc.cast(column, pa.float64())).as_py() or 0.0)
        elif pa.types.is_temporal(column.type):
            micros = pc.cast(pc.cast(column, pa.timestamp("us")), pa.int64())
            sums[name] = float(pc.sum(micros).as_py() or 0.0)
    return sums


# 64 bit FNV-1a, which is here rather than a library hash because the Mojo driver
# has to compute the same number and this is four lines in any language.
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK64 = (1 << 64) - 1

# What a null hashes to. Any constant would do; this one is not a value any real
# string hashes to, and it matters only that every engine uses the same one.
NULL_HASH = 0x9E3779B97F4A7C15


def column_hashes(table: pa.Table) -> dict[str, int]:
    """Digests every text column of an answer, independently of row order.

    Sums are the whole comparison for numbers and no help at all for text, so
    until this existed a query answering with strings was fingerprinted on its row
    count alone. db-benchmark q1 groups by a string key and sums one column: two
    engines that grouped differently but landed on the same number of groups and
    the same total agreed. TPC-H q20 answers with two strings and nothing else,
    and the old code raised rather than admit it had nothing to say.

    The digest is the sum of a 64 bit FNV-1a over each value's bytes, taken modulo
    two to the sixty four. A sum rather than a hash of the sorted values, for the
    same reason the numeric side is a sum: the Mojo driver has to be able to
    compute it without reimplementing Arrow's sort. It catches a different set of
    groups, a dropped row and a wrong file, and it does not catch two rows swapped
    between columns of the same type, which is the price.

    Args:
        table: The answer.

    Returns:
        A mapping from column name to digest. Numeric columns are left out.
    """
    hashes = {}
    for name in sorted(table.column_names):
        column = table.column(name)
        if not (
            pa.types.is_string(column.type)
            or pa.types.is_large_string(column.type)
            or pa.types.is_binary(column.type)
            or pa.types.is_large_binary(column.type)
        ):
            continue
        total = 0
        for value in column.to_pylist():
            total += NULL_HASH if value is None else fnv1a(value)
        hashes[name] = total & MASK64
    return hashes


def fnv1a(value) -> int:
    """Hashes one text or binary value.

    Args:
        value: A `str` or `bytes`.

    Returns:
        The 64 bit FNV-1a digest.
    """
    data = value.encode() if isinstance(value, str) else value
    digest = FNV_OFFSET
    for byte in data:
        digest = ((digest ^ byte) * FNV_PRIME) & MASK64
    return digest


def fingerprint(rows: int, sums: dict[str, float], hashes: dict[str, int] | None = None) -> str:
    """Builds the cross engine fingerprint from a row count and the column digests.

    Every engine computes this, including the one written in Mojo, which is why it
    is a function taking numbers rather than a function taking a table.

    Args:
        rows: The number of rows in the answer.
        sums: The sum of each numeric column, by column name.
        hashes: The digest of each text column, by column name. Omitted by an
            engine that cannot yet return a text column, which is the firepanda
            driver today, and an answer with no text columns hashes the same
            either way.

    Returns:
        A short hex digest.
    """
    document = {"rows": rows, "sums": {k: _significant(v) for k, v in sorted(sums.items())}}
    if hashes:
        document["hashes"] = {k: str(v) for k, v in sorted(hashes.items())}
    canonical = json.dumps(document, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _significant(value: float, digits: int = 9) -> str:
    """Rounds a float to a fixed number of significant figures, as text.

    Text rather than a float, because two floats that agree to nine figures can
    still differ in their repr and the fingerprint is over the text.

    Args:
        value: The number.
        digits: How many significant figures to keep.

    Returns:
        The rounded number, formatted. A value that is not finite is returned by
        name, so a NaN in one engine's answer and not another's is a disagreement
        rather than a crash.
    """
    if value != value:
        return "nan"
    if value in (float("inf"), float("-inf")):
        return "inf" if value > 0 else "-inf"
    if value == 0.0:
        return "0"
    return f"{value:.{digits}g}"


def as_arrow(answer) -> pa.Table:
    """Converts whatever an engine returned into an Arrow table.

    Args:
        answer: The engine's answer.

    Returns:
        The answer as a table.

    Raises:
        TypeError: If the answer is not something Arrow can take.
    """
    if isinstance(answer, pa.Table):
        return answer
    for attribute in ("to_arrow", "arrow", "to_arrow_table"):
        converter = getattr(answer, attribute, None)
        if callable(converter):
            result = converter()
            if isinstance(result, pa.Table):
                return result
    if hasattr(answer, "__arrow_c_stream__"):
        return pa.table(answer)
    raise TypeError(f"cannot digest a {type(answer).__name__}")
