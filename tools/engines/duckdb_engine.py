"""DuckDB, which is a correctness oracle as well as a competitor.

Every query goes through `CREATE OR REPLACE TABLE ans AS SELECT`, which is what
ClickHouse and DuckDB Labs use in db-benchmark and for the same reason: it forces
a lazy engine to materialize, and without it a query that returns a cursor
measures planning.

The tables are registered as Arrow views rather than copied into DuckDB storage,
so the load step costs what it costs everyone else and the query is not being run
against a format nobody else was given.

ClickBench is the one suite where the SQL is not written here. TPC-H already gets
its statements from DuckDB's own extension rather than from anything typed in this
repository, for the obvious reason that a benchmark you transcribed is a benchmark
you can get wrong in your favour. ClickBench publishes no extension, so its 43
statements are vendored verbatim under `suites/clickbench/queries.sql` and read
from there. Nothing in this file knows what any of them say.
"""

from __future__ import annotations

from pathlib import Path

import clickbench
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import queries

NAME = "duckdb"


def version() -> str:
    """Returns the installed version.

    Returns:
        The version string.
    """
    return duckdb.__version__


def sql_literal(value: str) -> str:
    """Escapes a string for inlining into a SQL single quoted literal.

    Only reached with a path this harness generated, and here anyway because a
    hand rolled quote that is correct for the paths we happen to use is the kind
    of thing that stops being correct quietly.

    Args:
        value: The string.

    Returns:
        The string with single quotes doubled.
    """
    return value.replace("'", "''")


def load(paths: dict[str, str], suite: str = "db-benchmark", io: str = "memory") -> dict:
    """Reads the tables and registers them on a connection.

    In memory mode the tables are read as Arrow and registered, which is what
    every other engine gets. In scan mode they become Parquet views and the read
    happens inside the timed region, where DuckDB can skip row groups.

    The one thing that is never done is loading into DuckDB's own storage format.
    That would be measuring DuckDB against a layout nobody else was given, and the
    result would say more about the loader than about the engine.

    Args:
        paths: A mapping from table name to a Parquet path.
        suite: Which suite is being run.
        io: How the tables should reach the engine.

    Returns:
        A context holding the connection and keeping the Arrow tables alive, or
        the paths for the ingestion suite, where reading the file is the thing
        being timed.
    """
    connection = duckdb.connect()
    if suite == "ingestion":
        return {"con": connection, "tables": {}, "paths": dict(paths)}
    if suite == "tpch":
        connection.execute("INSTALL tpch")
        connection.execute("LOAD tpch")
    if suite == "clickbench":
        return load_clickbench(connection, paths["hits"], io)
    if io == "scan":
        for name, path in paths.items():
            # The path is inlined rather than bound. DuckDB refuses to prepare a
            # DDL statement that carries a parameter, with "Unexpected prepared
            # parameter. This type of statement can't be prepared", so the bound
            # form failed on every table of every query and scan mode had no
            # DuckDB column at all. It read as an engine that skipped the suite.
            #
            # The name is quoted for the same reason the joins are: the
            # db-benchmark tables are called `left` and `right_small`, and LEFT is
            # a reserved word.
            connection.execute(
                f'CREATE OR REPLACE VIEW "{name}" AS '
                f"SELECT * FROM read_parquet('{sql_literal(path)}')"
            )
        return {"con": connection, "tables": {}}
    tables = {name: pq.read_table(path) for name, path in paths.items()}
    for name, table in tables.items():
        connection.register(name, table)
    return {"con": connection, "tables": tables}


# The published schema, expressed as a projection over what the file actually
# holds. `make_date` on a count of days and `epoch_ms` on a count of seconds are
# ClickBench's own conversions, copied from its DuckDB loader rather than worked
# out here, and `clickbench.retype` is the same four conversions in Arrow for the
# side that loads into memory first.
CLICKBENCH_PROJECTION = (
    "* REPLACE ("
    "make_date(EventDate) AS EventDate, "
    "epoch_ms(EventTime * 1000) AS EventTime, "
    "epoch_ms(ClientEventTime * 1000) AS ClientEventTime, "
    "epoch_ms(LocalEventTime * 1000) AS LocalEventTime)"
)


