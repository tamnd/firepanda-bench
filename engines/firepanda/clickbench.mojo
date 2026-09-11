"""The 43 ClickBench queries in firepanda, written against the frame API.

These are the published statements expressed as dataframe operations, the same
way `tpch.mojo` holds the twenty two TPC-H queries, because a dataframe library
with no SQL front end in the loop can only be in a ClickBench table that way. The
statements themselves are vendored under `suites/clickbench/queries.sql` and
nothing here changes what any of them asks for.

Three things about this port are worth reading before the queries.

The first is the loading, and it is the same fairness problem TPC-H has.
firepanda has no Parquet decoder of its own: it opens a Parquet file by handing
it to DuckDB and reading DuckDB's vectors back as Arrow. DuckDB is one of the
four engines in this table, so that read cannot be put on a timer here without
reporting DuckDB's reader under firepanda's name. The table is therefore read
before the clock starts, which is exactly what pandas and Polars do under `--io
memory`, and `--io scan` is refused by name rather than answered with a number
that means something else. The conversions in `projection` are ClickBench's own,
copied from its DuckDB loader: the file stores the date as a count of days, the
three timestamps as counts of seconds and every text column as bytes with no
logical type, and an engine that skips any of that is not running the benchmark.

The second is the column names. The harness compares engines by row count, by a
sum per numeric column and by a digest per text column, and it looks those up by
name, so two engines that computed the same answer under different column names
are reported as disagreeing. The names here are the ones DuckDB gives the
published statements, down to `count_star()` and `(ClientIP - 1)`, because DuckDB
is the engine whose output names the other ports already follow.

The third is projection, and firepanda gets some of it for free and none of the
rest. `group_by` reads the key columns and the aggregated ones and nothing else,
so an unfiltered group by over a 105 column table costs what a three column table
would cost and needs no projection at all. A filter is the other case: `filter`
keeps every column, so a query that filters and then reads three columns would
carry the other hundred and two through the filter for nobody. `_keep` filters
the named columns only, which is projection pushdown done by hand, and it is the
same thing the TPC-H port does for the same reason. q23 is the exception, since
it asks for `SELECT *`, so the wide frame is the answer and the width is the
query.

One query is refused. q28 groups by `REGEXP_REPLACE(Referer, ...)`, firepanda has
no regular expression engine, and the refusal says so and names the issue. The
library does have `text_hostname`, which is that pattern written out by hand and
byte for byte, and using it here would be answering a benchmark query with a
kernel written for that one query. That measures the kernel rather than the
engine, so the refusal stands until there is a regex engine to run the pattern
the statement actually carries.
"""

from firepanda.array.any import AnyArray
from firepanda.array.array import Array
from firepanda.array.strings import StringArray, StringBuilder
from firepanda.array.value import Value
from firepanda.dtype import Field, LogicalType, Schema
from firepanda.frame.frame import DataFrame
from firepanda.frame.groupby import AggKind, AggSpec
from firepanda.frame.series import Series
from firepanda.io.parquet import Session
from firepanda.kernel import (
    filter_any,
    is_in_any,
    logical_and,
    reduce_any,
    text_pick,
)
from firepanda.kernel.binary import BinaryOp, binary_value_any
from firepanda.kernel.pattern import text_contains
from firepanda.kernel.reduce import distinct_count_any
from firepanda.kernel.substr import text_byte_length
from firepanda.kernel.temporal import (
    ROUND_DOWN,
    TemporalField,
    temporal_field,
    temporal_round,
)

comptime QUERY_COUNT = 43
"""How many queries ClickBench publishes, numbered q0 through q42."""

comptime REGEX_REFUSAL = String(
    "firepanda has no regular expression engine, so it cannot evaluate the",
    (
        " REGEXP_REPLACE this query groups by. It is tracked as"
        " tamnd/firepanda#480"
    ),
    " under milestone tamnd/firepanda#477, which is where the RE2 work sits.",
)
"""Why q28 is not answered, in the words the report prints beside the gap."""


def projection() -> String:
    """Returns the select list that gives the file the published schema.

    Returns:
        The projection, with the four type conversions ClickBench's own loader
        makes.
    """
    return String(
        "* REPLACE (make_date(EventDate) AS EventDate,",
        " epoch_ms(EventTime * 1000) AS EventTime,",
        " epoch_ms(ClientEventTime * 1000) AS ClientEventTime,",
        " epoch_ms(LocalEventTime * 1000) AS LocalEventTime)",
    )


def load_clickbench(pattern: String) raises -> DataFrame:
    """Reads the hits table.

    Args:
        pattern: The path the harness passed, which for this suite is a glob over
            the partitions rather than one file.

    Returns:
        The whole table, 105 columns wide, in the published types.

    Raises:
        Error: If the harness passed nothing, or the files cannot be read.
    """
    if pattern.byte_length() == 0:
        raise Error("clickbench: the harness passed no file to read")
    var session = Session()
    return session.run(
        String(
            "SELECT ",
            projection(),
            " FROM read_parquet('",
            pattern,
            "', binary_as_string=True)",
        )
    )


def day(year: Int, month: Int, dom: Int) -> Value:
    """Builds a date scalar from a calendar date.

    The days from the civil calendar, by Howard Hinnant's algorithm, which is
    what the TPC-H port uses and for the same reason: the column is a day count
    and the literal in the statement is a calendar date.

    Args:
        year: The year.
        month: The month, one through twelve.
        dom: The day of the month.

    Returns:
        A scalar typed as a date, holding the days since the epoch.
    """
    var y = year - (1 if month <= 2 else 0)
    var era = (y if y >= 0 else y - 399) // 400
    var yoe = y - era * 400
    var doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + dom - 1
    var doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    var out = Value(Int32(era * 146097 + doe - 719468))
    out.type = LogicalType.DATE32
    return out^


