"""What TPC-H q6 costs when nothing between the scan and the sum is materialised.

The engine runs q6 as five comparisons, four ands, a filter of two columns, a
multiply and a reduce, and every one of those allocates and writes six million
elements that the next one reads once and throws away. This runs the same
predicate and the same sum in a single pass over the four columns the query
needs, with no intermediate array of any kind, so the difference between the two
timings is what fusing the query is worth and nothing else.

It is a floor and not a proposal. Nobody is suggesting we hand write queries.
The number exists so that the fusion pass in the planner milestone has something
to be measured against, and so we can tell a slow kernel apart from a query
shape that is paying for memory traffic it does not need.

Build and run it against a lineitem file:

    mojo build -I <firepanda checkout> tools/probes/q6floor.mojo -o q6floor
    ./q6floor --path=data/tpch/sf1/lineitem.parquet --runs=15 --tasks=256

It prints the best wall time over the runs and the revenue, which should be
123141078.2283 at sf1.
"""

from std.sys import argv
from std.time import perf_counter_ns

from firepanda.array.any import AnyArray
from firepanda.exec import parallel_for
from firepanda.io.parquet import Session

comptime SHIP_FROM = Int32(8766)
"""1994-01-01, in days since the epoch, which is how a date32 column stores it."""

comptime SHIP_TO = Int32(9131)
"""1995-01-01, the same way."""


def flag(name: String, fallback: String) -> String:
    """Reads a --name=value argument.

    Args:
        name: The flag name, without the dashes.
        fallback: What to answer when the flag is absent.

    Returns:
        The value given, or the fallback.
    """
    var wanted = String("--", name, "=")
    var args = argv()
    for i in range(len(args)):
        var arg = String(args[i])
        if arg.startswith(wanted):
            return String(arg[byte = wanted.byte_length() :])
    return fallback


def values[
    dt: DType
](imm column: AnyArray) -> Pointer[Scalar[dt], ImmUntrackedOrigin]:
    """Points at a column's values, dropping the origin.

    Taking the column immutably is what makes this compile: `unsafe_ptr` hands
    back a pointer with the origin of its argument, and an origin cast keeps the
    mutability it was given, so a mutable binding here would not convert. The
    caller has to keep the frame alive itself, see `main`.

    Parameters:
        dt: The dtype to read the values as, unchecked.

    Args:
        column: The column.

    Returns:
        Its first value, with an untracked origin.
    """
    return column.unsafe_ptr[dt]().unsafe_origin_cast[ImmUntrackedOrigin]()


def main() raises:
    var path = flag("path", "")
    if path == "":
        raise Error("q6floor: pass --path=<lineitem.parquet>")
    var runs = Int(flag("runs", "15"))
    var tasks = Int(flag("tasks", "256"))

    # The decimal columns are cast on the way in, the same way the TPC-H driver
    # casts them, because the reader has no DECIMAL128 yet and because a
    # comparison against a rival has to be against the same column types.
    var session = Session()
    var frame = session.run(
        String(
            "SELECT l_shipdate, l_discount::DOUBLE AS l_discount, ",
            "l_quantity::DOUBLE AS l_quantity, ",
            "l_extendedprice::DOUBLE AS l_extendedprice ",
            "FROM read_parquet('",
            path,
            "')",
        )
    )
    var rows = len(frame)

    var dates = values[DType.int32](frame.columns[0].chunks[0])
    var discounts = values[DType.float64](frame.columns[1].chunks[0])
    var quantities = values[DType.float64](frame.columns[2].chunks[0])
    var prices = values[DType.float64](frame.columns[3].chunks[0])

    var span = (rows + tasks - 1) // tasks
    var partials = List[Float64](length=tasks, fill=Float64(0))

    def slice(task: Int) raises {mut partials, imm}:
        var start = task * span
        var stop = min(start + span, rows)
        var total = Float64(0)
        for i in range(start, stop):
            var discount = discounts.unsafe_offset(i).unsafe_load()
            var shipped = dates.unsafe_offset(i).unsafe_load()
            if (
                shipped >= SHIP_FROM
                and shipped < SHIP_TO
                and discount >= 0.05
                and discount <= 0.07
                and quantities.unsafe_offset(i).unsafe_load() < 24.0
            ):
                total += prices.unsafe_offset(i).unsafe_load() * discount
        partials[task] = total

    var best = 0
    var answer = Float64(0)
    for run in range(runs):
        var started = perf_counter_ns()
        parallel_for(slice, tasks)
        var total = Float64(0)
        for i in range(tasks):
            total += partials[i]
        var took = perf_counter_ns() - started
        if run == 0 or took < best:
            best = took
        answer = total

    print(String("min_ms,", Float64(best) / 1.0e6))
    print(String("rows,", rows))
    print(String("tasks,", tasks))
    print(String("revenue,", answer))
    # The pointers above are untracked, so nothing here keeps the frame alive
    # past its last named use, and without this the loop reads a freed
    # allocation and quietly sums zeroes.
    _ = frame^
