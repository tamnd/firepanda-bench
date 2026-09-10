"""The twenty two TPC-H queries in firepanda, written against the frame API.

These are the official queries expressed as dataframe operations rather than
SQL, which is the only way a dataframe library can be in a TPC-H table at all.
The substitution parameters are the specification's validation values, the same
ones DuckDB's `tpch_queries()` carries and the same ones the Polars and pandas
versions in `tools/engines` use, so all four engines answer the same question.

Two things about the loading are worth stating plainly, because both of them
move numbers and a benchmark that hides either is not worth reading.

The first is that the tables are read before the clock starts. That is not a
special favour: in `--io memory`, which is the harness default, Polars and
pandas both read every column of every table the query needs in their `load` as
well, and DuckDB is handed registered Arrow views. What `--io scan` measures is
the other thing, where the read is inside the timed region, and firepanda has no
Parquet reader of its own yet, so it does not appear in that table. It says so
there rather than here.

The second is the money columns. TPC-H says they are DECIMAL(15,2) and DuckDB
and Polars both carry that through the whole query. firepanda has no decimal
column, so it gets float64 prices, cast on the way in. pandas is in the same
position for its own reasons and does the same thing. It is worth being clear
which way the bias runs: float multiplication is faster than decimal
multiplication, so this makes firepanda and pandas both look better than a
decimal version of themselves would, against two engines that are doing the
harder arithmetic. The report says so beside the table.

Dates arrive as they are, a day count since the epoch, which is what Arrow calls
date32 and what Polars and DuckDB both hold. The literals below are therefore
day numbers, and `day` is where each one is written out with the date it means.
"""

from firepanda.array.any import AnyArray
from firepanda.array.array import Array
from firepanda.array.strings import strings_from_list
from firepanda.array.value import Value
from firepanda.dtype import Field, LogicalType, Schema
from firepanda.frame.frame import DataFrame
from firepanda.frame.groupby import AggKind, AggSpec
from firepanda.frame.series import Series
from firepanda.io.parquet import Session
from firepanda.join import JoinKind
from firepanda.kernel import (
    filter_any,
    is_in_any,
    logical_and,
    logical_or,
    reduce_any,
)
from firepanda.kernel.binary import BinaryOp, binary_any, binary_value_any
from firepanda.kernel.pattern import text_contains
from firepanda.kernel.temporal import TemporalField, temporal_field

comptime TABLE_COUNT = 8
"""How many tables TPC-H has, which is how many paths the driver passes."""


def table_names() -> List[String]:
    """Returns the eight table names, in the order the driver passes paths.

    Returns:
        The names.
    """
    var out = List[String](capacity=TABLE_COUNT)
    out.append(String("customer"))
    out.append(String("lineitem"))
    out.append(String("nation"))
    out.append(String("orders"))
    out.append(String("part"))
    out.append(String("partsupp"))
    out.append(String("region"))
    out.append(String("supplier"))
    return out^


def day(year: Int, month: Int, dom: Int) -> Value:
    """Builds a date scalar from a calendar date.

    The days from the civil calendar, by Howard Hinnant's algorithm, which is
    the one everybody uses because it is branch free and correct for every
    proleptic Gregorian date rather than only the ones after 1970.

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


def _money(table: String) -> String:
    """Returns the DECIMAL columns of a table, so the load can cast them.

    Args:
        table: The table name.

    Returns:
        A `REPLACE` clause for the select star, or empty when the table has no
        decimal column.
    """
    var columns = List[String]()
    if table == "customer":
        columns.append("c_acctbal")
    elif table == "lineitem":
        columns.append("l_quantity")
        columns.append("l_extendedprice")
        columns.append("l_discount")
        columns.append("l_tax")
    elif table == "orders":
        columns.append("o_totalprice")
    elif table == "part":
        columns.append("p_retailprice")
    elif table == "partsupp":
        columns.append("ps_supplycost")
    elif table == "supplier":
        columns.append("s_acctbal")
    if len(columns) == 0:
        return String()
    var out = String(" REPLACE (")
    for i in range(len(columns)):
        if i != 0:
            out += ", "
        out += String(columns[i], "::DOUBLE AS ", columns[i])
    out += ")"
    return out^


struct Tpch(Movable):
    """The eight tables, whichever of them a query needs.

    A table a query does not read is an empty frame rather than a missing entry.
    The harness passes only the paths the query's `needs` names, so the ones that
    stay empty are the ones nobody was going to touch.
    """

    var customer: DataFrame
    """The customer table."""

    var lineitem: DataFrame
    """The lineitem table."""

    var nation: DataFrame
    """The nation table."""

    var orders: DataFrame
    """The orders table."""

    var part: DataFrame
    """The part table."""

    var partsupp: DataFrame
    """The partsupp table."""

    var region: DataFrame
    """The region table."""

    var supplier: DataFrame
    """The supplier table."""

    def __init__(out self):
        """Constructs a set with every table empty."""
        self.customer = DataFrame()
        self.lineitem = DataFrame()
        self.nation = DataFrame()
        self.orders = DataFrame()
        self.part = DataFrame()
        self.partsupp = DataFrame()
        self.region = DataFrame()
        self.supplier = DataFrame()


def load_tpch(paths: List[String]) raises -> Tpch:
    """Reads the tables whose paths were given.

    Args:
        paths: One entry per name in `table_names`, in that order, empty for a
            table this query does not read.

    Returns:
        The loaded tables.

    Raises:
        If the list is the wrong length, or a file cannot be read.
    """
    var names = table_names()
    if len(paths) != len(names):
        raise Error(
            "tpch: expected "
            + String(len(names))
            + " table paths and got "
            + String(len(paths))
        )
    var session = Session()
    var out = Tpch()
    for i in range(len(names)):
        if paths[i].byte_length() == 0:
            continue
        ref name = names[i]
        var frame = session.run(
            String(
                "SELECT *",
                _money(name),
                " FROM read_parquet('",
                paths[i],
                "')",
            )
        )
        if name == "customer":
            out.customer = frame^
        elif name == "lineitem":
            out.lineitem = frame^
        elif name == "nation":
            out.nation = frame^
        elif name == "orders":
            out.orders = frame^
        elif name == "part":
            out.part = frame^
        elif name == "partsupp":
            out.partsupp = frame^
        elif name == "region":
            out.region = frame^
        else:
            out.supplier = frame^
    return out^


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
        As `logical_and` does.
    """
    return logical_and(a, b)


def _either(
    a: Array[DType.bool], b: Array[DType.bool]
) raises -> Array[DType.bool]:
    """Ors two masks together.

    Args:
        a: The first mask.
        b: The second, of the same length.

    Returns:
        The rows either is true on.

    Raises:
        As `logical_or` does.
    """
    return logical_or(a, b)


def _mask(var series: Series) raises -> Array[DType.bool]:
    """Reads a comparison's answer as a mask.

    Args:
        series: The comparison result.

    Returns:
        The mask.

    Raises:
        If the series is not a boolean column.
    """
    return series.as_typed[DType.bool]()


def _cmp(
    frame: DataFrame, name: String, op: BinaryOp, value: Value
) raises -> Array[DType.bool]:
    """Compares one column against a constant without copying the column.

    `DataFrame.column` copies and flattens, which for `l_discount` at sf1 is
    forty eight megabytes moved to answer a question that only reads it.
    `__getitem__` on a position borrows instead, so this looks the name up once
    and hands the borrowed column straight to the kernel. On q6, whose five
    predicates read three columns, that is thirty eight milliseconds down to
    twenty three.

    Args:
        frame: The frame.
        name: The column to compare.
        op: The comparison.
        value: The constant to compare against.

    Returns:
        The mask.

    Raises:
        If the column is missing, or as the comparison does.
    """
    var at = frame.schema.index_of(name)
    return binary_value_any(frame[at], value, op).as_typed[DType.bool]()


def _in(
    frame: DataFrame, name: String, values: List[String]
) raises -> Array[DType.bool]:
    """Looks one column up in a set of strings without copying the column.

    The same argument as `_cmp` and a bigger number behind it, because a text
    column is sixteen bytes of view a row before any of the bytes. On q19,
    whose set is the two air shipping modes, reading `l_shipmode` through
    `DataFrame.column` is thirteen milliseconds at sf1 and reading it through
    the borrow is two. The lookup itself is the two milliseconds; the other
    eleven are a ninety six megabyte copy made to answer a question that only
    reads it.

    Args:
        frame: The frame.
        name: The column to look up.
        values: The set.

    Returns:
        The mask.

    Raises:
        If the column is missing, or as the lookup does.
    """
    var at = frame.schema.index_of(name)
    return is_in_any(frame[at], _texts(values).into_values())