def _cmp(
    frame: DataFrame, name: String, op: BinaryOp, value: Value
) raises -> Array[DType.bool]:
    """Compares one column against a constant without copying the column.

    `DataFrame.column` copies and flattens, and on this table a column is a
    hundred million values, so every predicate here reads the column through
    `__getitem__` on a position, which borrows.

    Args:
        frame: The frame.
        name: The column to compare.
        op: The comparison.
        value: The constant to compare against.

    Returns:
        The mask.

    Raises:
        Error: If the column is missing, or as the comparison does.
    """
    var at = frame.schema.index_of(name)
    return binary_value_any(frame[at], value, op).as_typed[DType.bool]()


def _both(
    a: Array[DType.bool], b: Array[DType.bool]
) raises -> Array[DType.bool]:
    """Ands two masks together.

    Args:
        a: The first mask.
        b: The second, of the same length.

    Returns:
        The rows both are true on.

    Raises:
        Error: As `logical_and` does.
    """
    return logical_and(a, b)


def _not(var mask: Array[DType.bool]) raises -> Array[DType.bool]:
    """Negates a mask.

    Args:
        mask: The mask.

    Returns:
        The rows it was false on.

    Raises:
        Error: As the inversion does.
    """
    return (~Series("m", mask^)).as_typed[DType.bool]()


def _contains(
    frame: DataFrame, name: String, needle: String
) raises -> Array[DType.bool]:
    """Returns the rows whose text holds a substring, borrowing the column.

    This is `LIKE '%needle%'`, which is what four of these queries filter on and
    the only pattern shape ClickBench uses outside q28.

    Args:
        frame: The frame.
        name: The text column.
        needle: The substring.

    Returns:
        The mask.

    Raises:
        Error: If the column is missing or is not text.
    """
    var at = frame.schema.index_of(name)
    return text_contains(frame[at].strings(), needle.as_bytes())


def _nonempty(frame: DataFrame, name: String) raises -> Array[DType.bool]:
    """Returns the rows where a text column is not the empty string.

    Eleven of the queries filter this way. The hits table has no nulls and uses
    the empty string where another dataset would have one, so this is the suite's
    idea of a missing value test.

    Args:
        frame: The frame.
        name: The text column.

    Returns:
        The mask.

    Raises:
        Error: If the column is missing, or as the comparison does.
    """
    return _cmp(frame, name, BinaryOp.NE, Value(String("")))


def _zero(frame: DataFrame, name: String) raises -> Array[DType.bool]:
    """Returns the rows where a small integer flag is zero.

    Args:
        frame: The frame.
        name: The flag column, which for every flag on this table is int16.

    Returns:
        The mask.

    Raises:
        Error: As the comparison does.
    """
    return _cmp(frame, name, BinaryOp.EQ, Value(Int16(0)))


def _nonzero(frame: DataFrame, name: String) raises -> Array[DType.bool]:
    """Returns the rows where a small integer flag is not zero.

    Args:
        frame: The frame.
        name: The flag column.

    Returns:
        The mask.

    Raises:
        Error: As the comparison does.
    """
    return _cmp(frame, name, BinaryOp.NE, Value(Int16(0)))


def _keep(
    frame: DataFrame, names: List[String], mask: Array[DType.bool]
) raises -> DataFrame:
    """Filters a frame down to the rows a mask keeps and the columns named.

    Projection pushdown by hand, for the reason in the module docstring: `filter`
    keeps every column and this table has 105 of them, nineteen of which are text
    that most queries never look at.

    Args:
        frame: The frame to filter.
        names: The columns to keep, in the order the result should have them.
        mask: The rows to keep.

    Returns:
        A frame of the kept rows and the named columns.

    Raises:
        Error: If a name is missing, or as the filter does.
    """
    var fields = List[Field](capacity=len(names))
    var columns = List[AnyArray](capacity=len(names))
    for i in range(len(names)):
        var at = frame.schema.index_of(names[i])
        columns.append(filter_any(frame[at], mask))
        fields.append(Field(names[i], columns[i].type))
    return DataFrame(Schema(fields^), columns^)


def _answer(
    names: List[String], var values: List[AnyArray]
) raises -> DataFrame:
    """Assembles a frame from columns that were computed one at a time.

    Args:
        names: One name per column, in order.
        values: The columns.

    Returns:
        The frame.

    Raises:
        Error: If the two lists disagree in length.
    """
    if len(names) != len(values):
        raise Error("clickbench: a column is missing its name")
    var fields = List[Field](capacity=len(names))
    for i in range(len(names)):
        fields.append(Field(names[i], values[i].type))
    return DataFrame(Schema(fields^), values^)


def _int64(value: Int) -> AnyArray:
    """Builds the one element column a scalar count is.

    Args:
        value: The number.

    Returns:
        The column.
    """
    var out = Array[DType.int64](1)
    out[0] = Int64(value)
    return AnyArray(out^)


def _rows(mask: Array[DType.bool]) raises -> Int:
    """Counts the rows a mask keeps without materializing them.

    `COUNT(*)` under a `WHERE` is a population count and nothing else, so the
    mask is reduced rather than applied. Applying it would allocate the rows that
    survive in order to ask how many there are.

    Args:
        mask: The mask.

    Returns:
        How many rows are true.

    Raises:
        Error: As the reduction does.
    """
    var total = reduce_any(AnyArray(mask.copy()), AggKind.SUM)
    return Int(total.as_typed[DType.uint64]()[0])


