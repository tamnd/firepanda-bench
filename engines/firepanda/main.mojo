"""The firepanda side of the harness, run as a separate process by `worker.py`.

firepanda cannot read a Parquet file yet, so it cannot be handed the same file
pandas and polars are handed. What it can do is generate the same numbers. The
dataset generator in `tools/data.py` is splitmix64 over a counter, and this file
is that same generator written out again in Mojo. Every constant, every stream
offset and every reduction to a bounded range is the same, so the columns this
produces are equal to the columns in the Parquet file value for value.

That is a claim rather than an assumption, and the harness checks it. Every engine
reports the row count of its answer and the sum of every numeric column in it, the
runner fingerprints those, and an engine whose fingerprint does not match the rest
is reported as a disagreement rather than as a fast result. If the generator here
ever drifts from the one in Python, the join queries fail that check on the first
run, because a join of two tables generated from different streams does not
produce the same row count.

Everything except q8 and q9 is here. q8 needs a top-k per group and q9 needs a
correlation, and neither kernel exists. Those two are reported as unsupported,
with the reason, rather than quietly dropped.

The string keyed queries, q1, q2, q3, q7 and q10, arrived with firepanda 0.6.6
and 0.6.7, which put a string column in a `DataFrame` and let one be a group by
key and an aggregation input. An answer holding a text column is compared the
same way the other engines' answers are, by the sum of a 64 bit FNV-1a over every
value, which this file computes rather than borrows.

The process prints one line of JSON on stdout. Anything else goes to stderr.

Usage:
    firepanda-driver --query=q4 --rows=10000000 --runs=10
    firepanda-driver --suite=ingestion --query=csv_narrow --path=narrow.csv --runs=7
"""

from std.collections.span import Span
from std.sys import argv
from std.time import perf_counter_ns

from firepanda.array.any import AnyArray
from firepanda.array.array import Array
from firepanda.array.strings import StringArray, StringBuilder
from firepanda.dtype import Field, LogicalType, Schema
from firepanda.frame.frame import DataFrame
from firepanda.frame.groupby import AggSpec
from firepanda.frame.series import Series
from firepanda.io import ReadOptions, read_csv, read_csv_as
from firepanda.join import JoinKind
from firepanda.kernel import AggKind, subtract

# 64 bit FNV-1a, which is what `tools/engines/__init__.py` hashes a text value
# with. The two constants are the published ones and the null constant is the
# harness's own, and all three have to match it exactly or every string keyed
# query is reported as a disagreement.
comptime FNV_OFFSET: UInt64 = 0xCBF29CE484222325
comptime FNV_PRIME: UInt64 = 0x100000001B3
comptime NULL_HASH: UInt64 = 0x9E3779B97F4A7C15

# The generator constants, matching `tools/data.py` exactly. The seeds are the
# hexadecimal digits of pi and the multipliers are Vigna's splitmix64.
comptime GOLDEN: UInt64 = 0x9E3779B97F4A7C15
comptime MIX_A: UInt64 = 0xBF58476D1CE4E5B9
comptime MIX_B: UInt64 = 0x94D049BB133111EB
comptime SEED: UInt64 = 0x243F6A8885A308D3
comptime JOIN_SEED: UInt64 = SEED ^ 0x5DEECE66D
comptime RIGHT_SEED: UInt64 = SEED ^ 0x9E3779B9

# The h2oai G1 cardinality factor. id4 and id5 take K distinct values and id6
# takes rows / K, and the gap between those two is the whole point of the design.
comptime K = 100

# 2^53, the largest integer a float64 represents exactly, which is what turns a
# random word into a uniform double the same way numpy's `random_sample` does.
comptime TWO_53 = Float64(9007199254740992)


def mix(z0: UInt64) -> UInt64:
    """Applies the splitmix64 finalizer to a counter value.

    Args:
        z0: The counter value, already multiplied and offset by the seed.

    Returns:
        The mixed word.
    """
    var z = z0
    z = (z ^ (z >> 30)) * MIX_A
    z = (z ^ (z >> 27)) * MIX_B
    return z ^ (z >> 31)


