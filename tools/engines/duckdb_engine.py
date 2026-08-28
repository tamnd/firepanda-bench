"""DuckDB, which is a correctness oracle as well as a competitor.

Every query goes through `CREATE OR REPLACE TABLE ans AS SELECT`, which is what
ClickHouse and DuckDB Labs use in db-benchmark and for the same reason: it forces
a lazy engine to materialize, and without it a query that returns a cursor
measures planning.

The tables are registered as Arrow views rather than copied into DuckDB storage,
so the load step costs what it costs everyone else and the query is not being run
against a format nobody else was given.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

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
        A context holding the connection and keeping the Arrow tables alive.
    """
    connection = duckdb.connect()
    if suite == "tpch":
        connection.execute("INSTALL tpch")
        connection.execute("LOAD tpch")
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
    "j3": (
        'SELECT count(*) AS "rows", sum(l.v1) AS v1, sum(r.v2) AS v2 '
        'FROM "left" l LEFT JOIN right_medium r USING (id2)'
    ),
    "j4": (
        'SELECT count(*) AS "rows", sum(l.v1) AS v1, sum(r.v2) AS v2 '
        'FROM "left" l JOIN right_big r USING (id3)'
    ),
    "j5": (
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