def _year(frame: DataFrame, name: String, into: String) raises -> Series:
    """Reads the year out of a date column without copying the column.

    Args:
        frame: The frame.
        name: The date column.
        into: The name the answer should carry.

    Returns:
        The year as a series.

    Raises:
        If the column is missing, or as the field read does.
    """
    var at = frame.schema.index_of(name)
    return Series(into, temporal_field(frame[at], TemporalField.YEAR))


def _product(
    frame: DataFrame, left: String, right: String, into: String
) raises -> Series:
    """Multiplies two columns of a frame without copying either.

    Args:
        frame: The frame.
        left: The left column.
        right: The right column.
        into: The name the answer should carry.

    Returns:
        The product as a series.

    Raises:
        If either column is missing, or as the multiplication does.
    """
    var a = frame.schema.index_of(left)
    var b = frame.schema.index_of(right)
    return Series(into, binary_any(frame[a], frame[b], BinaryOp.MUL))


def _contains(
    frame: DataFrame, name: String, needle: String
) raises -> Array[DType.bool]:
    """Returns a mask of the rows whose text holds a substring, borrowing.

    Args:
        frame: The frame.
        name: The text column.
        needle: The substring.

    Returns:
        The mask.

    Raises:
        If the column is missing or is not text.
    """
    var at = frame.schema.index_of(name)
    return text_contains(frame[at].strings(), needle.as_bytes())


def _cmp2(
    frame: DataFrame, left: String, right: String, op: BinaryOp
) raises -> Array[DType.bool]:
    """Compares two of a frame's columns against each other, borrowing both.

    Args:
        frame: The frame.
        left: The column on the left of the comparison.
        right: The column on the right.
        op: The comparison.

    Returns:
        The mask.

    Raises:
        If either column is missing, or as the comparison does.
    """
    var a = frame.schema.index_of(left)
    var b = frame.schema.index_of(right)
    return binary_any(frame[a], frame[b], op).as_typed[DType.bool]()


def _keep(
    frame: DataFrame, names: List[String], mask: Array[DType.bool]
) raises -> DataFrame:
    """Filters a frame down to the rows a mask keeps and the columns named.

    This is projection pushdown done by hand. `DataFrame.filter` keeps every
    column, and `lineitem` has sixteen of them including three text columns that
    no query in the set reads, so filtering the whole frame moves several
    hundred megabytes to produce an answer that needs two columns. DuckDB and
    polars both work this out for themselves from the query; firepanda has no
    planner yet, so the driver names the columns instead. On q6 that is two
    hundred and ten milliseconds down to eighty one.

    Args:
        frame: The frame to filter.
        names: The columns to keep, in the order the result should have them.
        mask: The rows to keep.

    Returns:
        A frame of the kept rows and the named columns.

    Raises:
        If a name is missing, or as the filter does.
    """
    var fields = List[Field](capacity=len(names))
    var columns = List[AnyArray](capacity=len(names))
    for i in range(len(names)):
        var at = frame.schema.index_of(names[i])
        columns.append(filter_any(frame[at], mask))
        fields.append(Field(names[i], columns[i].type))
    return DataFrame(Schema(fields^), columns^)


def _not(var mask: Array[DType.bool]) raises -> Array[DType.bool]:
    """Negates a mask.

    Args:
        mask: The mask.

    Returns:
        The rows it was false on.

    Raises:
        As the inversion does.
    """
    return (~Series("m", mask^)).as_typed[DType.bool]()


def _sorted(
    var frame: DataFrame, by: List[String], descending: List[Bool]
) raises -> DataFrame:
    """Sorts a frame, nulls last, which is what every TPC-H order by wants.

    Args:
        frame: The frame.
        by: The key columns, most significant first.
        descending: One flag per key.

    Returns:
        The sorted frame.

    Raises:
        As `sort_values` does.
    """
    var nulls = List[Bool](capacity=len(by))
    for _ in range(len(by)):
        nulls.append(False)
    return frame.sort_values(by, descending, nulls^)


def _texts(values: List[String]) raises -> Series:
    """Builds a text series to use as an `IN` set.

    Args:
        values: The members.

    Returns:
        The set as a one column series.

    Raises:
        As building the strings does.
    """
    return Series("set", strings_from_list(values))


def _int32s(values: List[Int]) -> Series:
    """Builds an int32 series to use as an `IN` set.

    The width matters: `is_in` refuses a set of a different type rather than
    casting it, and the TPC-H integer columns are int32.

    Args:
        values: The members.

    Returns:
        The set as a one column series.
    """
    var out = Array[DType.int32](len(values))
    for i in range(len(values)):
        out[i] = Int32(values[i])
    return Series("set", out^)


def _zeros(rows: Int) -> Series:
    """Builds a float column of zeros, which is the else side of a case when.

    Args:
        rows: How tall.

    Returns:
        The column.
    """
    return Series("zero", Array[DType.float64](rows))


def _flags(var mask: Array[DType.bool], name: String) -> Series:
    """Turns a mask into the one and zero column a conditional count sums.

    Args:
        mask: The condition.
        name: What to call the result.

    Returns:
        An int64 column, one where the mask held.
    """
    var out = Array[DType.int64](len(mask))
    for i in range(len(mask)):
        out[i] = Int64(1) if mask[i] else Int64(0)
    return Series(name, out^)


def _reduced(series: Series, kind: AggKind) raises -> Float64:
    """Reduces a float column to the single number a scalar subquery wants.

    Three queries compare every row against one number computed over the whole
    of something: q11's threshold, q15's best revenue and q22's average balance.
    Polars writes those as a cross join against a one row frame, which is the
    same number arriving through a join. This reads it out instead, because a
    cross join is not an operation this library has and a scalar is not one it
    needs.

    Args:
        series: The column, which must be float64.
        kind: Which reduction.

    Returns:
        The number.

    Raises:
        If the column is not float64, or as the reduction does.
    """
    var one = reduce_any(series.values, kind)
    var typed = one.as_typed[DType.float64]()
    return typed[0]


def _one(name: String, value: Float64) raises -> DataFrame:
    """Builds the one row, one column frame a scalar answer is.

    Args:
        name: The column name.
        value: The number.

    Returns:
        The frame.

    Raises:
        As the frame constructor does.
    """
    var column = Array[DType.float64](1)
    column[0] = value
    var fields = List[Field](capacity=1)
    fields.append(Field(name, LogicalType.FLOAT64))
    var columns = List[AnyArray](capacity=1)
    columns.append(AnyArray(column^))
    return DataFrame(Schema(fields^), columns^)


def _discounted(frame: DataFrame, name: String) raises -> Series:
    """Builds the extended price after discount, which twelve queries want.

    Args:
        frame: A frame carrying `l_extendedprice` and `l_discount`.
        name: What to call the result.

    Returns:
        The column.

    Raises:
        If either column is missing.
    """
    # Borrowed rather than copied, for the reason `_cmp` records. Copying both
    # columns and both intermediates is six passes over forty eight megabytes
    # each where four will do, and on q1 that is sixty four milliseconds down to
    # twenty eight.
    var d = frame.schema.index_of("l_discount")
    var e = frame.schema.index_of("l_extendedprice")
    var factor = binary_value_any(
        frame[d], Value(Float64(1.0)), BinaryOp.SUB, True
    )
    return Series(name, binary_any(frame[e], factor, BinaryOp.MUL))