def word(seed: UInt64, skip: Int, index: Int) -> UInt64:
    """Returns one word of a splitmix64 stream without generating the ones before it.

    This is the counter form. Calling `next_u64` n times and calling this with
    index 0 through n-1 give the same sequence, and this one computes any element
    on its own, which is what lets numpy vectorize it on the Python side and lets
    a column here be filled in any order.

    Args:
        seed: The stream seed.
        skip: How many words of the stream this column starts past.
        index: The position within the column.

    Returns:
        The word.
    """
    return mix(seed + UInt64(skip + index + 1) * GOLDEN)


def bounded_column(
    seed: UInt64, stream: Int, rows: Int, bound: Int
) -> Array[DType.int32]:
    """Builds a one-based integer key column from one stream.

    The reduction is a remainder, matching `Rng.next_below` and matching the
    Python generator including its bias, which is around two to the minus forty
    for these bounds and moves no query.

    Args:
        seed: The stream seed.
        stream: Which stream, counted in whole columns.
        rows: How many rows.
        bound: The number of distinct values.

    Returns:
        An int32 column of values in one through `bound`.
    """
    var out = Array[DType.int32](rows)
    var skip = stream * rows
    var divisor = UInt64(bound)
    for i in range(rows):
        out[i] = Int32(Int(word(seed, skip, i) % divisor) + 1)
    return out^


def uniform_column(
    seed: UInt64, stream: Int, rows: Int, rounded: Bool
) -> Array[DType.float64]:
    """Builds a float column of values uniform in zero to a hundred.

    Args:
        seed: The stream seed.
        stream: Which stream, counted in whole columns.
        rows: How many rows.
        rounded: Whether to round to six decimal places, which the group by
            table's v3 does and the join tables' floats do not.

    Returns:
        A float64 column.
    """
    var out = Array[DType.float64](rows)
    var skip = stream * rows
    for i in range(rows):
        var value = Float64(word(seed, skip, i) >> 11) / TWO_53 * 100.0
        if rounded:
            value = round(value * 1e6) / 1e6
        out[i] = value
    return out^


def text_column(
    seed: UInt64, stream: Int, rows: Int, bound: Int
) -> StringArray:
    """Builds a string key column from one stream.

    The Python generator writes `"id" + str(below(stream, bound))` and does not
    add one to the draw, while the integer columns beside it do. That asymmetry
    is in the h2oai generator and it is reproduced rather than tidied up, because
    a column that differs from the file by one is a column that fails the
    agreement check for a reason nobody would guess.

    Every value fits the twelve bytes an element carries inline, up to a hundred
    million rows, so this allocates no payload at these sizes.

    Args:
        seed: The stream seed.
        stream: Which stream, counted in whole columns.
        rows: How many rows.
        bound: The number of distinct values.

    Returns:
        A string column of values from "id0" through "id" and `bound` minus one.
    """
    var builder = StringBuilder(rows)
    var skip = stream * rows
    var divisor = UInt64(bound)
    for i in range(rows):
        var value = String("id", word(seed, skip, i) % divisor)
        builder.append(value.as_bytes())
    return builder^.finish()


def sequence_column(count: Int) -> Array[DType.int32]:
    """Builds the one through n key column a right join table has.

    Args:
        count: How many rows.

    Returns:
        An int32 column.
    """
    var out = Array[DType.int32](count)
    for i in range(count):
        out[i] = Int32(i + 1)
    return out^