def load_clickbench(connection, pattern: str, io: str) -> dict:
    """Registers the hits table under the types the published queries expect.

    Both modes end with a `hits` whose columns have the same types, which is the
    whole point of the function. In scan mode that means a view with the
    conversions in its projection, where DuckDB can push the filter and the column
    list into the Parquet reader and do them on the rows that survive. In memory
    mode the conversion happens in Arrow before the table is registered, so the
    timed region does not include it.

    Doing it in Arrow rather than wrapping the registered table in the same view is
    deliberate. A view over an Arrow table would cast a hundred million binary
    values to text inside every one of the 43 timed queries, which is work no other
    engine is doing and which would land in the number as if it were query
    execution.

    `binary_as_string` is what turns the text columns into text. Without it six of
    the 43 fail to bind at all and another nine answer with bytes where they should
    answer with strings, and the second group is the dangerous one: the row counts
    are right, the values are right, and the digest this harness compares engines on
    hashes bytes and text the same way, so the agreement check passes.

    Args:
        connection: The open DuckDB connection.
        pattern: The partition glob for the hits table.
        io: How the table should reach the engine.

    Returns:
        A context holding the connection and keeping the Arrow table alive.
    """
    if io == "scan":
        connection.execute(
            f"CREATE OR REPLACE VIEW hits AS SELECT {CLICKBENCH_PROJECTION} "
            f"FROM read_parquet('{sql_literal(pattern)}', binary_as_string=True)"
        )
        return {"con": connection, "tables": {}}
    table = clickbench.retype(pq.read_table(clickbench.partitions(pattern)))
    connection.register("hits", table)
    return {"con": connection, "tables": {"hits": table}}


def run_sql(ctx: dict, sql: str) -> pa.Table:
    """Runs one statement and materializes its answer.

    Args:
        ctx: The context from `load`.
        sql: The select statement.

    Returns:
        The answer as an Arrow table.
    """
    connection = ctx["con"]
    connection.execute(f"CREATE OR REPLACE TABLE ans AS {sql}")
    return connection.execute("SELECT * FROM ans").arrow()


