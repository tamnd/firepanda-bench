# The hand written answers

Ten of the 43 queries, answered at 1M by `tools/clickbench_hand.py`, which is a fourth implementation written from the SQL text in plain Python over the raw Parquet columns. It shares no planner, no dataframe library, no group by kernel and no type conversion with any of the three engines it checks.

This is what stands in for the validation output TPC-H comes with. `pixi run validate-tpch` compares all 66 TPC-H implementations against answers the TPC publishes, and that check found two real problems before any number here went out. ClickBench publishes no answers, so the strongest check in the repository does not exist for this suite. The cross engine fingerprint does not replace it: four engines agreeing means four ports made the same decision, and if the decision was wrong the agreement hides it.

The ten are the ones where a port had to make a decision rather than transcribe one. q4 and q8 count distinct exactly where the published ClickHouse entry estimates. q27 and q28 average a byte length that is not a character length. q28 also has a regular expression with a capture group, anchored at both ends, so a referer that does not match has to come back unchanged rather than empty. q34 groups by a constant. q22 puts a `NOT LIKE` next to an empty string comparison, which is the pair that stops being harmless the moment a reader turns an empty string into a null. q23 is the only `SELECT *`, so it is the only place every one of the 105 type conversions has to be right at once. q39 uses a `CASE` as a grouping key and has an OFFSET, which gives it two boundaries where a tie matters instead of one. q17 and q25 are two where the statement does not determine which rows come back.

## What each file holds

`statement` is the published SQL, copied from `../queries.sql`, and the whole file is about that one statement at that one size.

`columns` and `rows_out` are what every engine has to produce, because every statement here determines its own shape whatever else it leaves open.

`checked` names the columns whose values are compared, and `answer` holds them, sorted. For a query whose statement determines its answer that is every column. For one that does not it is the ordering key only, because the values in the key are determined even when which rows carry them is not: if nineteen rows tie at the cut in q25 then the ten search phrases that come back are fixed and the rows they came from are not. For q17 it is nothing, since the statement has no ORDER BY and any ten groups are a correct answer.

`ties` is what the ordering expression could not separate, as a mapping from the position the statement cuts at to how many rows share the key across that cut. Empty when the answer is determined.

`note` says in words what the file does and does not pin down, so that a reader who opens one file does not have to find this page to know what they are looking at.

## Running it

    pixi run validate-clickbench --engines pandas,polars,duckdb --size 1M

Three checks, in this order. The files are compared against a fresh run of the hand implementation, so a change to either side is a diff rather than a number that moved. Every engine is compared against the files. And for the queries that are not determined, every row an engine returned has to be a row of the full answer, which is what catches an engine that invented a row instead of picking a different legitimate one out of a tie.

The same command also recomputes the tie set for all 43 and reports it against `CLICKBENCH_UNDETERMINED` in `tools/queries.py`. That set is a fact about the size rather than about the queries, so it cannot be settled once and believed everywhere.

## Regenerating

    pixi run validate-clickbench --write --size 1M

Rewrites every file from the hand implementation. The review is the diff. A file that changed without the statement changing means either the data changed or the hand implementation did, and both of those are worth a look before the change is committed.

These are 1M. The answers to most of these queries are different at 10M and 100M, so a file here is not a claim about any other size.