def groupby_frame(rows: Int) raises -> DataFrame:
    """Builds the db-benchmark group by table, all nine columns of it.

    The three string keys are built even for a query that reads none of them, and
    that costs memory a narrower table would not. It is deliberate. pandas,
    polars and DuckDB are all handed the whole nine column table for every query,
    so a firepanda that generated four columns for q4 would be reported as using
    less memory than three engines that were asked to hold more. The comparison
    is worth more than the number.

    Args:
        rows: How many rows.

    Returns:
        The frame.
    """
    var high = rows // K
    if high < 1:
        high = 1
    var columns: List[Series] = [
        Series("id1", text_column(SEED, 0, rows, K)),
        Series("id2", text_column(SEED, 1, rows, K)),
        Series("id3", text_column(SEED, 2, rows, high)),
        Series("id4", bounded_column(SEED, 3, rows, K)),
        Series("id5", bounded_column(SEED, 4, rows, K)),
        Series("id6", bounded_column(SEED, 5, rows, high)),
        Series("v1", bounded_column(SEED, 6, rows, 5)),
        Series("v2", bounded_column(SEED, 7, rows, 15)),
        Series("v3", uniform_column(SEED, 8, rows, True)),
    ]
    return DataFrame.from_series(columns^)


def join_left_frame(rows: Int) raises -> DataFrame:
    """Builds the large left join table.

    Args:
        rows: How many rows.

    Returns:
        The frame.
    """
    var small = rows // 1_000
    if small < 1:
        small = 1
    var medium = rows // 100
    if medium < 1:
        medium = 1
    var columns: List[Series] = [
        Series("id1", bounded_column(JOIN_SEED, 0, rows, small)),
        Series("id2", bounded_column(JOIN_SEED, 1, rows, medium)),
        Series("id3", bounded_column(JOIN_SEED, 2, rows, rows)),
        Series("v1", uniform_column(JOIN_SEED, 3, rows, False)),
    ]
    return DataFrame.from_series(columns^)


def join_right_frame(stream: Int, count: Int, key: String) raises -> DataFrame:
    """Builds one right join table.

    The key is a dense one through n, so every right row is distinct and the join
    is a lookup rather than an expansion. That is the db-benchmark design and it
    is what makes the output row count predictable enough to be a check.

    Args:
        stream: Which stream, which is 0, 1 and 2 for the three tables.
        count: How many rows.
        key: The key column name.

    Returns:
        The frame.
    """
    var columns: List[Series] = [
        Series(key, sequence_column(count)),
        Series("v2", uniform_column(RIGHT_SEED, stream, count, False)),
    ]
    return DataFrame.from_series(columns^)


struct Tables(Movable):
    """Whatever a query needs loaded, built once before the timed runs start."""

    var groupby: DataFrame
    """The group by table, empty for a join query."""

    var left: DataFrame
    """The left join table, empty for a group by query."""

    var right: DataFrame
    """The right join table, empty for a group by query."""

    def __init__(out self, var groupby: DataFrame, var left: DataFrame, var right: DataFrame):
        """Constructs the set.

        Args:
            groupby: The group by table.
            left: The left join table.
            right: The right join table.
        """
        self.groupby = groupby^
        self.left = left^
        self.right = right^


def load(query: String, rows: Int) raises -> Tables:
    """Builds only the tables the query reads.

    A join query does not pay for the group by table and the other way round,
    which matters because the peak memory reported here is the peak of the whole
    process and generating a table nobody reads would inflate it.

    Args:
        query: The query name.
        rows: How many rows in the large table.

    Returns:
        The tables.

    Raises:
        Error: If the query is not one this engine runs.
    """
    var medium = rows // 100
    if medium < 1:
        medium = 1
    var small = rows // 1_000
    if small < 1:
        small = 1

    if (
        query == "q1"
        or query == "q2"
        or query == "q3"
        or query == "q4"
        or query == "q5"
        or query == "q6"
        or query == "q7"
        or query == "q10"
    ):
        return Tables(groupby_frame(rows), DataFrame(), DataFrame())
    if query == "j1":
        return Tables(
            DataFrame(), join_left_frame(rows), join_right_frame(0, small, "id1")
        )
    if query == "j2" or query == "j3":
        return Tables(
            DataFrame(),
            join_left_frame(rows),
            join_right_frame(1, medium, "id2"),
        )
    if query == "j4" or query == "j5":
        return Tables(
            DataFrame(), join_left_frame(rows), join_right_frame(2, rows, "id3")
        )
    raise Error(String("firepanda does not run ", query))


