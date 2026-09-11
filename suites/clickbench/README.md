# ClickBench

All 43 queries over one table of 99,997,497 rows and 105 columns of real anonymized web analytics traffic, published by ClickHouse at [ClickHouse/ClickBench](https://github.com/ClickHouse/ClickBench).

## Five ways a port of this suite is quietly wrong

TPC-H has the validation output the TPC publishes with the specification, and `pixi run validate-tpch` checks all 66 implementations against it cell by cell. That check found two real problems before any number here was published. ClickBench publishes no answers, so this suite starts without the strongest check the repository has, and the traps have to be found by reading the queries rather than by running them.

These five were written down before the ports were written rather than after. Each one produces a plausible table quickly, and a fast wrong answer is worse than no answer.

### One: a string column that is not a string column

The text columns in `hits.parquet` are `BYTE_ARRAY` with no string logical type on them. DuckDB needs `binary_as_string=True` or every one of them comes back as bytes, and pandas and Polars reading the same file make their own guesses. If two engines guess differently then `LIKE '%google%'` matches a hundred thousand rows in one and zero in the other, both quickly, both with the right output shape.

Measured rather than assumed: without the conversion six of the 43 fail to bind at all, which is loud, and another nine answer with bytes where they should answer with text, which is not. The row counts are right, the values are right, and the cross engine fingerprint hashes bytes and text through the same function, so the agreement check passes and the table looks finished.

So all four engines are handed text, every engine's loader calls `clickbench.check_text` with its own idea of which columns it ended up with, and a load that skipped the conversion stops there instead of producing a number. It is the cheapest test in this repository.

### Two: how long is a string

q27 and q28 average `STRLEN`. In DuckDB that is a byte count. `Series.str.len` in pandas is a character count, and Polars spells the two differently and will give you either. The `URL` and `Referer` columns are real web addresses with percent encoding and non ASCII in them, so the two counts differ on real rows: the averages over the real `URL` column are 88.56 bytes and 86.57 characters, which is over two percent.

Bytes wins, because bytes is what the reference engine computes. pandas gets `pyarrow.compute.binary_length` and Polars gets `str.len_bytes`, each with a comment beside it saying why the obvious spelling is the wrong one. Both queries sit behind a `HAVING COUNT(*) > 100000` that no fixture small enough for CI can reach through the query, so each port has a test that runs DuckDB's own `STRLEN` beside its byte count, and a second one asserting the two counts differ on the fixture so the first cannot quietly become a tautology.

### Three: an empty string is not a null

Eleven queries filter on `SearchPhrase <> ''` or `URL <> ''` or `MobilePhoneModel <> ''`. In this dataset the empty string is what a missing value looks like and there are no nulls at all, in any of the 105 columns, which is checked from the Parquet footers rather than believed.

An engine or a reader that helpfully converts empty strings to nulls changes the meaning of `<> ''` under three valued logic, because a null compared against anything is null and the row is dropped by the filter. Here that happens to be the same set of rows. It stops being the same set the moment a comparison is negated or a distinct count is taken, and q22's `NOT LIKE` is exactly that shape.

So nothing converts an empty string to a null anywhere in the loading path, and the dataset manifest records a null count per column. A reader that started doing it shows up as a changed count in the manifest rather than as a changed answer in a table.

### Four: ten rows out of a tie

q17 is `GROUP BY UserID, SearchPhrase LIMIT 10` with no order by at all. Thirty two of the 43 end `ORDER BY something LIMIT 10`, and the answer is determined only when the ordering expression can tell the last included row apart from the first excluded one. On this data it frequently cannot.

That is a fact about the data rather than about the engine, so it can be computed: rank the whole answer by its own ordering expression and count the rows sharing the rank at the boundary. At the 1M size, thirteen queries have an undetermined answer. q26 has 2 rows tied at the cut, q11 has 3, q22 and q24 have 4, q18 and q30 have 5, q40 has 10, q25 has 19, q39 has 177, q38 has 651, q31 has 69,354 because `WatchID` is nearly unique so almost every group has a count of one, and q32 has all of them, which at that size means the answer is any ten rows of the table.

This cannot be fixed by making the ports agree, because agreeing would mean adding an order by the query does not have and then measuring a different query. So those thirteen are flagged in `tools/queries.py`, the fingerprint comparison and the exact check both compare them on their row count and their column set rather than on their values, and both say so in their output. The report has its own section naming them, because a reader should be told which rows carry a weaker check rather than assume the suite is checked evenly.

The set is a function of the size, since which rows tie at row ten depends on how many rows there are. The list in `tools/queries.py` was measured at 1M and #47 is where it gets computed against whichever dataset a run actually used.

What is still checked in all thirteen is the row count, the column names and the column types, all of which the statements do determine. An engine that answered one of them with five rows, or lost a column, or answered with an integer where everybody else has a date, is still caught.

### Five: exact against approximate distinct counts

ClickHouse's published entry answers `COUNT(DISTINCT UserID)` with `uniq`, which is a HyperLogLog estimate. DuckDB, Polars and pandas all count exactly here, and the ports use `n_unique` and `nunique` rather than the approximate spellings each library also offers.

This does not affect our table, since all four of our engines are exact. It does affect anyone comparing our numbers against the published ones on q3, q4, q7, q8, q9, q10, q12 and q22, which is why it is written down. We count exactly everywhere and we do not reach for an estimate to improve a number.

## Where the queries come from

`suites/clickbench/queries.sql` is a byte for byte copy of what ClickHouse/ClickBench commits at `duckdb/queries.sql`, and nothing in this repository knows what any of them say. DuckDB runs each line with the semicolon removed, which is checked by a test, and the file's digest is pinned by another. The pandas and Polars ports were each written against that text and are compared against DuckDB's answer per query.

They are numbered q0 through q42 because ClickBench numbers from zero, so our q22 and a published q22 are the same query.
