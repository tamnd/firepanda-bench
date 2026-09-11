# ClickBench

All 43 queries over one table of 99,997,497 rows and 105 columns of real anonymized web analytics traffic, published by ClickHouse at [ClickHouse/ClickBench](https://github.com/ClickHouse/ClickBench).

## Checking answers for a suite that publishes none

TPC-H has the validation output the TPC publishes with the specification, and `pixi run validate-tpch` checks all 66 implementations against it cell by cell. That check found two real problems before any number here was published. ClickBench publishes no answers, so this suite starts without the strongest check the repository has.

Three things stand in for it, and they are not equally strong.

The cross engine fingerprint runs on every measurement and compares the row count, the sum of each numeric column and an order independent digest of each text column. It says the ports agree. It does not say any of them is right, and that is the whole difference from TPC-H: there, an engine that agrees with the other three and disagrees with the published answers is wrong, and here there is nothing to disagree with. DuckDB runs the published SQL unmodified so it is the closest thing to an authority, and it is also one of the four engines in the table.

The exact check runs on a release. It reads every engine's answer back out of Arrow IPC and compares them row by row through the comparison layer in firepanda-compat, which cannot be fooled by a permutation the way a per column fingerprint can. All 43 pass at 1M across DuckDB, pandas and Polars, at the ACCUMULATION tolerance and with no registered differences. It is a stronger version of the same claim: the ports agree with each other more exactly than anybody thought to check.

`pixi run validate-clickbench` is the one that is not about agreement. Ten of the queries are written again in `tools/clickbench_hand.py`, in plain Python over the raw Parquet columns, sharing no planner, no dataframe library, no group by kernel and no type conversion with anything they check. The answers are in [`expected/`](expected/), which has its own page on what each file holds and why those ten. The same command recomputes which statements the data does not determine an answer for, which is the fourth trap below and a fact about the size rather than about the query.

## Five ways a port of this suite is quietly wrong

The traps have to be found by reading the queries rather than by running them, since running them is what produces the wrong answer that looks right.

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

That is a fact about the data rather than about the engine, so it can be computed rather than argued about: rank the whole answer by its own ordering expression and count the rows sharing the rank at the boundary. `pixi run validate-clickbench` does exactly that for all 43 and reports what it found against the list in `tools/queries.py`, so this is not a set somebody typed in once.

At 1M, twelve queries have an undetermined answer. q11 has 3 rows tied at the cut, q22 and q24 have 4, q18 and q30 have 5, q25 has 19, q39 has 177, q38 has 651, q31 has 69,354 because `WatchID` is nearly unique so almost every group has a count of one, and q32 has all of them, which at that size means the answer is any ten rows of the table. q17 has no ORDER BY at all. q40 is the one with different counts at its two boundaries, 13 and 10, which is the next paragraph.

A statement with an OFFSET cuts twice and either cut can tie. A tie at the offset decides which rows are skipped just as a tie at the limit decides which are kept, and both make the answer the engine's choice. q38, q39, q40 and q42 all have an OFFSET, and q40 ties 13 rows at the offset boundary and 10 at the limit boundary, so a check that only looked at the limit would have found the smaller of the two.

q26 is the one this list used to get wrong. It ends `ORDER BY EventTime, SearchPhrase LIMIT 10` and it was written down as having 2 rows tied at the cut, which came from looking at `EventTime` alone. The two columns together separate the boundary rows, rows nine and ten are a duplicate pair that both fit inside the limit, and all three engines return the same ten rows. It was being compared on its shape alone for no reason, which is the cheaper of the two mistakes and still a lost check on ten rows.

This cannot be fixed by making the ports agree, because agreeing would mean adding an order by the query does not have and then measuring a different query. So those twelve are flagged in `tools/queries.py`, the fingerprint comparison and the exact check both compare them on their row count and their column set rather than on their values, and both say so in their output. The report has its own section naming them, because a reader should be told which rows carry a weaker check rather than assume the suite is checked evenly.

The set is a function of the size, since which rows tie at row ten depends on how many rows there are. The list in `tools/queries.py` is 1M. Running the validator at 10M or 100M is how the list for those sizes gets written, and it prints the numbers rather than only saying the list is wrong.

The scheduled benchmark runs this suite at 10M, where that list is carried over rather than checked, and the report says so under the table instead of leaving a 1M number sitting next to a 10M one. A query that ties at 10M and not at 1M does not go quietly: its values are compared, the engines return different rows, and it comes out as a disagreement in the report, which is the loudest place in the file.

What is still checked in all twelve is the row count, the column names and the column types, all of which the statements do determine. An engine that answered one of them with five rows, or lost a column, or answered with an integer where everybody else has a date, is still caught. For the four of them that are in [`expected/`](expected/) there is more: the values of the ordering key are determined even when which rows carry them is not, and every row an engine returned has to be a row of the full answer, which catches an engine that invented a row instead of picking a different legitimate one out of a tie.

### Five: exact against approximate distinct counts

ClickHouse's published entry answers `COUNT(DISTINCT UserID)` with `uniq`, which is a HyperLogLog estimate. DuckDB, Polars and pandas all count exactly here, and the ports use `n_unique` and `nunique` rather than the approximate spellings each library also offers.

This does not affect our table, since all four of our engines are exact. It does affect anyone comparing our numbers against the published ones on q3, q4, q7, q8, q9, q10, q12 and q22, which is why it is written down. We count exactly everywhere and we do not reach for an estimate to improve a number.

## Where our numbers differ from the published table

ClickHouse publishes a results table and a methodology, and ours differs from it in five ways. None of the five makes a number here look better than it is, and all five are in the report as well as here, because somebody is going to put one of our numbers next to one from the public table and the difference between the two is partly the engine and partly this list.

They report the minimum of three runs and we report the median of ten with the interquartile range in the result file. A minimum is the cleanest run the machine gave you and it is the standard in that table. A median with its spread is a measurement. Neither is wrong and they are not the same statistic.

They run each query three times and publish cold, warm and hot separately, and we run warm. Cold cache behaviour is measured in the ingestion suite instead, with the page cache dropped between runs, because mixing it into a query suite makes every number in that suite partly a measurement of the filesystem.

They report load time and data size on disk and we do not, for any suite. Both are real parts of the published table and neither is something this harness has ever measured. The report says the column is absent rather than leaving a reader to assume our engines loaded instantly.

Their table is one machine type, c6a.4xlarge, and ours is two, neither of which is that one. We publish the AMD EPYC VPS and the i9-13900K desktop, never averaged, with the machine in the result file. The ratio between two of our own engines is the only thing that transfers between machines.

We also run 1M and 10M, which are not ClickBench. They exist for CI and for machines that cannot hold the hundred million row table, and the report labels a partial size in the heading and again above the table rather than in a footnote underneath it.

The sixth difference is not about the harness. It is the exact against approximate distinct count in the fifth trap above, and it costs us rather than helps us on all eight queries it touches.

Submitting a firepanda entry upstream to ClickHouse/ClickBench is the right end state. It needs a firepanda that reads Parquet on its own and runs all 43, so not yet.

## Where the queries come from

`suites/clickbench/queries.sql` is a byte for byte copy of what ClickHouse/ClickBench commits at `duckdb/queries.sql`, and nothing in this repository knows what any of them say. DuckDB runs each line with the semicolon removed, which is checked by a test, and the file's digest is pinned by another. The pandas and Polars ports were each written against that text and are compared against DuckDB's answer per query.

They are numbered q0 through q42 because ClickBench numbers from zero, so our q22 and a published q22 are the same query.