def keys(name: String) -> List[String]:
    """Wraps one column name as a key list.

    Args:
        name: The column name.

    Returns:
        A single element list.
    """
    var out = List[String]()
    out.append(name)
    return out^


def run_query(query: String, ref tables: Tables) raises -> DataFrame:
    """Runs one query and returns its answer.

    Every answer is materialized, because firepanda is eager and there is nothing
    else it could be. The join answers reduce to one row holding the output row
    count and two sums, matching what the other engines compute, so the comparison
    is of the join and not of how fast each engine can print six million rows.

    Args:
        query: The query name.
        tables: The loaded tables.

    Returns:
        The answer frame.

    Raises:
        Error: If the query is not one this engine runs.
    """
    if query == "q1":
        var specs: List[AggSpec] = [AggSpec("v1", AggKind.SUM, "v1")]
        return tables.groupby.group_by(keys("id1"), specs^, True, False)

    if query == "q2":
        var by: List[String] = ["id1", "id2"]
        var specs: List[AggSpec] = [AggSpec("v1", AggKind.SUM, "v1")]
        return tables.groupby.group_by(by^, specs^, True, False)

    if query == "q3":
        var specs: List[AggSpec] = [
            AggSpec("v1", AggKind.SUM, "v1"),
            AggSpec("v3", AggKind.MEAN, "v3"),
        ]
        return tables.groupby.group_by(keys("id3"), specs^, True, False)

    if query == "q4":
        var specs: List[AggSpec] = [
            AggSpec("v1", AggKind.MEAN, "v1"),
            AggSpec("v2", AggKind.MEAN, "v2"),
            AggSpec("v3", AggKind.MEAN, "v3"),
        ]
        return tables.groupby.group_by(keys("id4"), specs^, True, False)

    if query == "q5":
        var specs: List[AggSpec] = [
            AggSpec("v1", AggKind.SUM, "v1"),
            AggSpec("v2", AggKind.SUM, "v2"),
            AggSpec("v3", AggKind.SUM, "v3"),
        ]
        return tables.groupby.group_by(keys("id6"), specs^, True, False)

    if query == "q6":
        var by: List[String] = ["id4", "id6"]
        var specs: List[AggSpec] = [
            AggSpec("v3", AggKind.MEDIAN, "v3_median"),
            AggSpec("v3", AggKind.STD, "v3_sd"),
        ]
        return tables.groupby.group_by(by^, specs^, True, False)

    if query == "q7":
        var specs: List[AggSpec] = [
            AggSpec("v1", AggKind.MAX, "v1_max"),
            AggSpec("v2", AggKind.MIN, "v2_min"),
        ]
        var grouped = tables.groupby.group_by(keys("id3"), specs^, True, False)
        # The answer is the key, the two extremes and their difference, and only
        # the key and the difference are reported, which is what the other three
        # engines report. Columns 1 and 2 are the two aggregates, in the order
        # the specs were given.
        var span = subtract(
            grouped[1].as_typed[DType.int32](),
            grouped[2].as_typed[DType.int32](),
        )
        var wide = grouped.with_column(Series("range_v1_v2", span^))
        var wanted: List[String] = ["id3", "range_v1_v2"]
        return wide.select(wanted^)

    if query == "q10":
        var by: List[String] = ["id1", "id2", "id3", "id4", "id5", "id6"]
        var specs: List[AggSpec] = [
            AggSpec("v3", AggKind.SUM, "v3"),
            # A size rather than a count, so a null v1 would still be a row. The
            # generated data has no nulls, and the other engines all ask for a
            # size here, so this is the same question rather than a shortcut.
            AggSpec("v1", AggKind.SIZE, "count"),
        ]
        return tables.groupby.group_by(by^, specs^, True, False)

    if query == "j1":
        return reduce_join(tables.left.join(tables.right, keys("id1"), JoinKind.INNER))
    if query == "j2":
        return reduce_join(tables.left.join(tables.right, keys("id2"), JoinKind.INNER))
    if query == "j3":
        return reduce_join(tables.left.join(tables.right, keys("id2"), JoinKind.LEFT))
    if query == "j4":
        return reduce_join(tables.left.join(tables.right, keys("id3"), JoinKind.INNER))
    if query == "j5":
        return reduce_join(tables.left.join(tables.right, keys("id3"), JoinKind.LEFT))

    raise Error(String("firepanda does not run ", query))