SQL = {
    "q1": "SELECT id1, sum(v1) AS v1 FROM groupby GROUP BY id1",
    "q2": "SELECT id1, id2, sum(v1) AS v1 FROM groupby GROUP BY id1, id2",
    "q3": "SELECT id3, sum(v1) AS v1, avg(v3) AS v3 FROM groupby GROUP BY id3",
    "q4": ("SELECT id4, avg(v1) AS v1, avg(v2) AS v2, avg(v3) AS v3 FROM groupby GROUP BY id4"),
    "q5": ("SELECT id6, sum(v1) AS v1, sum(v2) AS v2, sum(v3) AS v3 FROM groupby GROUP BY id6"),
    "q6": (
        "SELECT id4, id6, median(v3) AS v3_median, stddev(v3) AS v3_sd "
        "FROM groupby GROUP BY id4, id6"
    ),
    "q7": ("SELECT id3, max(v1) - min(v2) AS range_v1_v2 FROM groupby GROUP BY id3"),
    "q8": (
        "SELECT id6, v3 FROM (SELECT id6, v3, row_number() OVER "
        "(PARTITION BY id6 ORDER BY v3 DESC) AS rank FROM groupby) t "
        "WHERE rank <= 2"
    ),
    "q9": ("SELECT id2, id4, pow(corr(v1, v2), 2) AS r2 FROM groupby GROUP BY id2, id4"),
    "q10": (
        "SELECT id1, id2, id3, id4, id5, id6, sum(v3) AS v3, count(*) AS count "
        "FROM groupby GROUP BY id1, id2, id3, id4, id5, id6"
    ),
    # The db-benchmark tables are called `left`, `right_small`, `right_medium` and
    # `right_big`, and LEFT and RIGHT are reserved words, so the table name is
    # quoted in every join. Unquoted it parses as the start of a join clause and
    # the error points at the alias rather than at the table, which is how this
    # went unnoticed until every join in the suite failed at once.
    #
    # `rows` is quoted for the same reason: it is a window frame keyword.
    "j1": (
        'SELECT count(*) AS "rows", sum(l.v1) AS v1, sum(r.v2) AS v2 '
        'FROM "left" l JOIN right_small r USING (id1)'
    ),
    "j2": (
        'SELECT count(*) AS "rows", sum(l.v1) AS v1, sum(r.v2) AS v2 '
        'FROM "left" l JOIN right_medium r USING (id2)'
    ),
    # j3 is j2 with the join made outer, and on this data that changes nothing
    # about the answer: id2 is uniform over one to a million on both sides, every
    # left row matches, and the two queries return the same hundred million rows
    # and the same two sums to the last digit. They do not take the same time.
    # On a 13900K at 5GB the inner join is 0.386 to 0.474 s and the outer is
    # 0.167 to 0.217, which is backwards.
    #
    # EXPLAIN ANALYZE says why, and it is worth knowing before reading the j2
    # row as a firepanda win. The inner plan pushes a dynamic filter into the
    # left table scan, `optional: id2>=1 AND id2<=1000000`, built from the build
    # side's minimum and maximum. The outer plan has none, because an outer join
    # cannot drop probe rows so there is nothing to push down. That filter is
    # usually a good idea and here it rejects nothing at all, so it is a hundred
    # million comparisons for no rows saved: the scan goes from 0.61 to 0.83
    # cumulative thread seconds and the join operator from 5.53 to 12.11.
    #
    # So DuckDB's honest cost for a join of this shape is the outer number, and
    # any engine compared against the inner one is being compared against a plan
    # DuckDB pessimized itself. Nothing is changed here to work around it. The
    # query is what upstream runs and rewriting it to dodge another engine's
    # optimizer would be a worse kind of unfair than reporting it.
    "j3": (
        'SELECT count(*) AS "rows", sum(l.v1) AS v1, sum(r.v2) AS v2 '
        'FROM "left" l LEFT JOIN right_medium r USING (id2)'
    ),
    # j4 is j2 on the character key, which is the one join upstream runs on text.
    "j4": (
        'SELECT count(*) AS "rows", sum(l.v1) AS v1, sum(r.v2) AS v2 '
        'FROM "left" l JOIN right_medium r USING (id5)'
    ),
    "j5": (
        'SELECT count(*) AS "rows", sum(l.v1) AS v1, sum(r.v2) AS v2 '
        'FROM "left" l JOIN right_big r USING (id3)'
    ),
    "j6": (
        'SELECT count(*) AS "rows", sum(l.v1) AS v1, sum(r.v2) AS v2 '
        'FROM "left" l LEFT JOIN right_big r USING (id3)'
    ),
}


def _make(name: str):
    """Builds the callable for one query.

    Args:
        name: The query name.

    Returns:
        A function taking the context and returning the answer.
    """

    def run(ctx: dict) -> pa.Table:
        """Runs the query.

        Args:
            ctx: The context from `load`.

        Returns:
            The answer.
        """
        return run_sql(ctx, SQL[name])

    run.__name__ = name
    return run


QUERIES = {name: _make(name) for name in SQL}


def official_tpch_sql() -> dict[str, str]:
    """Reads the twenty two official TPC-H statements out of the extension.

    The query text is not written down in this repository on purpose. The
    substitution parameters are part of the specification, and a query with the
    wrong date literal is a different query wearing the same name. Reading them
    from the extension means the SQL engine runs the specification and the
    dataframe engines are checked against the specification's own answers.

    Returns:
        A mapping from query name to SQL, empty if the extension is unavailable.
    """
    try:
        connection = duckdb.connect()
        connection.execute("INSTALL tpch")
        connection.execute("LOAD tpch")
        rows = connection.execute(
            "SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr"
        ).fetchall()
    except Exception:
        return {}
    return {f"q{int(number)}": text for number, text in rows}


TPCH_SQL = official_tpch_sql()


def _make_tpch(name: str):
    """Builds the callable for one TPC-H query.

    Args:
        name: The query name.

    Returns:
        A function taking the context and returning the answer.
    """

    def run(ctx: dict) -> pa.Table:
        """Runs the query.

        Args:
            ctx: The context from `load`.

        Returns:
            The answer.
        """
        return run_sql(ctx, TPCH_SQL[name].rstrip().rstrip(";"))

    run.__name__ = f"tpch_{name}"
    return run


TPCH_QUERIES = {name: _make_tpch(name) for name in TPCH_SQL}