def _size(column: String, name: String) -> AggSpec:
    """Returns the spec that counts a group's rows.

    `SIZE` rather than `COUNT`, because `COUNT(*)` counts rows and `COUNT(x)`
    counts the rows where `x` is present. This table has no nulls so the two
    agree on it, and they would not agree on a table that did.

    Args:
        column: Any column of the frame, since the count does not read it.
        name: What to call the answer.

    Returns:
        The spec.
    """
    return AggSpec(column, AggKind.SIZE, name)


def _group(
    frame: DataFrame, var by: List[String], var specs: List[AggSpec]
) raises -> DataFrame:
    """Groups a frame, leaving the groups in the order they were found.

    Unsorted, because every one of these statements either orders its answer
    afterwards or does not determine the order at all, so sorting a million
    groups by their key here would be work nobody asked for.

    Args:
        frame: The frame.
        by: The key columns.
        specs: What to compute.

    Returns:
        The grouped answer, keys first.

    Raises:
        Error: As the grouping does.
    """
    return frame.group_by(by^, specs^, True, False)


def _top(
    var frame: DataFrame,
    by: List[String],
    descending: List[Bool],
    limit: Int,
    offset: Int = 0,
) raises -> DataFrame:
    """Takes the rows an `ORDER BY` with a `LIMIT` and an `OFFSET` takes.

    `sort_limit` rather than a sort and a slice, because the answer is ten rows
    out of however many groups there are and sorting the rest of them is work the
    statement did not ask for.

    Args:
        frame: The answer before the limit.
        by: The ordering columns, most significant first.
        descending: One flag per key.
        limit: How many rows the limit takes.
        offset: How many rows the offset skips.

    Returns:
        The rows that survive.

    Raises:
        Error: As the sort does.
    """
    var nulls = List[Bool](capacity=len(by))
    for _ in range(len(by)):
        nulls.append(False)
    return frame.sort_limit(by, descending, nulls^, limit, offset)


def _having(
    var frame: DataFrame, column: String, minimum: Int
) raises -> DataFrame:
    """Drops the groups a `HAVING COUNT(*) > n` drops.

    Args:
        frame: The grouped answer.
        column: The column holding each group's row count.
        minimum: The count a group has to exceed.

    Returns:
        The groups that survive.

    Raises:
        Error: As the comparison and the filter do.
    """
    var kept = _cmp(frame, column, BinaryOp.GT, Value(Int64(minimum)))
    return frame.filter(kept)


def _counter62(
    hits: DataFrame, start: Value, end: Value
) raises -> Array[DType.bool]:
    """Builds the date and counter filter the last seven queries all start with.

    Args:
        hits: The table.
        start: The first date the range includes.
        end: The last date the range includes.

    Returns:
        The mask.

    Raises:
        Error: As the comparisons do.
    """
    var counter = _cmp(hits, "CounterID", BinaryOp.EQ, Value(Int32(62)))
    var after = _cmp(hits, "EventDate", BinaryOp.GE, start)
    var before = _cmp(hits, "EventDate", BinaryOp.LE, end)
    return _both(counter, _both(after, before))


def _july(hits: DataFrame) raises -> Array[DType.bool]:
    """Builds the filter over the whole of July 2013, which six queries share.

    Args:
        hits: The table.

    Returns:
        The mask.

    Raises:
        Error: As the comparisons do.
    """
    return _counter62(hits, day(2013, 7, 1), day(2013, 7, 31))


def _empties(rows: Int) raises -> StringArray:
    """Builds a text column of empty strings, which is a `CASE`'s else branch.

    Args:
        rows: How tall.

    Returns:
        The column.

    Raises:
        Error: As the builder does.
    """
    var builder = StringBuilder(capacity=rows)
    var nothing = String("")
    for _ in range(rows):
        builder.append(nothing.as_bytes())
    return builder^.finish()


def q0(hits: DataFrame) raises -> DataFrame:
    """Counts every row.

    Args:
        hits: The table.

    Returns:
        The answer, one row.

    Raises:
        Error: As the frame assembly does.
    """
    var names: List[String] = ["count_star()"]
    var values = List[AnyArray]()
    values.append(_int64(len(hits)))
    return _answer(names, values^)


def q1(hits: DataFrame) raises -> DataFrame:
    """Counts the rows with an ad engine set.

    Args:
        hits: The table.

    Returns:
        The answer, one row.

    Raises:
        Error: As the comparison does.
    """
    var names: List[String] = ["count_star()"]
    var values = List[AnyArray]()
    values.append(_int64(_rows(_nonzero(hits, "AdvEngineID"))))
    return _answer(names, values^)


def q2(hits: DataFrame) raises -> DataFrame:
    """Takes three scalar aggregates over the whole table.

    Args:
        hits: The table.

    Returns:
        The answer, one row.

    Raises:
        Error: As the reductions do.
    """
    var names: List[String] = [
        "sum(AdvEngineID)",
        "count_star()",
        "avg(ResolutionWidth)",
    ]
    var values = List[AnyArray]()
    values.append(
        reduce_any(hits[hits.schema.index_of("AdvEngineID")], AggKind.SUM)
    )
    values.append(_int64(len(hits)))
    values.append(
        reduce_any(hits[hits.schema.index_of("ResolutionWidth")], AggKind.MEAN)
    )
    return _answer(names, values^)