def q1(ref tables: Tpch) raises -> DataFrame:
    """Pricing Summary Report.

    One table, one filter, one group by of eight aggregates. Two of those
    aggregates are over expressions rather than columns, so the expressions are
    built as columns first, which is what a planner would do with them too.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var want: List[String] = [
        "l_returnflag",
        "l_linestatus",
        "l_quantity",
        "l_extendedprice",
        "l_discount",
        "l_tax",
    ]
    var kept = _keep(
        tables.lineitem,
        want,
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.LE, day(1998, 9, 2)),
    )
    var disc_price = _discounted(kept, "disc_price")
    var tax = kept.schema.index_of("l_tax")
    var charge = Series(
        "charge",
        binary_any(
            disc_price.values,
            binary_value_any(kept[tax], Value(Float64(1.0)), BinaryOp.ADD),
            BinaryOp.MUL,
        ),
    )
    # In place, because `kept` is this query's own frame and nothing else reads
    # it. Two `with_column` calls would be two deep copies of six columns of
    # five point nine million rows to write two new ones.
    var wide = kept^
    wide.add_column(disc_price^)
    wide.add_column(charge^)
    var by: List[String] = ["l_returnflag", "l_linestatus"]
    var specs: List[AggSpec] = [
        AggSpec("l_quantity", AggKind.SUM, "sum_qty"),
        AggSpec("l_extendedprice", AggKind.SUM, "sum_base_price"),
        AggSpec("disc_price", AggKind.SUM, "sum_disc_price"),
        AggSpec("charge", AggKind.SUM, "sum_charge"),
        AggSpec("l_quantity", AggKind.MEAN, "avg_qty"),
        AggSpec("l_extendedprice", AggKind.MEAN, "avg_price"),
        AggSpec("l_discount", AggKind.MEAN, "avg_disc"),
        AggSpec("l_quantity", AggKind.SIZE, "count_order"),
    ]
    return wide.group_by(by^, specs^, True, True)


def q2(ref tables: Tpch) raises -> DataFrame:
    """Minimum Cost Supplier.

    The correlated subquery becomes a group by on part key and a join back
    against the same intermediate, which is what a planner does to it too.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var region_key: List[String] = ["r_regionkey"]
    var nation_key: List[String] = ["n_regionkey"]
    var europe = tables.region.filter(
        _cmp(tables.region, "r_name", BinaryOp.EQ, Value(String("EUROPE")))
    ).join_on(tables.nation, region_key^, nation_key^)
    var nation_id: List[String] = ["n_nationkey"]
    var supplier_nation: List[String] = ["s_nationkey"]
    europe = europe.join_on(tables.supplier, nation_id^, supplier_nation^)
    var supplier_id: List[String] = ["s_suppkey"]
    var partsupp_supplier: List[String] = ["ps_suppkey"]
    europe = europe.join_on(tables.partsupp, supplier_id^, partsupp_supplier^)

    var sized = _cmp(tables.part, "p_size", BinaryOp.EQ, Value(Int32(15)))
    var brass = tables.part.column("p_type").str_ends_with("BRASS")
    var wanted = tables.part.filter(_both(sized, brass))

    var partsupp_part: List[String] = ["ps_partkey"]
    var part_id: List[String] = ["p_partkey"]
    var joined = europe.join_on(wanted, partsupp_part^, part_id^)

    var by: List[String] = ["ps_partkey"]
    var specs: List[AggSpec] = [
        AggSpec("ps_supplycost", AggKind.MIN, "ps_supplycost")
    ]
    var cheapest = joined.group_by(by^, specs^, True, False)
    var pair: List[String] = ["ps_partkey", "ps_supplycost"]
    var best = joined.join(cheapest, pair^)

    var wantedcols: List[String] = [
        "s_acctbal",
        "s_name",
        "n_name",
        "p_partkey",
        "p_mfgr",
        "s_address",
        "s_phone",
        "s_comment",
    ]
    var order: List[String] = ["s_acctbal", "n_name", "s_name", "p_partkey"]
    var descending: List[Bool] = [True, False, False, False]
    return _sorted(best.select(wantedcols^), order^, descending^).head(100)


def q3(ref tables: Tpch) raises -> DataFrame:
    """Shipping Priority.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var cutoff = day(1995, 3, 15)
    var customer_want: List[String] = ["c_custkey"]
    var building = _keep(
        tables.customer,
        customer_want,
        _cmp(
            tables.customer,
            "c_mktsegment",
            BinaryOp.EQ,
            Value(String("BUILDING")),
        ),
    )
    var order_want: List[String] = [
        "o_orderkey",
        "o_custkey",
        "o_orderdate",
        "o_shippriority",
    ]
    var early = _keep(
        tables.orders,
        order_want,
        _cmp(tables.orders, "o_orderdate", BinaryOp.LT, cutoff),
    )
    var custkey: List[String] = ["c_custkey"]
    var ordercust: List[String] = ["o_custkey"]
    var placed = building.join_on(early, custkey^, ordercust^)
    var line_want: List[String] = [
        "l_orderkey",
        "l_extendedprice",
        "l_discount",
    ]
    var late_lines = _keep(
        tables.lineitem,
        line_want,
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.GT, cutoff),
    )
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var lines = placed.join_on(late_lines, orderkey^, lineorder^)

    var revenue = _discounted(lines, "revenue")
    var wide = lines^
    wide.add_column(revenue^)
    var by: List[String] = ["o_orderkey", "o_orderdate", "o_shippriority"]
    var specs: List[AggSpec] = [AggSpec("revenue", AggKind.SUM, "revenue")]
    var grouped = wide.group_by(by^, specs^, True, False)
    var renamed = grouped.rename("o_orderkey", "l_orderkey")
    var wantedcols: List[String] = [
        "l_orderkey",
        "revenue",
        "o_orderdate",
        "o_shippriority",
    ]
    var order: List[String] = ["revenue", "o_orderdate"]
    var descending: List[Bool] = [True, False]
    return _sorted(renamed.select(wantedcols^), order^, descending^).head(10)


def q4(ref tables: Tpch) raises -> DataFrame:
    """Order Priority Checking.

    The `exists` subquery is a semi join, which is what it is.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var late_want: List[String] = ["l_orderkey"]
    var late = _keep(
        tables.lineitem,
        late_want,
        _cmp2(tables.lineitem, "l_commitdate", "l_receiptdate", BinaryOp.LT),
    )
    var quarter = _both(
        _cmp(tables.orders, "o_orderdate", BinaryOp.GE, day(1993, 7, 1)),
        _cmp(tables.orders, "o_orderdate", BinaryOp.LT, day(1993, 10, 1)),
    )
    var placed_want: List[String] = ["o_orderkey", "o_orderpriority"]
    var placed = _keep(tables.orders, placed_want, quarter)
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var matched = placed.join_on(late, orderkey^, lineorder^, JoinKind.SEMI)
    var by: List[String] = ["o_orderpriority"]
    var specs: List[AggSpec] = [
        AggSpec("o_orderpriority", AggKind.SIZE, "order_count")
    ]
    return matched.group_by(by^, specs^, True, True)