def reduce_join(var joined: DataFrame) raises -> DataFrame:
    """Reduces a join result to one row of a count and two sums.

    A left join leaves nulls in v2 where nothing matched, and the sum skips them,
    which is what pandas, polars and DuckDB all do. That is the behaviour the
    fingerprint is checking, not an accident of this reduction.

    Args:
        joined: The join result.

    Returns:
        A one row frame with `rows`, `v1` and `v2`.
    """
    var height = len(joined)
    var specs: List[AggSpec] = [
        AggSpec("v1", AggKind.SUM, "v1"),
        AggSpec("v2", AggKind.SUM, "v2"),
    ]
    # A constant key column, so the group by reduces the whole frame to one row.
    var zeros = Array[DType.int32](height)
    var whole = joined.with_column(Series("all", zeros^))
    var out = whole.group_by(keys("all"), specs^, True, False)
    var counts = Array[DType.int64](len(out))
    for i in range(len(out)):
        counts[i] = Int64(height)
    return out.with_column(Series("rows", counts^)).drop(keys("all"))


def narrow_schema() raises -> Schema:
    """Returns the types `csv_narrow_typed` declares.

    These are `queries.NARROW_SCHEMA` in the harness, written out again here
    because a Mojo binary cannot read a Python tuple. An engine that declared a
    narrower integer than the others would be reading a different file from them,
    so the two lists have to say the same thing.

    Returns:
        The schema, in the file's column order.
    """
    var fields = List[Field]()
    fields.append(Field("id", LogicalType.INT64))
    fields.append(Field("pair", LogicalType.INT64))
    fields.append(Field("score", LogicalType.FLOAT64))
    fields.append(Field("label", LogicalType.STRING))
    return Schema(fields^)


def read_one(query: String, path: String) raises -> DataFrame:
    """Reads one CSV file, which is the whole of an ingestion measurement.

    Args:
        query: Which ingestion query, since one of them declares the types.
        path: The file.

    Returns:
        The frame that was read.

    Raises:
        Error: If the file cannot be read, or is not readable as CSV.
    """
    var options = ReadOptions()
    if query != "csv_narrow_typed":
        return read_csv(path, options)
    return read_csv_as(path, narrow_schema(), options)


def column_bytes(ref column: AnyArray) raises -> Float64:
    """Totals the byte lengths of a text column's values, skipping nulls.

    The ingestion suite does not hash its text, because an answer there has as
    many rows as the file has and the harness computes its side of that in Python.
    Byte length and null count are what it compares instead, and this is the first
    of the two.

    Args:
        column: The column, which must be a text column.

    Returns:
        The total, as a float, because that is the field the harness reads it
        into.
    """
    var total = Float64(0)
    ref text = column.strings()
    for i in range(len(text)):
        if text.is_valid(i):
            total += Float64(text.byte_length(i))
    return total


