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
from firepanda.kernel import logical_and, logical_or, reduce_any

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
    # One minus the discount, written as a multiply and an add because a scalar
    # on the left of a subtract is not a spelling the operators have.
    var factor = (
        frame.column("l_discount") * Value(Float64(-1.0))
    ) + Value(Float64(1.0))
    return (frame.column("l_extendedprice") * factor).rename(name)


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
    var kept = tables.lineitem.filter(
        _mask(tables.lineitem.column("l_shipdate") <= day(1998, 9, 2))
    )
    var disc_price = _discounted(kept, "disc_price")
    var charge = (
        disc_price * (kept.column("l_tax") + Value(Float64(1.0)))
    ).rename("charge")
    var wide = kept.with_column(disc_price^).with_column(charge^)
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
        _mask(tables.region.column("r_name") == Value(String("EUROPE")))
    ).join_on(tables.nation, region_key^, nation_key^)
    var nation_id: List[String] = ["n_nationkey"]
    var supplier_nation: List[String] = ["s_nationkey"]
    europe = europe.join_on(tables.supplier, nation_id^, supplier_nation^)
    var supplier_id: List[String] = ["s_suppkey"]
    var partsupp_supplier: List[String] = ["ps_suppkey"]
    europe = europe.join_on(tables.partsupp, supplier_id^, partsupp_supplier^)

    var sized = _mask(tables.part.column("p_size") == Value(Int32(15)))
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
    var building = tables.customer.filter(
        _mask(tables.customer.column("c_mktsegment") == Value(String("BUILDING")))
    )
    var custkey: List[String] = ["c_custkey"]
    var ordercust: List[String] = ["o_custkey"]
    var placed = building.join_on(tables.orders, custkey^, ordercust^)
    placed = placed.filter(_mask(placed.column("o_orderdate") < cutoff))
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var lines = placed.join_on(tables.lineitem, orderkey^, lineorder^)
    lines = lines.filter(_mask(lines.column("l_shipdate") > cutoff))

    var wide = lines.with_column(_discounted(lines, "revenue"))
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
    var late = tables.lineitem.filter(
        _mask(
            tables.lineitem.column("l_commitdate")
            < tables.lineitem.column("l_receiptdate")
        )
    )
    var quarter = _both(
        _mask(tables.orders.column("o_orderdate") >= day(1993, 7, 1)),
        _mask(tables.orders.column("o_orderdate") < day(1993, 10, 1)),
    )
    var placed = tables.orders.filter(quarter)
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
        _mask(tables.region.column("r_name") == Value(String("ASIA")))
    ).join_on(tables.nation, region_key^, nation_region^)
    var nation_id: List[String] = ["n_nationkey"]
    var cust_nation: List[String] = ["c_nationkey"]
    var here = asia.join_on(tables.customer, nation_id^, cust_nation^)
    var custkey: List[String] = ["c_custkey"]
    var ordercust: List[String] = ["o_custkey"]
    var placed = here.join_on(tables.orders, custkey^, ordercust^)
    var year = _both(
        _mask(placed.column("o_orderdate") >= day(1994, 1, 1)),
        _mask(placed.column("o_orderdate") < day(1995, 1, 1)),
    )
    placed = placed.filter(year)
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var lines = placed.join_on(tables.lineitem, orderkey^, lineorder^)
    var left_pair: List[String] = ["l_suppkey", "n_nationkey"]
    var right_pair: List[String] = ["s_suppkey", "s_nationkey"]
    var local = lines.join_on(tables.supplier, left_pair^, right_pair^)

    var wide = local.with_column(_discounted(local, "revenue"))
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
        _mask(lineitem.column("l_shipdate") >= day(1994, 1, 1)),
        _mask(lineitem.column("l_shipdate") < day(1995, 1, 1)),
    )
    var discounted = _both(
        _mask(lineitem.column("l_discount") >= Value(Float64(0.05))),
        _mask(lineitem.column("l_discount") <= Value(Float64(0.07))),
    )
    var small = _mask(lineitem.column("l_quantity") < Value(Float64(24.0)))
    var kept = lineitem.filter(_both(_both(shipped, discounted), small))
    var revenue = kept.column("l_extendedprice") * kept.column("l_discount")
    return _one("revenue", _reduced(revenue, AggKind.SUM))