def q3(hits: DataFrame) raises -> DataFrame:
    """Averages a sixty four bit identifier, which is a scan and nothing else.

    Args:
        hits: The table.

    Returns:
        The answer, one row.

    Raises:
        Error: As the reduction does.
    """
    var names: List[String] = ["avg(UserID)"]
    var values = List[AnyArray]()
    values.append(
        reduce_any(hits[hits.schema.index_of("UserID")], AggKind.MEAN)
    )
    return _answer(names, values^)


def q4(hits: DataFrame) raises -> DataFrame:
    """Counts distinct users, exactly.

    Exactly rather than approximately, which is the whole story of this query.
    ClickHouse's published entry answers it with `uniq`, a HyperLogLog estimate,
    and that is why its number there is not comparable with DuckDB's.

    Args:
        hits: The table.

    Returns:
        The answer, one row.

    Raises:
        Error: As the distinct count does.
    """
    var names: List[String] = ["count(DISTINCT UserID)"]
    var values = List[AnyArray]()
    values.append(
        _int64(distinct_count_any(hits[hits.schema.index_of("UserID")]))
    )
    return _answer(names, values^)


def q5(hits: DataFrame) raises -> DataFrame:
    """Counts distinct search phrases, which is the same question over text.

    Args:
        hits: The table.

    Returns:
        The answer, one row.

    Raises:
        Error: As the distinct count does.
    """
    var names: List[String] = ["count(DISTINCT SearchPhrase)"]
    var values = List[AnyArray]()
    values.append(
        _int64(distinct_count_any(hits[hits.schema.index_of("SearchPhrase")]))
    )
    return _answer(names, values^)


def q6(hits: DataFrame) raises -> DataFrame:
    """Takes the first and last day the table covers.

    Args:
        hits: The table.

    Returns:
        The answer, one row.

    Raises:
        Error: As the reductions do.
    """
    var names: List[String] = ["min(EventDate)", "max(EventDate)"]
    var at = hits.schema.index_of("EventDate")
    var values = List[AnyArray]()
    values.append(reduce_any(hits[at], AggKind.MIN))
    values.append(reduce_any(hits[at], AggKind.MAX))
    return _answer(names, values^)


def q7(hits: DataFrame) raises -> DataFrame:
    """Counts rows per ad engine, ordered, with no limit under the order.

    The only grouped query in the suite that returns every group it built, which
    on this column is a handful.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["AdvEngineID"]
    var kept = _keep(hits, want, _nonzero(hits, "AdvEngineID"))
    var by: List[String] = ["AdvEngineID"]
    var specs: List[AggSpec] = [_size("AdvEngineID", "count_star()")]
    var grouped = _group(kept, by^, specs^)
    var order: List[String] = ["count_star()"]
    return grouped.sort_values(order, [True], [False])


def q8(hits: DataFrame) raises -> DataFrame:
    """Counts distinct users per region and takes the ten biggest.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var by: List[String] = ["RegionID"]
    var specs: List[AggSpec] = [AggSpec("UserID", AggKind.NUNIQUE, "u")]
    return _top(_group(hits, by^, specs^), ["u"], [True], 10)


def q9(hits: DataFrame) raises -> DataFrame:
    """Four aggregates per region, one of them a distinct count.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var by: List[String] = ["RegionID"]
    var specs: List[AggSpec] = [
        AggSpec("AdvEngineID", AggKind.SUM, "sum(AdvEngineID)"),
        _size("RegionID", "c"),
        AggSpec("ResolutionWidth", AggKind.MEAN, "avg(ResolutionWidth)"),
        AggSpec("UserID", AggKind.NUNIQUE, "count(DISTINCT UserID)"),
    ]
    return _top(_group(hits, by^, specs^), ["c"], [True], 10)


def q10(hits: DataFrame) raises -> DataFrame:
    """Counts distinct users per phone model.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["MobilePhoneModel", "UserID"]
    var kept = _keep(hits, want, _nonempty(hits, "MobilePhoneModel"))
    var by: List[String] = ["MobilePhoneModel"]
    var specs: List[AggSpec] = [AggSpec("UserID", AggKind.NUNIQUE, "u")]
    return _top(_group(kept, by^, specs^), ["u"], [True], 10)


def q11(hits: DataFrame) raises -> DataFrame:
    """The same, keyed on the phone and the model together.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["MobilePhone", "MobilePhoneModel", "UserID"]
    var kept = _keep(hits, want, _nonempty(hits, "MobilePhoneModel"))
    var by: List[String] = ["MobilePhone", "MobilePhoneModel"]
    var specs: List[AggSpec] = [AggSpec("UserID", AggKind.NUNIQUE, "u")]
    return _top(_group(kept, by^, specs^), ["u"], [True], 10)


def q12(hits: DataFrame) raises -> DataFrame:
    """Counts rows per search phrase and takes the ten commonest.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["SearchPhrase"]
    var kept = _keep(hits, want, _nonempty(hits, "SearchPhrase"))
    var by: List[String] = ["SearchPhrase"]
    var specs: List[AggSpec] = [_size("SearchPhrase", "c")]
    return _top(_group(kept, by^, specs^), ["c"], [True], 10)