def q5(ref tables: Tpch) raises -> DataFrame:
    """Local Supplier Volume.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var region_key: List[String] = ["r_regionkey"]
    var nation_region: List[String] = ["n_regionkey"]
    var asia = tables.region.filter(
        _cmp(tables.region, "r_name", BinaryOp.EQ, Value(String("ASIA")))
    ).join_on(tables.nation, region_key^, nation_region^)
    var customer_want: List[String] = ["c_custkey", "c_nationkey"]
    var buyers = tables.customer.select(customer_want^)
    var nation_id: List[String] = ["n_nationkey"]
    var cust_nation: List[String] = ["c_nationkey"]
    var here = asia.join_on(buyers, nation_id^, cust_nation^)
    var year = _both(
        _cmp(tables.orders, "o_orderdate", BinaryOp.GE, day(1994, 1, 1)),
        _cmp(tables.orders, "o_orderdate", BinaryOp.LT, day(1995, 1, 1)),
    )
    var order_want: List[String] = ["o_orderkey", "o_custkey"]
    var early = _keep(tables.orders, order_want, year)
    var custkey: List[String] = ["c_custkey"]
    var ordercust: List[String] = ["o_custkey"]
    var placed = here.join_on(early, custkey^, ordercust^)
    var line_want: List[String] = [
        "l_orderkey",
        "l_suppkey",
        "l_extendedprice",
        "l_discount",
    ]
    var sold = tables.lineitem.select(line_want^)
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var lines = placed.join_on(sold, orderkey^, lineorder^)
    var supplier_want: List[String] = ["s_suppkey", "s_nationkey"]
    var sellers = tables.supplier.select(supplier_want^)
    var left_pair: List[String] = ["l_suppkey", "n_nationkey"]
    var right_pair: List[String] = ["s_suppkey", "s_nationkey"]
    var local = lines.join_on(sellers, left_pair^, right_pair^)

    var revenue = _discounted(local, "revenue")
    var wide = local^
    wide.add_column(revenue^)
    var by: List[String] = ["n_name"]
    var specs: List[AggSpec] = [AggSpec("revenue", AggKind.SUM, "revenue")]
    var grouped = wide.group_by(by^, specs^, True, False)
    var order: List[String] = ["revenue"]
    var descending: List[Bool] = [True]
    return _sorted(grouped^, order^, descending^)


def q6(ref tables: Tpch) raises -> DataFrame:
    """Forecasting Revenue Change.

    One table, three predicates and one sum.

    Args:
        tables: The loaded tables.

    Returns:
        The answer, one row.

    Raises:
        As the operations it runs do.
    """
    ref lineitem = tables.lineitem
    var shipped = _both(
        _cmp(lineitem, "l_shipdate", BinaryOp.GE, day(1994, 1, 1)),
        _cmp(lineitem, "l_shipdate", BinaryOp.LT, day(1995, 1, 1)),
    )
    var discounted = _both(
        _cmp(lineitem, "l_discount", BinaryOp.GE, Value(Float64(0.05))),
        _cmp(lineitem, "l_discount", BinaryOp.LE, Value(Float64(0.07))),
    )
    var small = _cmp(lineitem, "l_quantity", BinaryOp.LT, Value(Float64(24.0)))
    var want: List[String] = ["l_extendedprice", "l_discount"]
    var kept = _keep(
        lineitem, want, _both(_both(shipped, discounted), small)
    )
    var revenue = _product(kept, "l_extendedprice", "l_discount", "revenue")
    return _one("revenue", _reduced(revenue, AggKind.SUM))


def q7(ref tables: Tpch) raises -> DataFrame:
    """Volume Shipping.

    The disjunctive nation pair predicate becomes two joins against the same two
    nation frame and a filter that the two sides disagree, rather than a filter
    over the full cross product, which is the shape the query is trying to
    avoid.

    The two nation joins happen on `supplier` and `customer` before either one
    meets `lineitem`, which is the whole cost of this query. Two nations out of
    twenty five is eight per cent of the suppliers, so joining the shipping
    window to that subset first drops four and a half million rows to a third of
    a million, and everything after it is that much smaller. Written the other
    way round, with the full supplier and customer tables joined in and the
    nation filter applied at the end, it is a hundred and fifty five
    milliseconds at sf1 against seventy two for this.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var pair: List[String] = ["FRANCE", "GERMANY"]
    var two = tables.nation.filter(
        tables.nation.column("n_name").is_in(_texts(pair^))
    )
    var keep: List[String] = ["n_nationkey", "n_name"]
    var supp = two.select(keep.copy())
    supp = supp.rename("n_nationkey", "supp_nationkey")
    supp = supp.rename("n_name", "supp_nation")
    var cust = two.select(keep^)
    cust = cust.rename("n_nationkey", "cust_nationkey")
    cust = cust.rename("n_name", "cust_nation")

    var supplier_want: List[String] = ["s_suppkey", "s_nationkey"]
    var sellers = tables.supplier.select(supplier_want^)
    var supp_nation_key: List[String] = ["s_nationkey"]
    var supp_key: List[String] = ["supp_nationkey"]
    sellers = sellers.join_on(supp, supp_nation_key^, supp_key^)
    var seller_keep: List[String] = ["s_suppkey", "supp_nation"]
    sellers = sellers.select(seller_keep^)

    var customer_want: List[String] = ["c_custkey", "c_nationkey"]
    var buyers = tables.customer.select(customer_want^)
    var cust_nation_key: List[String] = ["c_nationkey"]
    var cust_key: List[String] = ["cust_nationkey"]
    buyers = buyers.join_on(cust, cust_nation_key^, cust_key^)
    var buyer_keep: List[String] = ["c_custkey", "cust_nation"]
    buyers = buyers.select(buyer_keep^)

    var window = _both(
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.GE, day(1995, 1, 1)),
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.LE, day(1996, 12, 31)),
    )
    var line_want: List[String] = [
        "l_orderkey",
        "l_suppkey",
        "l_shipdate",
        "l_extendedprice",
        "l_discount",
    ]
    var lines = _keep(tables.lineitem, line_want, window)
    var suppkey: List[String] = ["l_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    var shipping = lines.join_on(sellers, suppkey^, supplier_id^)
    var order_want: List[String] = ["o_orderkey", "o_custkey"]
    var placed = tables.orders.select(order_want^)
    var lineorder: List[String] = ["l_orderkey"]
    var orderkey: List[String] = ["o_orderkey"]
    shipping = shipping.join_on(placed, lineorder^, orderkey^)
    var ordercust: List[String] = ["o_custkey"]
    var custkey: List[String] = ["c_custkey"]
    shipping = shipping.join_on(buyers, ordercust^, custkey^)
    shipping = shipping.filter(
        _cmp2(shipping, "supp_nation", "cust_nation", BinaryOp.NE)
    )

    var year = _year(shipping, "l_shipdate", "l_year")
    var volume = _discounted(shipping, "volume")
    var wide = shipping^
    wide.add_column(year^)
    wide.add_column(volume^)
    var by: List[String] = ["supp_nation", "cust_nation", "l_year"]
    var specs: List[AggSpec] = [AggSpec("volume", AggKind.SUM, "revenue")]
    return wide.group_by(by^, specs^, True, True)


def q8(ref tables: Tpch) raises -> DataFrame:
    """National Market Share.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var region_key: List[String] = ["r_regionkey"]
    var nation_region: List[String] = ["n_regionkey"]
    var keep: List[String] = ["n_nationkey"]
    var america = (
        tables.region.filter(
            _cmp(tables.region, "r_name", BinaryOp.EQ, Value(String("AMERICA")))
        )
        .join_on(tables.nation, region_key^, nation_region^)
        .select(keep^)
    )
    america = america.rename("n_nationkey", "am_nationkey")

    var nation_keep: List[String] = ["n_nationkey", "n_name"]
    var supplier_nation = tables.nation.select(nation_keep^)
    supplier_nation = supplier_nation.rename("n_nationkey", "sn_nationkey")
    supplier_nation = supplier_nation.rename("n_name", "nation")

    var part_want: List[String] = ["p_partkey"]
    var steel = _keep(
        tables.part,
        part_want,
        _cmp(
            tables.part,
            "p_type",
            BinaryOp.EQ,
            Value(String("ECONOMY ANODIZED STEEL")),
        ),
    )
    var line_want: List[String] = [
        "l_orderkey",
        "l_partkey",
        "l_suppkey",
        "l_extendedprice",
        "l_discount",
    ]
    var sold = tables.lineitem.select(line_want^)
    var partkey: List[String] = ["p_partkey"]
    var linepart: List[String] = ["l_partkey"]
    var lines = steel.join_on(sold, partkey^, linepart^)
    # The window reads only orders, so it runs on the base table rather than on
    # the join output that used to carry it.
    var window = _both(
        _cmp(tables.orders, "o_orderdate", BinaryOp.GE, day(1995, 1, 1)),
        _cmp(tables.orders, "o_orderdate", BinaryOp.LE, day(1996, 12, 31)),
    )
    var order_want: List[String] = ["o_orderkey", "o_custkey", "o_orderdate"]
    var within = _keep(tables.orders, order_want, window)
    var lineorder: List[String] = ["l_orderkey"]
    var orderkey: List[String] = ["o_orderkey"]
    var placed = lines.join_on(within, lineorder^, orderkey^)
    var customer_want: List[String] = ["c_custkey", "c_nationkey"]
    var buyers = tables.customer.select(customer_want^)
    var ordercust: List[String] = ["o_custkey"]
    var custkey: List[String] = ["c_custkey"]
    placed = placed.join_on(buyers, ordercust^, custkey^)
    var cust_nation: List[String] = ["c_nationkey"]
    var am_key: List[String] = ["am_nationkey"]
    placed = placed.join_on(america, cust_nation^, am_key^)
    var supplier_want: List[String] = ["s_suppkey", "s_nationkey"]
    var sellers = tables.supplier.select(supplier_want^)
    var suppkey: List[String] = ["l_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    placed = placed.join_on(sellers, suppkey^, supplier_id^)
    var supp_nation: List[String] = ["s_nationkey"]
    var sn_key: List[String] = ["sn_nationkey"]
    placed = placed.join_on(supplier_nation, supp_nation^, sn_key^)

    var year = _year(placed, "o_orderdate", "o_year")
    var volume = _discounted(placed, "volume")
    var brazil = _cmp(placed, "nation", BinaryOp.EQ, Value(String("BRAZIL")))
    var only_brazil = volume.pick(brazil, _zeros(placed.rows)).rename(
        "brazil_volume"
    )
    var wide = placed^
    wide.add_column(year^)
    wide.add_column(volume^)
    wide.add_column(only_brazil^)
    var by: List[String] = ["o_year"]
    var specs: List[AggSpec] = [
        AggSpec("brazil_volume", AggKind.SUM, "brazil"),
        AggSpec("volume", AggKind.SUM, "total"),
    ]
    var grouped = wide.group_by(by^, specs^, True, True)
    var share = (grouped.column("brazil") / grouped.column("total")).rename(
        "mkt_share"
    )
    var wantedcols: List[String] = ["o_year", "mkt_share"]
    return grouped.with_column(share^).select(wantedcols^)


def q9(ref tables: Tpch) raises -> DataFrame:
    """Product Type Profit Measure.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var part_want: List[String] = ["p_partkey"]
    var green = _keep(
        tables.part,
        part_want,
        _contains(tables.part, "p_name", "green"),
    )
    var line_want: List[String] = [
        "l_orderkey",
        "l_partkey",
        "l_suppkey",
        "l_quantity",
        "l_extendedprice",
        "l_discount",
    ]
    var sold = tables.lineitem.select(line_want^)
    var partkey: List[String] = ["p_partkey"]
    var linepart: List[String] = ["l_partkey"]
    var lines = green.join_on(sold, partkey^, linepart^)
    var supplier_want: List[String] = ["s_suppkey", "s_nationkey"]
    var sellers = tables.supplier.select(supplier_want^)
    var suppkey: List[String] = ["l_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    lines = lines.join_on(sellers, suppkey^, supplier_id^)
    var stock_want: List[String] = [
        "ps_partkey",
        "ps_suppkey",
        "ps_supplycost",
    ]
    var stock = tables.partsupp.select(stock_want^)
    var left_pair: List[String] = ["p_partkey", "l_suppkey"]
    var right_pair: List[String] = ["ps_partkey", "ps_suppkey"]
    lines = lines.join_on(stock, left_pair^, right_pair^)
    var order_want: List[String] = ["o_orderkey", "o_orderdate"]
    var placed = tables.orders.select(order_want^)
    var lineorder: List[String] = ["l_orderkey"]
    var orderkey: List[String] = ["o_orderkey"]
    lines = lines.join_on(placed, lineorder^, orderkey^)
    var nation_want: List[String] = ["n_nationkey", "n_name"]
    var nations = tables.nation.select(nation_want^)
    var supp_nation: List[String] = ["s_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    lines = lines.join_on(nations, supp_nation^, nation_id^)

    var year = _year(lines, "o_orderdate", "o_year")
    var cost = _product(lines, "ps_supplycost", "l_quantity", "cost")
    var amount = (_discounted(lines, "amount") - cost).rename("amount")
    var named = lines^
    named.add_column(year^)
    named.add_column(amount^)
    var wide = named.rename("n_name", "nation")
    var by: List[String] = ["nation", "o_year"]
    var specs: List[AggSpec] = [AggSpec("amount", AggKind.SUM, "sum_profit")]
    var grouped = wide.group_by(by^, specs^, True, False)
    var order: List[String] = ["nation", "o_year"]
    var descending: List[Bool] = [False, True]
    return _sorted(grouped^, order^, descending^)


def q10(ref tables: Tpch) raises -> DataFrame:
    """Returned Item Reporting.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var quarter = _both(
        _cmp(tables.orders, "o_orderdate", BinaryOp.GE, day(1993, 10, 1)),
        _cmp(tables.orders, "o_orderdate", BinaryOp.LT, day(1994, 1, 1)),
    )
    var order_want: List[String] = ["o_orderkey", "o_custkey"]
    var early = _keep(tables.orders, order_want, quarter)
    var custkey: List[String] = ["c_custkey"]
    var ordercust: List[String] = ["o_custkey"]
    var placed = tables.customer.join_on(early, custkey^, ordercust^)
    var line_want: List[String] = [
        "l_orderkey",
        "l_extendedprice",
        "l_discount",
    ]
    var returned = _keep(
        tables.lineitem,
        line_want,
        _cmp(tables.lineitem, "l_returnflag", BinaryOp.EQ, Value(String("R"))),
    )
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var lines = placed.join_on(returned, orderkey^, lineorder^)
    var nation_want: List[String] = ["n_nationkey", "n_name"]
    var nations = tables.nation.select(nation_want^)
    var cust_nation: List[String] = ["c_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    lines = lines.join_on(nations, cust_nation^, nation_id^)

    var revenue = _discounted(lines, "revenue")
    var wide = lines^
    wide.add_column(revenue^)
    var by: List[String] = [
        "c_custkey",
        "c_name",
        "c_acctbal",
        "c_phone",
        "n_name",
        "c_address",
        "c_comment",
    ]
    var specs: List[AggSpec] = [AggSpec("revenue", AggKind.SUM, "revenue")]
    var grouped = wide.group_by(by^, specs^, True, False)
    var wantedcols: List[String] = [
        "c_custkey",
        "c_name",
        "revenue",
        "c_acctbal",
        "n_name",
        "c_address",
        "c_phone",
        "c_comment",
    ]
    var order: List[String] = ["revenue"]
    var descending: List[Bool] = [True]
    return _sorted(grouped.select(wantedcols^), order^, descending^).head(20)


def q11(ref tables: Tpch) raises -> DataFrame:
    """Important Stock Identification.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var stock_want: List[String] = [
        "ps_partkey",
        "ps_suppkey",
        "ps_availqty",
        "ps_supplycost",
    ]
    var held = tables.partsupp.select(stock_want^)
    var supplier_want: List[String] = ["s_suppkey", "s_nationkey"]
    var sellers = tables.supplier.select(supplier_want^)
    var partsupp_supplier: List[String] = ["ps_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    var stock = held.join_on(sellers, partsupp_supplier^, supplier_id^)
    # One nation survives, so selecting it before the join makes this a probe
    # against a single row rather than a filter over the join output.
    var nation_want: List[String] = ["n_nationkey"]
    var germany = _keep(
        tables.nation,
        nation_want,
        _cmp(tables.nation, "n_name", BinaryOp.EQ, Value(String("GERMANY"))),
    )
    var supp_nation: List[String] = ["s_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    stock = stock.join_on(germany, supp_nation^, nation_id^)
    var value = (
        stock.column("ps_supplycost") * stock.column("ps_availqty")
    ).rename("value")
    var threshold = _reduced(value, AggKind.SUM) * 0.0001
    var wide = stock^
    wide.add_column(value^)
    var by: List[String] = ["ps_partkey"]
    var specs: List[AggSpec] = [AggSpec("value", AggKind.SUM, "value")]
    var grouped = wide.group_by(by^, specs^, True, False)
    grouped = grouped.filter(
        _cmp(grouped, "value", BinaryOp.GT, Value(Float64(threshold)))
    )
    var wantedcols: List[String] = ["ps_partkey", "value"]
    var order: List[String] = ["value"]
    var descending: List[Bool] = [True]
    return _sorted(grouped.select(wantedcols^), order^, descending^)


def q12(ref tables: Tpch) raises -> DataFrame:
    """Shipping Modes and Order Priority.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    # Every predicate here reads only lineitem, so they run before the join
    # rather than after it. Joining first meant building twenty five columns of
    # six million rows to keep about thirty thousand of them.
    ref lineitem = tables.lineitem
    var modes: List[String] = ["MAIL", "SHIP"]
    var wanted = _in(lineitem, "l_shipmode", modes)
    wanted = _both(
        wanted,
        _cmp2(lineitem, "l_commitdate", "l_receiptdate", BinaryOp.LT),
    )
    wanted = _both(
        wanted,
        _cmp2(lineitem, "l_shipdate", "l_commitdate", BinaryOp.LT),
    )
    wanted = _both(
        wanted, _cmp(lineitem, "l_receiptdate", BinaryOp.GE, day(1994, 1, 1))
    )
    wanted = _both(
        wanted, _cmp(lineitem, "l_receiptdate", BinaryOp.LT, day(1995, 1, 1))
    )
    var line_want: List[String] = ["l_orderkey", "l_shipmode"]
    var lines = _keep(lineitem, line_want, wanted)
    var order_want: List[String] = ["o_orderkey", "o_orderpriority"]
    var placed = tables.orders.select(order_want^)
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var kept = placed.join_on(lines, orderkey^, lineorder^)

    var urgent: List[String] = ["1-URGENT", "2-HIGH"]
    var priority = kept.column("o_orderpriority").is_in(_texts(urgent^))
    var high = _flags(priority.copy(), "high_line_count")
    var low = _flags(_not(priority^), "low_line_count")
    var wide = kept^
    wide.add_column(high^)
    wide.add_column(low^)
    var by: List[String] = ["l_shipmode"]
    var specs: List[AggSpec] = [
        AggSpec("high_line_count", AggKind.SUM, "high_line_count"),
        AggSpec("low_line_count", AggKind.SUM, "low_line_count"),
    ]
    return wide.group_by(by^, specs^, True, True)


def q13(ref tables: Tpch) raises -> DataFrame:
    """Customer Distribution.

    The outer join is the whole query: a customer with no order still has a
    count, and it is zero. An inner join would drop exactly the rows the
    distribution is about.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var order_want: List[String] = ["o_orderkey", "o_custkey"]
    var ordinary = _keep(
        tables.orders,
        order_want,
        _not(
            tables.orders.column("o_comment").str_contains_in_order(
                "special", "requests"
            )
        ),
    )
    var custkey: List[String] = ["c_custkey"]
    var ordercust: List[String] = ["o_custkey"]
    var placed = tables.customer.join_on(
        ordinary, custkey^, ordercust^, JoinKind.LEFT
    )
    var by: List[String] = ["c_custkey"]
    var specs: List[AggSpec] = [
        AggSpec("o_orderkey", AggKind.COUNT, "c_count")
    ]
    var per_customer = placed.group_by(by^, specs^, True, False)
    var again: List[String] = ["c_count"]
    var counts: List[AggSpec] = [AggSpec("c_count", AggKind.SIZE, "custdist")]
    var distribution = per_customer.group_by(again^, counts^, True, False)
    var order: List[String] = ["custdist", "c_count"]
    var descending: List[Bool] = [True, True]
    return _sorted(distribution^, order^, descending^)


def q14(ref tables: Tpch) raises -> DataFrame:
    """Promotion Effect.

    Args:
        tables: The loaded tables.

    Returns:
        The answer, one row.

    Raises:
        As the operations it runs do.
    """
    var month = _both(
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.GE, day(1995, 9, 1)),
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.LT, day(1995, 10, 1)),
    )
    var line_want: List[String] = [
        "l_partkey",
        "l_extendedprice",
        "l_discount",
    ]
    var lines = _keep(tables.lineitem, line_want, month)
    var linepart: List[String] = ["l_partkey"]
    var partkey: List[String] = ["p_partkey"]
    var joined = lines.join_on(tables.part, linepart^, partkey^)
    var revenue = _discounted(joined, "revenue")
    var promo = joined.column("p_type").str_starts_with("PROMO")
    var only_promo = revenue.pick(promo, _zeros(joined.rows))
    var share = _reduced(only_promo, AggKind.SUM) / _reduced(
        revenue, AggKind.SUM
    )
    return _one("promo_revenue", 100.0 * share)


def q15(ref tables: Tpch) raises -> DataFrame:
    """Top Supplier.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var quarter = _both(
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.GE, day(1996, 1, 1)),
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.LT, day(1996, 4, 1)),
    )
    var line_want: List[String] = [
        "l_suppkey",
        "l_extendedprice",
        "l_discount",
    ]
    var lines = _keep(tables.lineitem, line_want, quarter)
    var revenue = _discounted(lines, "revenue")
    var wide = lines^
    wide.add_column(revenue^)
    var by: List[String] = ["l_suppkey"]
    var specs: List[AggSpec] = [
        AggSpec("revenue", AggKind.SUM, "total_revenue")
    ]
    var per_supplier = wide.group_by(by^, specs^, True, False)
    var best = _reduced(per_supplier.column("total_revenue"), AggKind.MAX)

    var supplier_id: List[String] = ["s_suppkey"]
    var line_supplier: List[String] = ["l_suppkey"]
    var joined = tables.supplier.join_on(
        per_supplier, supplier_id^, line_supplier^
    )
    joined = joined.filter(
        _cmp(joined, "total_revenue", BinaryOp.EQ, Value(Float64(best)))
    )
    var wantedcols: List[String] = [
        "s_suppkey",
        "s_name",
        "s_address",
        "s_phone",
        "total_revenue",
    ]
    var order: List[String] = ["s_suppkey"]
    var descending: List[Bool] = [False]
    return _sorted(joined.select(wantedcols^), order^, descending^)


def q16(ref tables: Tpch) raises -> DataFrame:
    """Parts/Supplier Relationship.

    The `not in` subquery is an anti join, and the count of distinct suppliers
    is one aggregate rather than a distinct followed by a count.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var keep: List[String] = ["s_suppkey"]
    var complained = tables.supplier.filter(
        tables.supplier.column("s_comment").str_contains_in_order(
            "Customer", "Complaints"
        )
    ).select(keep^)

    var brands = _cmp(tables.part, "p_brand", BinaryOp.NE, Value(String("Brand#45")))
    var types = _not(
        tables.part.column("p_type").str_starts_with("MEDIUM POLISHED")
    )
    var sizes: List[Int] = [49, 14, 23, 45, 19, 3, 36, 9]
    var wanted_size = tables.part.column("p_size").is_in(_int32s(sizes^))
    var wanted = tables.part.filter(
        _both(_both(brands, types), wanted_size)
    )

    var partkey: List[String] = ["p_partkey"]
    var partsupp_part: List[String] = ["ps_partkey"]
    var offered = wanted.join_on(tables.partsupp, partkey^, partsupp_part^)
    var partsupp_supplier: List[String] = ["ps_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    offered = offered.join_on(
        complained, partsupp_supplier^, supplier_id^, JoinKind.ANTI
    )

    var by: List[String] = ["p_brand", "p_type", "p_size"]
    var specs: List[AggSpec] = [
        AggSpec("ps_suppkey", AggKind.NUNIQUE, "supplier_cnt")
    ]
    var grouped = offered.group_by(by^, specs^, True, False)
    var wantedcols: List[String] = [
        "p_brand",
        "p_type",
        "p_size",
        "supplier_cnt",
    ]
    var order: List[String] = [
        "supplier_cnt",
        "p_brand",
        "p_type",
        "p_size",
    ]
    var descending: List[Bool] = [True, False, False, False]
    return _sorted(grouped.select(wantedcols^), order^, descending^)


def q17(ref tables: Tpch) raises -> DataFrame:
    """Small-Quantity-Order Revenue.

    The correlated subquery compares each line against its own part's average
    quantity, which is `group_broadcast`: the reduction runs once per part and
    every line reads its own part's answer. Without it the query is a group by,
    a filter and a join back onto the input, which is a build and a probe over
    a question that is a gather.

    Args:
        tables: The loaded tables.

    Returns:
        The answer, one row.

    Raises:
        As the operations it runs do.
    """
    var brand = _cmp(tables.part, "p_brand", BinaryOp.EQ, Value(String("Brand#23")))
    var container = _cmp(tables.part, "p_container", BinaryOp.EQ, Value(String("MED BOX")))
    var keep: List[String] = ["p_partkey"]
    var wanted = tables.part.filter(_both(brand, container)).select(keep^)

    var linepart: List[String] = ["l_partkey"]
    var partkey: List[String] = ["p_partkey"]
    var lines = tables.lineitem.join_on(wanted, linepart^, partkey^)
    var by: List[String] = ["l_partkey"]
    var specs: List[AggSpec] = [
        AggSpec("l_quantity", AggKind.MEAN, "avg_qty")
    ]
    var average = lines.group_broadcast(by^, specs^)
    var threshold = (average.column("avg_qty") * Value(Float64(0.2))).rename(
        "threshold"
    )
    var wide = lines^
    wide.add_column(threshold^)
    var small = wide.filter(
        _cmp2(wide, "l_quantity", "threshold", BinaryOp.LT)
    )
    return _one(
        "avg_yearly",
        _reduced(small.column("l_extendedprice"), AggKind.SUM) / 7.0,
    )


def q18(ref tables: Tpch) raises -> DataFrame:
    """Large Volume Customer.

    The `in` subquery is a group's sum used as a filter on that group's own
    rows, which is what `group_broadcast` is for, and `group_broadcast` is the
    wrong tool here anyway. It was written for a small number of large groups,
    and this groups six million lines into a million and a half orders, so the
    pass that spreads each order's total back across its lines costs more than
    aggregating the orders and probing them. Measured at sf1, the broadcast is
    a hundred and forty six milliseconds and this is forty eight. q17 is the
    same shape with two hundred thousand groups and there the broadcast wins.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var by: List[String] = ["l_orderkey"]
    var specs: List[AggSpec] = [AggSpec("l_quantity", AggKind.SUM, "total")]
    var totals = tables.lineitem.group_by(by^, specs^, True, False)
    var key_want: List[String] = ["l_orderkey"]
    var heavy_keys = _keep(
        totals,
        key_want,
        _cmp(totals, "total", BinaryOp.GT, Value(Float64(300.0))),
    )
    var line_want: List[String] = ["l_orderkey", "l_quantity"]
    var lines = tables.lineitem.select(line_want^)
    var on: List[String] = ["l_orderkey"]
    var heavy = lines.join(heavy_keys, on^, JoinKind.SEMI)

    var order_want: List[String] = [
        "o_orderkey",
        "o_custkey",
        "o_orderdate",
        "o_totalprice",
    ]
    var placed_orders = tables.orders.select(order_want^)
    var customer_want: List[String] = ["c_custkey", "c_name"]
    var named = tables.customer.select(customer_want^)
    var lineorder: List[String] = ["l_orderkey"]
    var orderkey: List[String] = ["o_orderkey"]
    var placed = heavy.join_on(placed_orders, lineorder^, orderkey^)
    var ordercust: List[String] = ["o_custkey"]
    var custkey: List[String] = ["c_custkey"]
    placed = placed.join_on(named, ordercust^, custkey^)

    var keys: List[String] = [
        "c_name",
        "o_custkey",
        "o_orderkey",
        "o_orderdate",
        "o_totalprice",
    ]
    var sums: List[AggSpec] = [
        AggSpec("l_quantity", AggKind.SUM, "sum(l_quantity)")
    ]
    var grouped = placed.group_by(keys^, sums^, True, False)
    var renamed = grouped.rename("o_custkey", "c_custkey")
    var wantedcols: List[String] = [
        "c_name",
        "c_custkey",
        "o_orderkey",
        "o_orderdate",
        "o_totalprice",
        "sum(l_quantity)",
    ]
    var order: List[String] = ["o_totalprice", "o_orderdate"]
    var descending: List[Bool] = [True, False]
    return _sorted(renamed.select(wantedcols^), order^, descending^).head(100)


def q19(ref tables: Tpch) raises -> DataFrame:
    """Discounted Revenue.

    The three disjuncts differ in the brand, the container, the quantity and the
    size, but between them they only ever ask for one of three brands, one of
    twelve containers, a size from one to fifteen and a quantity from one to
    thirty. Those four run on `part` and on `lineitem` before the join, which is
    where a planner would put them, and the disjunction itself then runs over
    what is left rather than over every shipped line. Eighty three milliseconds
    at sf1 written the other way and seventy this way.

    Args:
        tables: The loaded tables.

    Returns:
        The answer, one row.

    Raises:
        As the operations it runs do.
    """
    var modes: List[String] = ["AIR", "AIR REG"]
    var narrow = _in(tables.lineitem, "l_shipmode", modes)
    narrow = _both(
        narrow,
        _cmp(
            tables.lineitem,
            "l_shipinstruct",
            BinaryOp.EQ,
            Value(String("DELIVER IN PERSON")),
        ),
    )
    narrow = _both(
        narrow,
        _cmp(tables.lineitem, "l_quantity", BinaryOp.GE, Value(Float64(1.0))),
    )
    narrow = _both(
        narrow,
        _cmp(tables.lineitem, "l_quantity", BinaryOp.LE, Value(Float64(30.0))),
    )
    var line_want: List[String] = [
        "l_partkey",
        "l_quantity",
        "l_extendedprice",
        "l_discount",
    ]
    var lines = _keep(tables.lineitem, line_want, narrow)

    var brands: List[String] = ["Brand#12", "Brand#23", "Brand#34"]
    var boxes: List[String] = [
        "SM CASE",
        "SM BOX",
        "SM PACK",
        "SM PKG",
        "MED BAG",
        "MED BOX",
        "MED PKG",
        "MED PACK",
        "LG CASE",
        "LG BOX",
        "LG PACK",
        "LG PKG",
    ]
    var wanted = _both(
        _in(tables.part, "p_brand", brands),
        _in(tables.part, "p_container", boxes),
    )
    wanted = _both(
        wanted, _cmp(tables.part, "p_size", BinaryOp.GE, Value(Int32(1)))
    )
    wanted = _both(
        wanted, _cmp(tables.part, "p_size", BinaryOp.LE, Value(Int32(15)))
    )
    var part_want: List[String] = [
        "p_partkey",
        "p_brand",
        "p_container",
        "p_size",
    ]
    var parts = _keep(tables.part, part_want, wanted)

    var linepart: List[String] = ["l_partkey"]
    var partkey: List[String] = ["p_partkey"]
    var joined = lines.join_on(parts, linepart^, partkey^)

    # Each disjunct still names its own container set and its own bounds. The
    # bounds the pushed filters already guarantee are the ones left out: every
    # surviving row has a size of at least one and a quantity between one and
    # thirty, so only the upper size bound and the tighter quantity bounds are
    # asked about again.
    var small_boxes: List[String] = ["SM CASE", "SM BOX", "SM PACK", "SM PKG"]
    var first = _both(
        _cmp(joined, "p_brand", BinaryOp.EQ, Value(String("Brand#12"))),
        _in(joined, "p_container", small_boxes),
    )
    first = _both(
        first, _cmp(joined, "l_quantity", BinaryOp.LE, Value(Float64(11.0)))
    )
    first = _both(first, _cmp(joined, "p_size", BinaryOp.LE, Value(Int32(5))))

    var medium: List[String] = ["MED BAG", "MED BOX", "MED PKG", "MED PACK"]
    var second = _both(
        _cmp(joined, "p_brand", BinaryOp.EQ, Value(String("Brand#23"))),
        _in(joined, "p_container", medium),
    )
    second = _both(
        second, _cmp(joined, "l_quantity", BinaryOp.GE, Value(Float64(10.0)))
    )
    second = _both(
        second, _cmp(joined, "l_quantity", BinaryOp.LE, Value(Float64(20.0)))
    )
    second = _both(
        second, _cmp(joined, "p_size", BinaryOp.LE, Value(Int32(10)))
    )

    var large: List[String] = ["LG CASE", "LG BOX", "LG PACK", "LG PKG"]
    var third = _both(
        _cmp(joined, "p_brand", BinaryOp.EQ, Value(String("Brand#34"))),
        _in(joined, "p_container", large),
    )
    third = _both(
        third, _cmp(joined, "l_quantity", BinaryOp.GE, Value(Float64(20.0)))
    )

    var kept = joined.filter(_either(_either(first^, second^), third^))
    return _one("revenue", _reduced(_discounted(kept, "revenue"), AggKind.SUM))


def q20(ref tables: Tpch) raises -> DataFrame:
    """Potential Part Promotion.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var keep: List[String] = ["p_partkey"]
    var forest = tables.part.filter(
        tables.part.column("p_name").str_starts_with("forest")
    ).select(keep^)

    var year = _both(
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.GE, day(1994, 1, 1)),
        _cmp(tables.lineitem, "l_shipdate", BinaryOp.LT, day(1995, 1, 1)),
    )
    var line_want: List[String] = ["l_partkey", "l_suppkey", "l_quantity"]
    var lines = _keep(tables.lineitem, line_want, year)
    var by: List[String] = ["l_partkey", "l_suppkey"]
    var specs: List[AggSpec] = [AggSpec("l_quantity", AggKind.SUM, "shipped")]
    var per_pair = lines.group_by(by^, specs^, True, False)
    var threshold = (
        per_pair.column("shipped") * Value(Float64(0.5))
    ).rename("threshold")
    per_pair = per_pair.with_column(threshold^)

    var partsupp_part: List[String] = ["ps_partkey"]
    var partkey: List[String] = ["p_partkey"]
    var candidates = tables.partsupp.join_on(forest, partsupp_part^, partkey^)
    var left_pair: List[String] = ["ps_partkey", "ps_suppkey"]
    var right_pair: List[String] = ["l_partkey", "l_suppkey"]
    candidates = candidates.join_on(per_pair, left_pair^, right_pair^)
    candidates = candidates.filter(
        _cmp2(candidates, "ps_availqty", "threshold", BinaryOp.GT)
    )
    var supplier_keep: List[String] = ["ps_suppkey"]
    candidates = candidates.select(supplier_keep^).drop_duplicates()

    var supp_nation: List[String] = ["s_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    var canadian = tables.supplier.join_on(
        tables.nation, supp_nation^, nation_id^
    )
    canadian = canadian.filter(
        _cmp(canadian, "n_name", BinaryOp.EQ, Value(String("CANADA")))
    )
    var supplier_id: List[String] = ["s_suppkey"]
    var candidate_key: List[String] = ["ps_suppkey"]
    canadian = canadian.join_on(
        candidates, supplier_id^, candidate_key^, JoinKind.SEMI
    )
    var wantedcols: List[String] = ["s_name", "s_address"]
    var order: List[String] = ["s_name"]
    var descending: List[Bool] = [False]
    return _sorted(canadian.select(wantedcols^), order^, descending^)


def q21(ref tables: Tpch) raises -> DataFrame:
    """Suppliers Who Kept Orders Waiting.

    The two correlated subqueries become counts of distinct suppliers per order,
    one over every line and one over the late lines. That is the same predicate:
    there is another supplier on the order, and no other supplier on it was
    late.

    Both counts are compared against a constant, so the comparison happens on
    the order rather than on the three million late lines the order would be
    joined to, and what the lines then need from the orders is only whether
    there is a row, which is a semi join. The same for the order status, the
    nation and the supplier, all of which used to be joined in whole and
    filtered afterwards. A hundred and sixty milliseconds against two hundred
    and eighty five at sf1.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    ref lineitem = tables.lineitem
    var by: List[String] = ["l_orderkey"]
    var specs: List[AggSpec] = [
        AggSpec("l_suppkey", AggKind.NUNIQUE, "distinct_suppliers")
    ]
    var per_order = lineitem.group_by(by^, specs^, True, False)
    var shared_want: List[String] = ["l_orderkey"]
    var shared = _keep(
        per_order,
        shared_want,
        _cmp(per_order, "distinct_suppliers", BinaryOp.GT, Value(Int64(1))),
    )

    var late_mask = _cmp2(lineitem, "l_receiptdate", "l_commitdate", BinaryOp.GT)
    var late_want: List[String] = ["l_orderkey", "l_suppkey"]
    var late = _keep(lineitem, late_want, late_mask)
    var late_by: List[String] = ["l_orderkey"]
    var late_specs: List[AggSpec] = [
        AggSpec("l_suppkey", AggKind.NUNIQUE, "distinct_late_suppliers")
    ]
    var late_per_order = late.group_by(late_by^, late_specs^, True, False)
    var only_want: List[String] = ["l_orderkey"]
    var only = _keep(
        late_per_order,
        only_want,
        _cmp(
            late_per_order,
            "distinct_late_suppliers",
            BinaryOp.EQ,
            Value(Int64(1)),
        ),
    )

    var orderkey: List[String] = ["l_orderkey"]
    var joined = late.join(only, orderkey.copy(), JoinKind.SEMI)
    joined = joined.join(shared, orderkey^, JoinKind.SEMI)

    var order_want: List[String] = ["o_orderkey"]
    var finished = _keep(
        tables.orders,
        order_want,
        _cmp(tables.orders, "o_orderstatus", BinaryOp.EQ, Value(String("F"))),
    )
    var lineorder: List[String] = ["l_orderkey"]
    var order_id: List[String] = ["o_orderkey"]
    joined = joined.join_on(finished, lineorder^, order_id^, JoinKind.SEMI)

    var nation_want: List[String] = ["n_nationkey"]
    var saudi = _keep(
        tables.nation,
        nation_want,
        _cmp(
            tables.nation, "n_name", BinaryOp.EQ, Value(String("SAUDI ARABIA"))
        ),
    )
    var supplier_want: List[String] = ["s_suppkey", "s_name", "s_nationkey"]
    var sellers = tables.supplier.select(supplier_want^)
    var supp_nation: List[String] = ["s_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    var local = sellers.join_on(saudi, supp_nation^, nation_id^, JoinKind.SEMI)
    var suppkey: List[String] = ["l_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    joined = joined.join_on(local, suppkey^, supplier_id^)

    var names: List[String] = ["s_name"]
    var counts: List[AggSpec] = [AggSpec("s_name", AggKind.SIZE, "numwait")]
    var grouped = joined.group_by(names^, counts^, True, False)
    var order: List[String] = ["numwait", "s_name"]
    var descending: List[Bool] = [True, False]
    return _sorted(grouped^, order^, descending^).head(100)


def q22(ref tables: Tpch) raises -> DataFrame:
    """Global Sales Opportunity.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var codes: List[String] = ["13", "31", "23", "29", "30", "18", "17"]
    var country = tables.customer.column("c_phone").str_slice(0, 2).rename(
        "cntrycode"
    )
    var wide = tables.customer.with_column(country^)
    var selected = wide.filter(
        wide.column("cntrycode").is_in(_texts(codes^))
    )
    var positive = selected.filter(
        _cmp(selected, "c_acctbal", BinaryOp.GT, Value(Float64(0.0)))
    )
    var average = _reduced(positive.column("c_acctbal"), AggKind.MEAN)
    var rich = selected.filter(
        _cmp(selected, "c_acctbal", BinaryOp.GT, Value(Float64(average)))
    )

    var keep: List[String] = ["o_custkey"]
    var placed = tables.orders.select(keep^)
    var custkey: List[String] = ["c_custkey"]
    var ordercust: List[String] = ["o_custkey"]
    var idle = rich.join_on(placed, custkey^, ordercust^, JoinKind.ANTI)

    var by: List[String] = ["cntrycode"]
    var specs: List[AggSpec] = [
        AggSpec("c_custkey", AggKind.SIZE, "numcust"),
        AggSpec("c_acctbal", AggKind.SUM, "totacctbal"),
    ]
    return idle.group_by(by^, specs^, True, True)


def run_tpch(query: String, ref tables: Tpch) raises -> DataFrame:
    """Runs one TPC-H query and returns its answer.

    Args:
        query: The query name, `q1` through `q22`.
        tables: The loaded tables.

    Returns:
        The answer frame.

    Raises:
        If the query is not one this engine runs.
    """
    if query == "q1":
        return q1(tables)
    if query == "q2":
        return q2(tables)
    if query == "q3":
        return q3(tables)
    if query == "q4":
        return q4(tables)
    if query == "q5":
        return q5(tables)
    if query == "q6":
        return q6(tables)
    if query == "q7":
        return q7(tables)
    if query == "q8":
        return q8(tables)
    if query == "q9":
        return q9(tables)
    if query == "q10":
        return q10(tables)
    if query == "q11":
        return q11(tables)
    if query == "q12":
        return q12(tables)
    if query == "q13":
        return q13(tables)
    if query == "q14":
        return q14(tables)
    if query == "q15":
        return q15(tables)
    if query == "q16":
        return q16(tables)
    if query == "q17":
        return q17(tables)
    if query == "q18":
        return q18(tables)
    if query == "q19":
        return q19(tables)
    if query == "q20":
        return q20(tables)
    if query == "q21":
        return q21(tables)
    if query == "q22":
        return q22(tables)
    raise Error("tpch: no query called " + query)


def tpch_supported() -> List[String]:
    """Returns the queries this module answers.

    Returns:
        The query names.
    """
    var out = List[String](capacity=22)
    for i in range(1, 23):
        out.append(String("q", i))
    return out^