def column_sum(ref column: AnyArray) raises -> Float64:
    """Sums a column as a float, skipping nulls.

    This is deliberately a plain loop rather than the SIMD reduction the library
    has. It runs once per measurement and never inside a timed run, and what it
    has to be is obviously correct, because it is what the cross engine agreement
    check is built on.

    Args:
        column: The column.

    Returns:
        The sum, or zero for a column of no numeric type.
    """
    var total = Float64(0)
    if column.is_string():
        # A string column's physical dtype is uint8, so without this the arm
        # below would sum the first byte of every view and report it as a
        # column sum. `column_hash` is what covers a text column.
        return total
    var dtype = column.dtype()
    if dtype == DType.int32:
        var typed = column.as_typed[DType.int32]()
        for i in range(len(typed)):
            if typed.is_valid(i):
                total += Float64(typed[i])
    elif dtype == DType.int64:
        var typed = column.as_typed[DType.int64]()
        for i in range(len(typed)):
            if typed.is_valid(i):
                total += Float64(typed[i])
    elif dtype == DType.uint32:
        var typed = column.as_typed[DType.uint32]()
        for i in range(len(typed)):
            if typed.is_valid(i):
                total += Float64(typed[i])
    elif dtype == DType.uint64:
        var typed = column.as_typed[DType.uint64]()
        for i in range(len(typed)):
            if typed.is_valid(i):
                total += Float64(typed[i])
    elif dtype == DType.float64:
        var typed = column.as_typed[DType.float64]()
        for i in range(len(typed)):
            if typed.is_valid(i):
                total += typed[i]
    elif dtype == DType.float32:
        var typed = column.as_typed[DType.float32]()
        for i in range(len(typed)):
            if typed.is_valid(i):
                total += Float64(typed[i])
    return total


def column_hash(ref column: AnyArray) raises -> UInt64:
    """Digests a text column the way the harness digests one.

    The harness sums a 64 bit FNV-1a over each value's bytes, modulo two to the
    sixty four, and a null contributes a fixed constant. A sum rather than a hash
    over sorted values, so that neither side has to sort, which is what makes it
    something this file can compute at all.

    Args:
        column: The column, which must be a text column.

    Returns:
        The digest.
    """
    var total = UInt64(0)
    ref text = column.strings()
    for i in range(len(text)):
        if not text.is_valid(i):
            total += NULL_HASH
            continue
        var digest = FNV_OFFSET
        for byte in text.unsafe_bytes(i):
            digest = (digest ^ UInt64(byte)) * FNV_PRIME
        total += digest
    return total


def read_cpu_ticks() -> List[Int]:
    """Reads this process's user and system CPU time from `/proc/self/stat`.

    The two fields are the fourteenth and fifteenth, counted from one, and they
    are in clock ticks rather than seconds. The tick is USER_HZ, which is a
    hundred on every Linux the kernel ships as a supported configuration, so a
    tick is ten milliseconds. That is far too coarse to time one run of a query
    that takes twenty milliseconds, which is why the harness only ever reads
    these once before the first run and once after the last, and reports CPU for
    the timed region as a whole. Ten runs of twenty milliseconds is two hundred
    milliseconds, and ten milliseconds of quantization on that is a few percent.

    The name of the executable is the second field and it is wrapped in
    parentheses and may itself contain spaces, which is why this finds the last
    closing parenthesis and counts from there rather than splitting the line.

    Returns:
        The user and system times in ticks, or two zeros if `/proc` is absent.
    """
    var out: List[Int] = [0, 0]
    try:
        with open("/proc/self/stat", "r") as handle:
            var text = handle.read()
            var close = text.rfind(")")
            if close < 0:
                return out^
            # Field 3 onwards, so field 14 is index 11 and field 15 is index 12.
            var fields = String(text[byte = close + 1 :]).split()
            if len(fields) > 12:
                out[0] = Int(String(fields[11]))
                out[1] = Int(String(fields[12]))
    except:
        pass
    return out^


def status_field(text: String, field: String) -> Int:
    """Pulls one numeric field out of the contents of `/proc/self/status`.

    Args:
        text: The file contents.
        field: The field name, without its colon.

    Returns:
        The value, or zero if the kernel did not report it.
    """
    var wanted = String(field, ":")
    for line in text.split("\n"):
        if line.startswith(wanted):
            var parts = line.split()
            if len(parts) >= 2:
                try:
                    return Int(String(parts[1]))
                except:
                    return 0
    return 0