def q13(hits: DataFrame) raises -> DataFrame:
    """Counts distinct users per search phrase, which is millions of groups.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["SearchPhrase", "UserID"]
    var kept = _keep(hits, want, _nonempty(hits, "SearchPhrase"))
    var by: List[String] = ["SearchPhrase"]
    var specs: List[AggSpec] = [AggSpec("UserID", AggKind.NUNIQUE, "u")]
    return _top(_group(kept, by^, specs^), ["u"], [True], 10)


def q14(hits: DataFrame) raises -> DataFrame:
    """Counts rows per engine and phrase.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["SearchEngineID", "SearchPhrase"]
    var kept = _keep(hits, want, _nonempty(hits, "SearchPhrase"))
    var by: List[String] = ["SearchEngineID", "SearchPhrase"]
    var specs: List[AggSpec] = [_size("SearchPhrase", "c")]
    return _top(_group(kept, by^, specs^), ["c"], [True], 10)


def q15(hits: DataFrame) raises -> DataFrame:
    """Counts rows per user, which is the suite's high cardinality group by.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var by: List[String] = ["UserID"]
    var specs: List[AggSpec] = [_size("UserID", "count_star()")]
    return _top(_group(hits, by^, specs^), ["count_star()"], [True], 10)


def q16(hits: DataFrame) raises -> DataFrame:
    """Counts rows per user and phrase, ordered.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var by: List[String] = ["UserID", "SearchPhrase"]
    var specs: List[AggSpec] = [_size("UserID", "count_star()")]
    return _top(_group(hits, by^, specs^), ["count_star()"], [True], 10)


def q17(hits: DataFrame) raises -> DataFrame:
    """The same grouping with no order under the limit.

    Ten rows out of millions and the statement does not say which ten, so this is
    the one query here whose answer is not a contract. It is still a real
    measurement, because the grouping is the work and the limit is not.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var by: List[String] = ["UserID", "SearchPhrase"]
    var specs: List[AggSpec] = [_size("UserID", "count_star()")]
    return _group(hits, by^, specs^).head(10)


def q18(hits: DataFrame) raises -> DataFrame:
    """Counts rows per user, minute of the hour and phrase.

    The minute is a computed group key, which makes this and q42 the two queries
    that group by something the table does not hold.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["UserID", "EventTime", "SearchPhrase"]
    var narrow = hits.select(want)
    var minute = temporal_field(
        narrow[narrow.schema.index_of("EventTime")], TemporalField.MINUTE
    )
    narrow.add_column(Series("m", minute^))
    var by: List[String] = ["UserID", "m", "SearchPhrase"]
    var specs: List[AggSpec] = [_size("UserID", "count_star()")]
    return _top(_group(narrow, by^, specs^), ["count_star()"], [True], 10)


def q19(hits: DataFrame) raises -> DataFrame:
    """Finds one user by identifier, which is a filter and nothing else.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["UserID"]
    return _keep(
        hits,
        want,
        _cmp(hits, "UserID", BinaryOp.EQ, Value(Int64(435090932899640449))),
    )


def q20(hits: DataFrame) raises -> DataFrame:
    """Counts the URLs holding a substring.

    Args:
        hits: The table.

    Returns:
        The answer, one row.

    Raises:
        Error: As the search does.
    """
    var names: List[String] = ["count_star()"]
    var values = List[AnyArray]()
    values.append(_int64(_rows(_contains(hits, "URL", String("google")))))
    return _answer(names, values^)


def q21(hits: DataFrame) raises -> DataFrame:
    """Groups the matching URLs by phrase and takes the smallest URL of each.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["SearchPhrase", "URL"]
    var kept = _keep(
        hits,
        want,
        _both(
            _contains(hits, "URL", String("google")),
            _nonempty(hits, "SearchPhrase"),
        ),
    )
    var by: List[String] = ["SearchPhrase"]
    var specs: List[AggSpec] = [
        AggSpec("URL", AggKind.MIN, "min(URL)"),
        _size("SearchPhrase", "c"),
    ]
    return _top(_group(kept, by^, specs^), ["c"], [True], 10)


def q22(hits: DataFrame) raises -> DataFrame:
    """Two text searches, one of them negated, and five columns out.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["SearchPhrase", "URL", "Title", "UserID"]
    var titled = _contains(hits, "Title", String("Google"))
    var elsewhere = _not(_contains(hits, "URL", String(".google.")))
    var kept = _keep(
        hits,
        want,
        _both(_both(titled, elsewhere), _nonempty(hits, "SearchPhrase")),
    )
    var by: List[String] = ["SearchPhrase"]
    var specs: List[AggSpec] = [
        AggSpec("URL", AggKind.MIN, "min(URL)"),
        AggSpec("Title", AggKind.MIN, "min(Title)"),
        _size("SearchPhrase", "c"),
        AggSpec("UserID", AggKind.NUNIQUE, "count(DISTINCT UserID)"),
    ]
    return _top(_group(kept, by^, specs^), ["c"], [True], 10)


def q23(hits: DataFrame) raises -> DataFrame:
    """Takes ten whole rows, all 105 columns of them.

    The only query in the suite that asks for the table rather than for a
    reduction of it, and the width is the point: the filter keeps a small number
    of rows and every one of the 105 columns has to come with them.

    `filter_sort_limit` rather than a filter and then a limit, because the
    filtered frame in the middle is the whole cost of this query. Ninety five
    rows out of a million survive at 1M and ten of those are the answer, so
    building the filtered frame means gathering 105 columns a million rows at a
    time to read ten rows out of the result.

    Args:
        hits: The table.

    Returns:
        The answer, ten rows and 105 columns.

    Raises:
        Error: As the operations do.
    """
    return hits.filter_sort_limit(
        _contains(hits, "URL", String("google")),
        ["EventTime"],
        [False],
        [False],
        10,
    )


def q24(hits: DataFrame) raises -> DataFrame:
    """Takes the ten earliest search phrases.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["SearchPhrase", "EventTime"]
    var kept = _keep(hits, want, _nonempty(hits, "SearchPhrase"))
    var ten = _top(kept^, ["EventTime"], [False], 10)
    var out: List[String] = ["SearchPhrase"]
    return ten.select(out)