# The vendored copy of ClickBench's `duckdb/queries.sql`. Byte identical to the
# file that repository commits, so refreshing it is a copy and a diff means the
# benchmark changed. It has not changed since November 2022. Refresh with:
#
#   curl -fsSL https://raw.githubusercontent.com/ClickHouse/ClickBench/main/duckdb/queries.sql \
#     -o suites/clickbench/queries.sql
#
# Vendored rather than fetched, for the same reason `tools/cost-matrix.json` is: a
# benchmark whose queries arrive over the network is a benchmark that measures
# something different depending on the day you ran it.
CLICKBENCH_SQL_PATH = Path(__file__).resolve().parents[2] / "suites" / "clickbench" / "queries.sql"


def clickbench_sql() -> dict[str, str]:
    """Reads the published statements out of the vendored file.

    One statement per line, in published order, which is the format the file is in
    and the reason it can be used as it stands rather than parsed. The trailing
    semicolon comes off because `run_sql` wraps the text in a `CREATE TABLE ans AS`
    and a semicolon in the middle of that is a syntax error.

    Returns:
        A mapping from `q0` through `q42` to the statement.

    Raises:
        SystemExit: If the file does not hold 43 statements, which means the copy
            is stale or somebody edited it.
    """
    statements = [
        line.strip().rstrip(";")
        for line in CLICKBENCH_SQL_PATH.read_text().splitlines()
        if line.strip()
    ]
    if len(statements) != 43:
        raise SystemExit(
            f"{CLICKBENCH_SQL_PATH} holds {len(statements)} statements and ClickBench "
            "publishes 43. Refresh it from the URL in the comment above."
        )
    return {f"q{index}": statement for index, statement in enumerate(statements)}


CLICKBENCH_SQL = clickbench_sql()


def _make_clickbench(name: str):
    """Builds the callable for one ClickBench query.

    Args:
        name: The query name.

    Returns:
        A function taking the context and returning the answer.
    """

    def run(ctx: dict) -> pa.Table:
        """Runs the query.

        Args:
            ctx: The context from `load`.

        Returns:
            The answer.
        """
        return run_sql(ctx, CLICKBENCH_SQL[name])

    run.__name__ = f"clickbench_{name}"
    return run


CLICKBENCH_QUERIES = {name: _make_clickbench(name) for name in CLICKBENCH_SQL}


# The neutral names in `queries.NARROW_SCHEMA`, in DuckDB.
DUCKDB_TYPES = {"int64": "BIGINT", "float64": "DOUBLE", "string": "VARCHAR"}


def read_one(ctx: dict, table: str, columns: dict | None = None):
    """Reads one CSV file into a DuckDB table and hands the rows back.

    The read lands in a real table rather than a view, for the same reason every
    other query here ends in `CREATE OR REPLACE TABLE`: a view would return a
    cursor and measure planning.

    What is returned is the relation over that table rather than its Arrow
    conversion, which is what `run_sql` would have done. The other two Python
    engines hand back their own frame here so the conversion falls outside the
    timing, and DuckDB gets the same treatment; the harness pulls the rows out
    afterwards. The `CREATE TABLE` has already forced the whole file through the
    parser by the time this returns, so nothing about the read is deferred.

    Args:
        ctx: The connection and paths from `load`.
        table: Which file.
        columns: Declared types, or None to let the sniffer work them out.

    Returns:
        A relation over the rows that were read.
    """
    path = sql_literal(ctx["paths"][table])
    if columns is None:
        source = f"read_csv('{path}')"
    else:
        declared = ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items())
        source = f"read_csv('{path}', header=true, auto_detect=false, columns={{{declared}}})"
    connection = ctx["con"]
    connection.execute(f"CREATE OR REPLACE TABLE ans AS SELECT * FROM {source}")
    return connection.sql("SELECT * FROM ans")


INGESTION_QUERIES = {
    "csv_narrow": lambda ctx: read_one(ctx, "narrow"),
    "csv_narrow_typed": lambda ctx: read_one(
        ctx, "narrow", {name: DUCKDB_TYPES[kind] for name, kind in queries.NARROW_SCHEMA}
    ),
    "csv_wide": lambda ctx: read_one(ctx, "wide"),
    "csv_quoted": lambda ctx: read_one(ctx, "quoted"),
    "csv_nulls": lambda ctx: read_one(ctx, "nulls"),
}