struct ProcessFacts(Copyable, Movable):
    """What the kernel says about this process, read from `/proc/self/status`."""

    var peak_rss_bytes: Int
    """The high water mark of resident memory, which never falls."""

    var rss_bytes: Int
    """Resident memory right now."""

    var threads: Int
    """How many threads exist right now."""

    var voluntary_switches: Int
    """Context switches this process asked for, which is mostly waiting."""

    var involuntary_switches: Int
    """Context switches the scheduler imposed, which is mostly contention."""

    def __init__(out self):
        """Constructs an all-zero set of facts, for a kernel that reports none."""
        self.peak_rss_bytes = 0
        self.rss_bytes = 0
        self.threads = 0
        self.voluntary_switches = 0
        self.involuntary_switches = 0


def read_process_facts() -> ProcessFacts:
    """Reads the process facts, returning zeros where the file is not readable.

    On anything that is not Linux there is no `/proc`, and the harness reports
    zero rather than failing, because a machine that cannot report memory can
    still report time.

    Returns:
        The facts.
    """
    var facts = ProcessFacts()
    try:
        with open("/proc/self/status", "r") as handle:
            var text = handle.read()
            # The kernel reports these in kibibytes and the harness reports bytes.
            facts.peak_rss_bytes = status_field(text, "VmHWM") * 1024
            facts.rss_bytes = status_field(text, "VmRSS") * 1024
            facts.threads = status_field(text, "Threads")
            facts.voluntary_switches = status_field(
                text, "voluntary_ctxt_switches"
            )
            facts.involuntary_switches = status_field(
                text, "nonvoluntary_ctxt_switches"
            )
    except:
        pass
    return facts^


def flag(name: String, fallback: String) -> String:
    """Reads a `--name=value` argument.

    Args:
        name: The flag name, without dashes.
        fallback: What to return when the flag is absent.

    Returns:
        The value.
    """
    var prefix = String("--", name, "=")
    var args = argv()
    for i in range(1, len(args)):
        var arg = args[i]
        if arg.startswith(prefix):
            return String(arg[byte = prefix.byte_length() :])
    return fallback


def json_string(value: String) -> String:
    """Quotes a string for JSON.

    The only strings this emits are query names, column names and error messages,
    so the escaping it needs is quotes, backslashes and newlines.

    Args:
        value: The string.

    Returns:
        The quoted form.
    """
    var out = String('"')
    for char in value.codepoints():
        if char == Codepoint.ord('"'):
            out += '\\"'
        elif char == Codepoint.ord("\\"):
            out += "\\\\"
        elif char == Codepoint.ord("\n"):
            out += "\\n"
        else:
            out += String(char)
    out += '"'
    return out^