def q25(hits: DataFrame) raises -> DataFrame:
    """Takes the ten smallest search phrases, which is a sort over text.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["SearchPhrase"]
    var kept = _keep(hits, want, _nonempty(hits, "SearchPhrase"))
    return _top(kept^, ["SearchPhrase"], [False], 10)


def q26(hits: DataFrame) raises -> DataFrame:
    """The same over two keys, a timestamp and then the text.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["SearchPhrase", "EventTime"]
    var kept = _keep(hits, want, _nonempty(hits, "SearchPhrase"))
    var ten = _top(kept^, ["EventTime", "SearchPhrase"], [False, False], 10)
    var out: List[String] = ["SearchPhrase"]
    return ten.select(out)


def q27(hits: DataFrame) raises -> DataFrame:
    """Averages a URL's length per counter, with a having and a limit of 25.

    The length is bytes rather than characters, because DuckDB's `STRLEN` is
    bytes and that is the published question. On this data the two are not the
    same number either: a good part of these URLs carry Cyrillic, which is two
    bytes a letter in UTF-8.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["CounterID", "URL"]
    var kept = _keep(hits, want, _nonempty(hits, "URL"))
    var length = text_byte_length(kept[kept.schema.index_of("URL")].strings())
    kept.add_column(Series("l", length^))
    var by: List[String] = ["CounterID"]
    var specs: List[AggSpec] = [
        AggSpec("l", AggKind.MEAN, "l"),
        _size("CounterID", "c"),
    ]
    var grouped = _group(kept, by^, specs^)
    return _top(_having(grouped^, "c", 100000), ["l"], [True], 25)


def q29(hits: DataFrame) raises -> DataFrame:
    """Sums one column ninety times, each with a different constant added.

    Nothing here folds the ninety into one pass. `SUM(x + k)` is `SUM(x) + k *
    COUNT(x)` and a planner is welcome to work that out, but no engine in this
    table does it and doing it by hand in one of the four ports would be
    reporting an optimizer's work with no optimizer behind it. So this is ninety
    adds and ninety sums, which is what the statement says.

    Args:
        hits: The table.

    Returns:
        The answer, one row and ninety columns.

    Raises:
        Error: As the operations do.
    """
    var at = hits.schema.index_of("ResolutionWidth")
    var names = List[String](capacity=90)
    var values = List[AnyArray](capacity=90)
    names.append(String("sum(ResolutionWidth)"))
    values.append(reduce_any(hits[at], AggKind.SUM))
    for k in range(1, 90):
        names.append(String("sum((ResolutionWidth + ", k, "))"))
        var shifted = binary_value_any(hits[at], Value(Int16(k)), BinaryOp.ADD)
        values.append(reduce_any(shifted, AggKind.SUM))
    return _answer(names, values^)


def q30(hits: DataFrame) raises -> DataFrame:
    """Three aggregates per engine and client address.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = [
        "SearchEngineID",
        "ClientIP",
        "IsRefresh",
        "ResolutionWidth",
    ]
    var kept = _keep(hits, want, _nonempty(hits, "SearchPhrase"))
    var by: List[String] = ["SearchEngineID", "ClientIP"]
    var specs: List[AggSpec] = [
        _size("ClientIP", "c"),
        AggSpec("IsRefresh", AggKind.SUM, "sum(IsRefresh)"),
        AggSpec("ResolutionWidth", AggKind.MEAN, "avg(ResolutionWidth)"),
    ]
    return _top(_group(kept, by^, specs^), ["c"], [True], 10)


def q31(hits: DataFrame) raises -> DataFrame:
    """The same keyed on the watch identifier, which is nearly unique.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = [
        "WatchID",
        "ClientIP",
        "IsRefresh",
        "ResolutionWidth",
    ]
    var kept = _keep(hits, want, _nonempty(hits, "SearchPhrase"))
    var by: List[String] = ["WatchID", "ClientIP"]
    var specs: List[AggSpec] = [
        _size("ClientIP", "c"),
        AggSpec("IsRefresh", AggKind.SUM, "sum(IsRefresh)"),
        AggSpec("ResolutionWidth", AggKind.MEAN, "avg(ResolutionWidth)"),
    ]
    return _top(_group(kept, by^, specs^), ["c"], [True], 10)


def q32(hits: DataFrame) raises -> DataFrame:
    """The same again with no filter, so the group count is nearly the row count.

    This is the heaviest group by in the suite, and the one that says most about
    how an engine's hash table behaves when almost every row is its own group.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var by: List[String] = ["WatchID", "ClientIP"]
    var specs: List[AggSpec] = [
        _size("ClientIP", "c"),
        AggSpec("IsRefresh", AggKind.SUM, "sum(IsRefresh)"),
        AggSpec("ResolutionWidth", AggKind.MEAN, "avg(ResolutionWidth)"),
    ]
    return _top(_group(hits, by^, specs^), ["c"], [True], 10)