def q7(ref tables: Tpch) raises -> DataFrame:
    """Volume Shipping.

    The disjunctive nation pair predicate becomes two joins against the same two
    nation frame and a filter that the two sides disagree, rather than a filter
    over the full cross product, which is the shape the query is trying to
    avoid.

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

    var window = _both(
        _mask(tables.lineitem.column("l_shipdate") >= day(1995, 1, 1)),
        _mask(tables.lineitem.column("l_shipdate") <= day(1996, 12, 31)),
    )
    var lines = tables.lineitem.filter(window)
    var suppkey: List[String] = ["l_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    var shipping = lines.join_on(tables.supplier, suppkey^, supplier_id^)
    var supp_nation_key: List[String] = ["s_nationkey"]
    var supp_key: List[String] = ["supp_nationkey"]
    shipping = shipping.join_on(supp, supp_nation_key^, supp_key^)
    var lineorder: List[String] = ["l_orderkey"]
    var orderkey: List[String] = ["o_orderkey"]
    shipping = shipping.join_on(tables.orders, lineorder^, orderkey^)
    var ordercust: List[String] = ["o_custkey"]
    var custkey: List[String] = ["c_custkey"]
    shipping = shipping.join_on(tables.customer, ordercust^, custkey^)
    var cust_nation_key: List[String] = ["c_nationkey"]
    var cust_key: List[String] = ["cust_nationkey"]
    shipping = shipping.join_on(cust, cust_nation_key^, cust_key^)
    shipping = shipping.filter(
        _mask(
            shipping.column("supp_nation") != shipping.column("cust_nation")
        )
    )

    var year = shipping.column("l_shipdate").dt("year").rename("l_year")
    var wide = shipping.with_column(year^).with_column(
        _discounted(shipping, "volume")
    )
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
            _mask(tables.region.column("r_name") == Value(String("AMERICA")))
        )
        .join_on(tables.nation, region_key^, nation_region^)
        .select(keep^)
    )
    america = america.rename("n_nationkey", "am_nationkey")

    var nation_keep: List[String] = ["n_nationkey", "n_name"]
    var supplier_nation = tables.nation.select(nation_keep^)
    supplier_nation = supplier_nation.rename("n_nationkey", "sn_nationkey")
    supplier_nation = supplier_nation.rename("n_name", "nation")

    var steel = tables.part.filter(
        _mask(
            tables.part.column("p_type") == Value(String("ECONOMY ANODIZED STEEL"))
        )
    )
    var partkey: List[String] = ["p_partkey"]
    var linepart: List[String] = ["l_partkey"]
    var lines = steel.join_on(tables.lineitem, partkey^, linepart^)
    var lineorder: List[String] = ["l_orderkey"]
    var orderkey: List[String] = ["o_orderkey"]
    var placed = lines.join_on(tables.orders, lineorder^, orderkey^)
    var window = _both(
        _mask(placed.column("o_orderdate") >= day(1995, 1, 1)),
        _mask(placed.column("o_orderdate") <= day(1996, 12, 31)),
    )
    placed = placed.filter(window)
    var ordercust: List[String] = ["o_custkey"]
    var custkey: List[String] = ["c_custkey"]
    placed = placed.join_on(tables.customer, ordercust^, custkey^)
    var cust_nation: List[String] = ["c_nationkey"]
    var am_key: List[String] = ["am_nationkey"]
    placed = placed.join_on(america, cust_nation^, am_key^)
    var suppkey: List[String] = ["l_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    placed = placed.join_on(tables.supplier, suppkey^, supplier_id^)
    var supp_nation: List[String] = ["s_nationkey"]
    var sn_key: List[String] = ["sn_nationkey"]
    placed = placed.join_on(supplier_nation, supp_nation^, sn_key^)

    var year = placed.column("o_orderdate").dt("year").rename("o_year")
    var volume = _discounted(placed, "volume")
    var brazil = _mask(placed.column("nation") == Value(String("BRAZIL")))
    var only_brazil = volume.pick(brazil, _zeros(placed.rows)).rename(
        "brazil_volume"
    )
    var wide = (
        placed.with_column(year^)
        .with_column(volume^)
        .with_column(only_brazil^)
    )
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
    var green = tables.part.filter(
        tables.part.column("p_name").str_contains("green")
    )
    var partkey: List[String] = ["p_partkey"]
    var linepart: List[String] = ["l_partkey"]
    var lines = green.join_on(tables.lineitem, partkey^, linepart^)
    var suppkey: List[String] = ["l_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    lines = lines.join_on(tables.supplier, suppkey^, supplier_id^)
    var left_pair: List[String] = ["p_partkey", "l_suppkey"]
    var right_pair: List[String] = ["ps_partkey", "ps_suppkey"]
    lines = lines.join_on(tables.partsupp, left_pair^, right_pair^)
    var lineorder: List[String] = ["l_orderkey"]
    var orderkey: List[String] = ["o_orderkey"]
    lines = lines.join_on(tables.orders, lineorder^, orderkey^)
    var supp_nation: List[String] = ["s_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    lines = lines.join_on(tables.nation, supp_nation^, nation_id^)

    var year = lines.column("o_orderdate").dt("year").rename("o_year")
    var cost = lines.column("ps_supplycost") * lines.column("l_quantity")
    var amount = (_discounted(lines, "amount") - cost).rename("amount")
    var wide = (
        lines.with_column(year^)
        .with_column(amount^)
        .rename("n_name", "nation")
    )
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
    var custkey: List[String] = ["c_custkey"]
    var ordercust: List[String] = ["o_custkey"]
    var placed = tables.customer.join_on(tables.orders, custkey^, ordercust^)
    var quarter = _both(
        _mask(placed.column("o_orderdate") >= day(1993, 10, 1)),
        _mask(placed.column("o_orderdate") < day(1994, 1, 1)),
    )
    placed = placed.filter(quarter)
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var lines = placed.join_on(tables.lineitem, orderkey^, lineorder^)
    lines = lines.filter(
        _mask(lines.column("l_returnflag") == Value(String("R")))
    )
    var cust_nation: List[String] = ["c_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    lines = lines.join_on(tables.nation, cust_nation^, nation_id^)

    var wide = lines.with_column(_discounted(lines, "revenue"))
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
    var partsupp_supplier: List[String] = ["ps_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    var stock = tables.partsupp.join_on(
        tables.supplier, partsupp_supplier^, supplier_id^
    )
    var supp_nation: List[String] = ["s_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    stock = stock.join_on(tables.nation, supp_nation^, nation_id^)
    stock = stock.filter(
        _mask(stock.column("n_name") == Value(String("GERMANY")))
    )
    var value = (
        stock.column("ps_supplycost") * stock.column("ps_availqty")
    ).rename("value")
    var threshold = _reduced(value, AggKind.SUM) * 0.0001
    var wide = stock.with_column(value^)
    var by: List[String] = ["ps_partkey"]
    var specs: List[AggSpec] = [AggSpec("value", AggKind.SUM, "value")]
    var grouped = wide.group_by(by^, specs^, True, False)
    grouped = grouped.filter(
        _mask(grouped.column("value") > Value(Float64(threshold)))
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
    var orderkey: List[String] = ["o_orderkey"]
    var lineorder: List[String] = ["l_orderkey"]
    var lines = tables.orders.join_on(tables.lineitem, orderkey^, lineorder^)
    var modes: List[String] = ["MAIL", "SHIP"]
    var wanted = lines.column("l_shipmode").is_in(_texts(modes^))
    wanted = _both(
        wanted,
        _mask(lines.column("l_commitdate") < lines.column("l_receiptdate")),
    )
    wanted = _both(
        wanted,
        _mask(lines.column("l_shipdate") < lines.column("l_commitdate")),
    )
    wanted = _both(
        wanted, _mask(lines.column("l_receiptdate") >= day(1994, 1, 1))
    )
    wanted = _both(
        wanted, _mask(lines.column("l_receiptdate") < day(1995, 1, 1))
    )
    var kept = lines.filter(wanted)

    var urgent: List[String] = ["1-URGENT", "2-HIGH"]
    var priority = kept.column("o_orderpriority").is_in(_texts(urgent^))
    var high = _flags(priority.copy(), "high_line_count")
    var low = _flags(_not(priority^), "low_line_count")
    var wide = kept.with_column(high^).with_column(low^)
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
    var ordinary = tables.orders.filter(
        _not(
            tables.orders.column("o_comment").str_contains_in_order(
                "special", "requests"
            )
        )
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
        _mask(tables.lineitem.column("l_shipdate") >= day(1995, 9, 1)),
        _mask(tables.lineitem.column("l_shipdate") < day(1995, 10, 1)),
    )
    var lines = tables.lineitem.filter(month)
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
        _mask(tables.lineitem.column("l_shipdate") >= day(1996, 1, 1)),
        _mask(tables.lineitem.column("l_shipdate") < day(1996, 4, 1)),
    )
    var lines = tables.lineitem.filter(quarter)
    var wide = lines.with_column(_discounted(lines, "revenue"))
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
        _mask(joined.column("total_revenue") == Value(Float64(best)))
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

    var brands = _mask(
        tables.part.column("p_brand") != Value(String("Brand#45"))
    )
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
    var brand = _mask(
        tables.part.column("p_brand") == Value(String("Brand#23"))
    )
    var container = _mask(
        tables.part.column("p_container") == Value(String("MED BOX"))
    )
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
    var wide = lines.with_column(threshold^)
    var small = wide.filter(
        _mask(wide.column("l_quantity") < wide.column("threshold"))
    )
    return _one(
        "avg_yearly",
        _reduced(small.column("l_extendedprice"), AggKind.SUM) / 7.0,
    )


def q18(ref tables: Tpch) raises -> DataFrame:
    """Large Volume Customer.

    The `in` subquery is a group's sum used as a filter on that group's own
    rows, which is the other half of what `group_broadcast` is for. Every line
    of a heavy order survives the filter directly, so the lineitem table is read
    once rather than joined to itself.

    Args:
        tables: The loaded tables.

    Returns:
        The answer.

    Raises:
        As the operations it runs do.
    """
    var by: List[String] = ["l_orderkey"]
    var specs: List[AggSpec] = [AggSpec("l_quantity", AggKind.SUM, "total")]
    var totals = tables.lineitem.group_broadcast(by^, specs^)
    var wide = tables.lineitem.with_column(totals.column("total"))
    var heavy = wide.filter(
        _mask(wide.column("total") > Value(Float64(300.0)))
    )

    var lineorder: List[String] = ["l_orderkey"]
    var orderkey: List[String] = ["o_orderkey"]
    var placed = heavy.join_on(tables.orders, lineorder^, orderkey^)
    var ordercust: List[String] = ["o_custkey"]
    var custkey: List[String] = ["c_custkey"]
    placed = placed.join_on(tables.customer, ordercust^, custkey^)

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

    Args:
        tables: The loaded tables.

    Returns:
        The answer, one row.

    Raises:
        As the operations it runs do.
    """
    var modes: List[String] = ["AIR", "AIR REG"]
    var shipped = tables.lineitem.column("l_shipmode").is_in(_texts(modes^))
    var in_person = _mask(
        tables.lineitem.column("l_shipinstruct")
        == Value(String("DELIVER IN PERSON"))
    )
    var lines = tables.lineitem.filter(_both(shipped, in_person))
    var linepart: List[String] = ["l_partkey"]
    var partkey: List[String] = ["p_partkey"]
    var joined = lines.join_on(tables.part, linepart^, partkey^)

    var small_boxes: List[String] = ["SM CASE", "SM BOX", "SM PACK", "SM PKG"]
    var first = _both(
        _mask(joined.column("p_brand") == Value(String("Brand#12"))),
        joined.column("p_container").is_in(_texts(small_boxes^)),
    )
    first = _both(
        first, _mask(joined.column("l_quantity") >= Value(Float64(1.0)))
    )
    first = _both(
        first, _mask(joined.column("l_quantity") <= Value(Float64(11.0)))
    )
    first = _both(first, _mask(joined.column("p_size") >= Value(Int32(1))))
    first = _both(first, _mask(joined.column("p_size") <= Value(Int32(5))))

    var medium: List[String] = ["MED BAG", "MED BOX", "MED PKG", "MED PACK"]
    var second = _both(
        _mask(joined.column("p_brand") == Value(String("Brand#23"))),
        joined.column("p_container").is_in(_texts(medium^)),
    )
    second = _both(
        second, _mask(joined.column("l_quantity") >= Value(Float64(10.0)))
    )
    second = _both(
        second, _mask(joined.column("l_quantity") <= Value(Float64(20.0)))
    )
    second = _both(second, _mask(joined.column("p_size") >= Value(Int32(1))))
    second = _both(second, _mask(joined.column("p_size") <= Value(Int32(10))))

    var large: List[String] = ["LG CASE", "LG BOX", "LG PACK", "LG PKG"]
    var third = _both(
        _mask(joined.column("p_brand") == Value(String("Brand#34"))),
        joined.column("p_container").is_in(_texts(large^)),
    )
    third = _both(
        third, _mask(joined.column("l_quantity") >= Value(Float64(20.0)))
    )
    third = _both(
        third, _mask(joined.column("l_quantity") <= Value(Float64(30.0)))
    )
    third = _both(third, _mask(joined.column("p_size") >= Value(Int32(1))))
    third = _both(third, _mask(joined.column("p_size") <= Value(Int32(15))))

    var kept = joined.filter(_either(_either(first^, second^), third^))
    return _one(
        "revenue", _reduced(_discounted(kept, "revenue"), AggKind.SUM)
    )


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
        _mask(tables.lineitem.column("l_shipdate") >= day(1994, 1, 1)),
        _mask(tables.lineitem.column("l_shipdate") < day(1995, 1, 1)),
    )
    var lines = tables.lineitem.filter(year)
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
        _mask(
            candidates.column("ps_availqty") > candidates.column("threshold")
        )
    )
    var supplier_keep: List[String] = ["ps_suppkey"]
    candidates = candidates.select(supplier_keep^).drop_duplicates()

    var supp_nation: List[String] = ["s_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    var canadian = tables.supplier.join_on(
        tables.nation, supp_nation^, nation_id^
    )
    canadian = canadian.filter(
        _mask(canadian.column("n_name") == Value(String("CANADA")))
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

    var late_mask = _mask(
        lineitem.column("l_receiptdate") > lineitem.column("l_commitdate")
    )
    var late = lineitem.filter(late_mask)
    var late_by: List[String] = ["l_orderkey"]
    var late_specs: List[AggSpec] = [
        AggSpec("l_suppkey", AggKind.NUNIQUE, "distinct_late_suppliers")
    ]
    var late_per_order = late.group_by(late_by^, late_specs^, True, False)

    var orderkey: List[String] = ["l_orderkey"]
    var joined = late.join(per_order, orderkey.copy())
    joined = joined.join(late_per_order, orderkey^)
    joined = joined.filter(
        _both(
            _mask(joined.column("distinct_suppliers") > Value(Int64(1))),
            _mask(
                joined.column("distinct_late_suppliers") == Value(Int64(1))
            ),
        )
    )

    var lineorder: List[String] = ["l_orderkey"]
    var order_id: List[String] = ["o_orderkey"]
    joined = joined.join_on(tables.orders, lineorder^, order_id^)
    joined = joined.filter(
        _mask(joined.column("o_orderstatus") == Value(String("F")))
    )
    var suppkey: List[String] = ["l_suppkey"]
    var supplier_id: List[String] = ["s_suppkey"]
    joined = joined.join_on(tables.supplier, suppkey^, supplier_id^)
    var supp_nation: List[String] = ["s_nationkey"]
    var nation_id: List[String] = ["n_nationkey"]
    joined = joined.join_on(tables.nation, supp_nation^, nation_id^)
    joined = joined.filter(
        _mask(joined.column("n_name") == Value(String("SAUDI ARABIA")))
    )

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
        _mask(selected.column("c_acctbal") > Value(Float64(0.0)))
    )
    var average = _reduced(positive.column("c_acctbal"), AggKind.MEAN)
    var rich = selected.filter(
        _mask(selected.column("c_acctbal") > Value(Float64(average)))
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