def main() raises:
    """Runs one query and prints one line of JSON."""
    var query = flag("query", "q4")
    var rows = Int(flag("rows", "1000000"))
    var runs = Int(flag("runs", "10"))
    var suite = flag("suite", "db-benchmark")
    var path = flag("path", "")
    var reading = suite == "ingestion"

    var before = read_process_facts()

    var load_start = perf_counter_ns()
    var tables: Tables
    try:
        # An ingestion query loads nothing before it is timed. Opening the file
        # is the measurement, so anything done here would be work taken out of
        # the number.
        tables = Tables(
            DataFrame(), DataFrame(), DataFrame()
        ) if reading else load(query, rows)
    except error:
        print(
            String(
                '{"ok": false, "query": ',
                json_string(query),
                ', "note": ',
                json_string(String(error)),
                "}",
            )
        )
        return
    var load_ns = perf_counter_ns() - load_start
    var loaded = read_process_facts()

    var cpu_before = read_cpu_ticks()
    var timings = List[Int]()
    var run_rss = List[Int]()
    var run_peaks = List[Int]()
    var answer = DataFrame()
    var failure = String()
    for _ in range(runs):
        # Free the previous run's frame before the clock starts. Assigning into
        # `answer` below destroys it inside the timed region, and on the
        # ingestion files that is gigabytes of free charged to the wrong run.
        answer = DataFrame()
        var started = perf_counter_ns()
        # A read can fail on the file rather than on the code, which a generated
        # query cannot, so the run is reported as a failure with its reason
        # instead of leaving the harness to explain a driver that printed
        # nothing.
        try:
            answer = read_one(query, path) if reading else run_query(query, tables)
        except error:
            failure = String(error)
            break
        timings.append(perf_counter_ns() - started)
        # Read once per run rather than on a sampling thread. A thread that woke
        # every few milliseconds would show up in this process's own CPU and
        # context switch numbers, which are two of the things being reported.
        var each = read_process_facts()
        run_rss.append(each.rss_bytes)
        run_peaks.append(each.peak_rss_bytes)

    if failure:
        print(
            String(
                '{"ok": false, "query": ',
                json_string(query),
                ', "note": ',
                json_string(failure),
                "}",
            )
        )
        return

    var cpu_after = read_cpu_ticks()
    var after = read_process_facts()

    # Numbers go in `sums` and text goes in `hashes`, and a column is in exactly
    # one of the two. The harness splits an answer the same way, so a column
    # landing in the wrong one reads as a missing column rather than as a
    # different value.
    var names = answer.names()
    var sums = String("{")
    var hashes = String("{")
    var summed = 0
    var hashed = 0
    for i in range(len(names)):
        if reading:
            # An ingestion answer is the whole file, so it is reduced the way
            # `read_digest` in the harness reduces it: a null count for every
            # column, and a byte total for a text column where a numeric one
            # gets a sum. Nothing is hashed, because the harness would have to
            # hash ten million values in Python to compare it.
            if summed > 0:
                sums += ", "
            sums += String(
                json_string(String(names[i], ".nulls")),
                ": ",
                Float64(answer[i].null_count()),
                ", ",
            )
            if answer[i].is_string():
                sums += String(
                    json_string(String(names[i], ".bytes")),
                    ": ",
                    column_bytes(answer[i]),
                )
            else:
                sums += String(
                    json_string(String(names[i], ".sum")), ": ", column_sum(answer[i])
                )
            summed += 1
        elif answer[i].is_string():
            if hashed > 0:
                hashes += ", "
            hashes += String(json_string(names[i]), ": ", column_hash(answer[i]))
            hashed += 1
        else:
            if summed > 0:
                sums += ", "
            sums += String(json_string(names[i]), ": ", column_sum(answer[i]))
            summed += 1
    sums += "}"
    hashes += "}"

    var runs_json = String("[")
    for i in range(len(timings)):
        if i > 0:
            runs_json += ", "
        runs_json += String(
            '{"wall_s": ',
            Float64(timings[i]) / 1e9,
            ', "rss_bytes": ',
            run_rss[i],
            ', "peak_rss_bytes": ',
            run_peaks[i],
            "}",
        )
    runs_json += "]"

    # A tick is a hundredth of a second. See `read_cpu_ticks` for why this covers
    # the whole timed region rather than one run.
    var cpu_user_s = Float64(cpu_after[0] - cpu_before[0]) / 100.0
    var cpu_sys_s = Float64(cpu_after[1] - cpu_before[1]) / 100.0

    print(
        String(
            '{"ok": true, "query": ',
            json_string(query),
            ', "rows_in": ',
            rows,
            ', "load_s": ',
            Float64(load_ns) / 1e9,
            ', "runs": ',
            runs_json,
            ', "runs_cpu_user_s": ',
            cpu_user_s,
            ', "runs_cpu_sys_s": ',
            cpu_sys_s,
            ', "rows_out": ',
            len(answer),
            ', "cols_out": ',
            answer.width(),
            ', "sums": ',
            sums,
            ', "hashes": ',
            hashes,
            ', "peak_rss_bytes": ',
            after.peak_rss_bytes,
            ', "rss_before_load_bytes": ',
            before.rss_bytes,
            ', "rss_after_load_bytes": ',
            loaded.rss_bytes,
            ', "rss_end_bytes": ',
            after.rss_bytes,
            ', "threads": ',
            after.threads,
            ', "voluntary_switches": ',
            after.voluntary_switches - before.voluntary_switches,
            ', "involuntary_switches": ',
            after.involuntary_switches - before.involuntary_switches,
            "}",
        )
    )