def q33(hits: DataFrame) raises -> DataFrame:
    """Counts rows per URL, which groups by the table's widest text column.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var by: List[String] = ["URL"]
    var specs: List[AggSpec] = [_size("URL", "c")]
    return _top(_group(hits, by^, specs^), ["c"], [True], 10)


def q34(hits: DataFrame) raises -> DataFrame:
    """The same with a constant as the first group key.

    The statement groups by a constant and so does this. Some planners remove it
    and some do not, and removing it here by hand would be doing an optimizer's
    job and then reporting the result as the optimizer's work.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["URL"]
    var narrow = hits.select(want)
    var ones = Array[DType.int32](len(narrow))
    for i in range(len(narrow)):
        ones[i] = Int32(1)
    narrow.add_column(Series("1", ones^))
    var by: List[String] = ["1", "URL"]
    var specs: List[AggSpec] = [_size("URL", "c")]
    return _top(_group(narrow, by^, specs^), ["c"], [True], 10)


def q35(hits: DataFrame) raises -> DataFrame:
    """Groups by an address and three expressions over the same address.

    Four keys that are functionally one, which is the point: an engine that
    hashes four columns does four times the work of one that notices.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var want: List[String] = ["ClientIP"]
    var narrow = hits.select(want)
    var at = narrow.schema.index_of("ClientIP")
    for k in range(1, 4):
        var shifted = binary_value_any(
            narrow[at], Value(Int32(k)), BinaryOp.SUB
        )
        narrow.add_column(Series(String("(ClientIP - ", k, ")"), shifted^))
    var by: List[String] = [
        "ClientIP",
        "(ClientIP - 1)",
        "(ClientIP - 2)",
        "(ClientIP - 3)",
    ]
    var specs: List[AggSpec] = [_size("ClientIP", "c")]
    return _top(_group(narrow, by^, specs^), ["c"], [True], 10)


def q36(hits: DataFrame) raises -> DataFrame:
    """Page views per URL inside one counter and one month.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var mask = _both(
        _july(hits),
        _both(
            _both(_zero(hits, "DontCountHits"), _zero(hits, "IsRefresh")),
            _nonempty(hits, "URL"),
        ),
    )
    var want: List[String] = ["URL"]
    var kept = _keep(hits, want, mask)
    var by: List[String] = ["URL"]
    var specs: List[AggSpec] = [_size("URL", "PageViews")]
    return _top(_group(kept, by^, specs^), ["PageViews"], [True], 10)


def q37(hits: DataFrame) raises -> DataFrame:
    """The same over titles.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var mask = _both(
        _july(hits),
        _both(
            _both(_zero(hits, "DontCountHits"), _zero(hits, "IsRefresh")),
            _nonempty(hits, "Title"),
        ),
    )
    var want: List[String] = ["Title"]
    var kept = _keep(hits, want, mask)
    var by: List[String] = ["Title"]
    var specs: List[AggSpec] = [_size("Title", "PageViews")]
    return _top(_group(kept, by^, specs^), ["PageViews"], [True], 10)


def q38(hits: DataFrame) raises -> DataFrame:
    """Page views per URL among followed links, a thousand rows in.

    The first of the five queries with an `OFFSET`. The offset goes into the
    bounded sort rather than being a slice after a full one, so a limit of ten a
    thousand rows down still never sorts the rows that cannot reach it.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var mask = _both(
        _july(hits),
        _both(
            _both(_zero(hits, "IsRefresh"), _zero(hits, "IsDownload")),
            _nonzero(hits, "IsLink"),
        ),
    )
    var want: List[String] = ["URL"]
    var kept = _keep(hits, want, mask)
    var by: List[String] = ["URL"]
    var specs: List[AggSpec] = [_size("URL", "PageViews")]
    return _top(_group(kept, by^, specs^), ["PageViews"], [True], 10, 1000)


def q39(hits: DataFrame) raises -> DataFrame:
    """Five group keys, one of them a conditional over a text column.

    The `CASE` is the reason this one is here: the key is the referer on the rows
    that came from neither a search engine nor an ad engine, and the empty string
    on the rest, so one of the five keys is computed and another is renamed.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var mask = _both(_july(hits), _zero(hits, "IsRefresh"))
    var want: List[String] = [
        "TraficSourceID",
        "SearchEngineID",
        "AdvEngineID",
        "Referer",
        "URL",
    ]
    var kept = _keep(hits, want, mask)
    var direct = _both(
        _zero(kept, "SearchEngineID"), _zero(kept, "AdvEngineID")
    )
    var referer = kept.schema.index_of("Referer")
    var src = text_pick(direct, kept[referer].strings(), _empties(len(kept)))
    kept.add_column(Series("Src", src^))
    var dst = kept.rename("URL", "Dst")
    var by: List[String] = [
        "TraficSourceID",
        "SearchEngineID",
        "AdvEngineID",
        "Src",
        "Dst",
    ]
    var specs: List[AggSpec] = [_size("Dst", "PageViews")]
    return _top(_group(dst, by^, specs^), ["PageViews"], [True], 10, 1000)


def q40(hits: DataFrame) raises -> DataFrame:
    """Page views per URL hash and day for one referer.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var sources = Array[DType.int16](2)
    sources[0] = Int16(-1)
    sources[1] = Int16(6)
    var from_one_of = is_in_any(
        hits[hits.schema.index_of("TraficSourceID")],
        Series("set", sources^).into_values(),
    )
    var mask = _both(
        _july(hits),
        _both(
            _both(_zero(hits, "IsRefresh"), from_one_of),
            _cmp(
                hits,
                "RefererHash",
                BinaryOp.EQ,
                Value(Int64(3594120000172545465)),
            ),
        ),
    )
    var want: List[String] = ["URLHash", "EventDate"]
    var kept = _keep(hits, want, mask)
    var by: List[String] = ["URLHash", "EventDate"]
    var specs: List[AggSpec] = [_size("URLHash", "PageViews")]
    return _top(_group(kept, by^, specs^), ["PageViews"], [True], 10, 100)


def q41(hits: DataFrame) raises -> DataFrame:
    """Page views per window size for one URL, ten thousand rows in.

    The deepest offset in the suite, which is what makes it worth having: the
    answer is ten rows and everything before them still has to be ordered.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var mask = _both(
        _july(hits),
        _both(
            _both(_zero(hits, "IsRefresh"), _zero(hits, "DontCountHits")),
            _cmp(
                hits,
                "URLHash",
                BinaryOp.EQ,
                Value(Int64(2868770270353813622)),
            ),
        ),
    )
    var want: List[String] = ["WindowClientWidth", "WindowClientHeight"]
    var kept = _keep(hits, want, mask)
    var by: List[String] = ["WindowClientWidth", "WindowClientHeight"]
    var specs: List[AggSpec] = [_size("WindowClientWidth", "PageViews")]
    return _top(_group(kept, by^, specs^), ["PageViews"], [True], 10, 10000)


def q42(hits: DataFrame) raises -> DataFrame:
    """Page views per minute over two days, ordered by the minute.

    The group key is a truncated timestamp, which is the other computed key in
    the suite, and the ordering is on the key rather than on the count.

    Args:
        hits: The table.

    Returns:
        The answer.

    Raises:
        Error: As the operations do.
    """
    var mask = _both(
        _counter62(hits, day(2013, 7, 14), day(2013, 7, 15)),
        _both(_zero(hits, "IsRefresh"), _zero(hits, "DontCountHits")),
    )
    var want: List[String] = ["EventTime"]
    var kept = _keep(hits, want, mask)
    var minute = temporal_round(
        kept[kept.schema.index_of("EventTime")], "min", ROUND_DOWN
    )
    kept.add_column(Series("M", minute^))
    var by: List[String] = ["M"]
    var specs: List[AggSpec] = [_size("M", "PageViews")]
    var grouped = _group(kept, by^, specs^)
    return _top(grouped^, ["M"], [False], 10, 1000)


def run_clickbench(query: String, hits: DataFrame) raises -> DataFrame:
    """Runs one ClickBench query and returns its answer.

    Args:
        query: The query name, `q0` through `q42`.
        hits: The loaded table.

    Returns:
        The answer frame.

    Raises:
        Error: If the query is not one this engine runs.
    """
    if query == "q0":
        return q0(hits)
    if query == "q1":
        return q1(hits)
    if query == "q2":
        return q2(hits)
    if query == "q3":
        return q3(hits)
    if query == "q4":
        return q4(hits)
    if query == "q5":
        return q5(hits)
    if query == "q6":
        return q6(hits)
    if query == "q7":
        return q7(hits)
    if query == "q8":
        return q8(hits)
    if query == "q9":
        return q9(hits)
    if query == "q10":
        return q10(hits)
    if query == "q11":
        return q11(hits)
    if query == "q12":
        return q12(hits)
    if query == "q13":
        return q13(hits)
    if query == "q14":
        return q14(hits)
    if query == "q15":
        return q15(hits)
    if query == "q16":
        return q16(hits)
    if query == "q17":
        return q17(hits)
    if query == "q18":
        return q18(hits)
    if query == "q19":
        return q19(hits)
    if query == "q20":
        return q20(hits)
    if query == "q21":
        return q21(hits)
    if query == "q22":
        return q22(hits)
    if query == "q23":
        return q23(hits)
    if query == "q24":
        return q24(hits)
    if query == "q25":
        return q25(hits)
    if query == "q26":
        return q26(hits)
    if query == "q27":
        return q27(hits)
    if query == "q28":
        raise Error(REGEX_REFUSAL)
    if query == "q29":
        return q29(hits)
    if query == "q30":
        return q30(hits)
    if query == "q31":
        return q31(hits)
    if query == "q32":
        return q32(hits)
    if query == "q33":
        return q33(hits)
    if query == "q34":
        return q34(hits)
    if query == "q35":
        return q35(hits)
    if query == "q36":
        return q36(hits)
    if query == "q37":
        return q37(hits)
    if query == "q38":
        return q38(hits)
    if query == "q39":
        return q39(hits)
    if query == "q40":
        return q40(hits)
    if query == "q41":
        return q41(hits)
    if query == "q42":
        return q42(hits)
    raise Error("clickbench: no query called " + query)


def clickbench_supported() -> List[String]:
    """Returns the queries this module answers.

    Returns:
        The query names, in the order ClickBench publishes them.
    """
    var out = List[String](capacity=QUERY_COUNT)
    for i in range(QUERY_COUNT):
        if i != 28:
            out.append(String("q", i))
    return out^


def clickbench_refused() -> List[String]:
    """Returns the queries this module refuses, each followed by its reason.

    Read as pairs, because the harness asks the driver for this list rather than
    keeping a second copy of it. A copy is what goes stale: the driver learns a
    query, nobody edits the Python, and the table goes on reporting a gap that
    closed two releases ago.

    Returns:
        The name and reason of each refusal.
    """
    var out = List[String](capacity=2)
    out.append(String("q28"))
    out.append(REGEX_REFUSAL)
    return out^
