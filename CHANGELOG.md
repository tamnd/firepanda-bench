# Changelog

Versions here track the harness, not the engines it measures and not firepanda itself. A change that alters what a published number means gets a minor bump, because a reader comparing two result files needs to know whether the measurement changed under them.

## Unreleased

### What a group by adds on top of the load is measured too

`pixi run group-memory --size 1M` is the sibling of `load-memory`. It loads the hits table, reads the peak resident set, runs q32 once and reads the peak again, one process per engine, and reports the difference. q32 groups by `WatchID` and `ClientIP` with no filter, so the answer has close to one row per row of input, and that is the one shape in the suite where a group by is an allocation question rather than a throughput one.

The difference between the two readings is the only part of a process peak that can be attributed to a query. A peak never falls, so comparing the raw peaks would mostly compare the loaders, which `load-memory` already does on purpose.

At 1M the group by adds 0.15 GB for pandas and 0.11 GB for duckdb, and nothing measurable for firepanda and polars. Those two zeros are bounds rather than measurements, because both engines' loads reached higher than anything their query did, and that is said in the table rather than left to be read as a group by that costs nothing. What a zero does rule out is a second copy of the table, which would have been a gigabyte and would have shown. The size that would settle it is 10M, and it does not fit on the laptop these were taken on, so it waits on tamnd/firepanda#427 or on the bench machines.

The driver now prints `peak_rss_after_load_bytes` next to the peak it already printed, which is where the first of the two readings comes from. It costs nothing, since the driver was already reading the process facts at that point and reporting only the current resident set out of them, which is zero on a platform with no `/proc`.

## v0.4.2

A patch. No published number changes and there is a new one that was never published before, which is what an engine pays to get the table into memory at all.

### What the load costs is measured on its own

Every other number in this repository is a query, and on the hits table the load is the largest allocation any engine makes. The peak resident set a timed run reports is the high water mark of the whole process, so the load is buried inside it rather than reported by it. `pixi run load-memory --size 1M` runs each engine in a process of its own, times nothing but the load, and prints what each one peaked at and what it had already allocated before it opened the file.

At 1M the payload is 0.73 GB of Arrow once the three conversions have been applied. firepanda peaks at 3.16 GB, polars at 2.43, duckdb at 1.31 and pandas at 1.23. firepanda is 4.3 times the payload against pandas' 1.5, and the reason is the path rather than the frame: firepanda has no Parquet decoder, so DuckDB decodes the file into its own vectors, hands them over as Arrow, and firepanda copies out of that into its own arrays, which is three representations of one table with at least two of them resident at once. The table is in `suites/clickbench/README.md` and the finding belongs to tamnd/firepanda#405.

### The driver says what it loaded

`--schema=clickbench` loads the table, prints all 105 column names and types on one JSON line along with the load time and the peak, and stops. That is what the load measurement reads, and it is also what `tests/test_clickbench_schema.py` checks the driver against column by column, with the expected schema read out of the file's own footer rather than transcribed into the test.

The 28 text columns are why that test exists. The file stores them as BYTE_ARRAY with no logical type on them, nine of the 43 queries read one, and an engine that leaves them as bytes does not fail: it matches nothing, returns an empty answer and reports it as a result. `check_text` guards that at load time for the three Python engines, and firepanda is a separate binary that cannot call it. All 105 columns arrive at the published types today, so this is a guard rather than a fix.

`published_type` and `published_schema` in `tools/clickbench.py` hold the three conversions ClickBench's own loader makes, and `retype` goes through the first of them, so the function that converts the data and the function that says what the data should be cannot drift apart.

Closes tamnd/firepanda#478.

In #78.

## v0.4.1

A patch. Nothing here changes what a published number means, and one number got a lot better.

### q23 stopped building a frame it was going to throw away

firepanda's worst ClickBench query was the one that asks for the table rather than a reduction of it. A pattern match over `URL` keeps ninety five rows out of a million at 1M and ten of those are the answer, and the driver was building the filtered frame first, which means gathering 105 columns a million rows at a time to read ten rows out of the result.

tamnd/firepanda#582 added `DataFrame.filter_sort_limit`, which carries positions through the filter and the sort and gathers the wide columns once at the rows the limit left. The driver calls that now. At 1M over three runs the query goes from 0.053 s to 0.008 s, which is level with Polars and four times ahead of DuckDB.

All 43 queries still agree, all three engines, same digests.

In #76.

## v0.4.0

A minor bump, and the reason is the agreement check rather than the suite. A ClickBench result file written before this reads differently after it, because the check that decides whether four engines got the same answer was reading a third of the suite's columns as zero.

### ClickBench runs on all four engines

firepanda has a cell in all 43 rows now. `engines/firepanda/clickbench.mojo` answers 42 of them and the harness asks the driver which ones with `--list=clickbench` rather than keeping a second copy of the list that drifts out of step.

q28 is the only refusal. It needs a regular expression with a capture group, firepanda has no regular expression engine, and the driver raises with that reason and names tamnd/firepanda#480 rather than answering something close. The estimate when the suite shipped was 31 of 43, and the six rows in that table that predicted a gap were mostly wrong: the minimum over a text column, the byte length per element, the offset under a limit and the conditional answering a text column all exist in the library already. q23 answers by materializing the 105 column frame, which is slow and correct rather than fast and missing.

ClickBench is loaded the way TPC-H is. firepanda reads Parquet by handing the file to DuckDB, so the table is loaded before the clock starts, exactly as pandas and Polars are handed theirs under memory mode, and scan mode is refused by name because timing that load would be timing DuckDB in a table where DuckDB is one of the four engines. That is a real hole at 100M and closing it is the native Parquet reader in the library.

Closes #45.

### The agreement check was reading small integers as zero

`column_sum` in the firepanda driver had an arm for int32, int64, uint32, uint64, float32 and float64 and for nothing narrower. TPC-H never produces a small integer and ClickBench produces them everywhere, so a column of them summed to zero.

What that did was report q7, q14 and q23 as disagreements on answers that were already correct, which is the harmless direction. The other direction is what makes this a minor bump: a query whose only real difference was in a small integer column would have been reported as agreeing. Every published number in this repository rests on that check, so a hole in it is worth a version number even though no number moved. int8, int16, uint8, uint16 and bool are summed now.

All four engines agree on all 43 queries at 1M after it, 31 of them value by value and 12 on shape alone because their statements do not determine which rows come back at that size.

## v0.3.6

A patch. Nothing here changes what a published number means and no result file a reader has ever seen reads differently after it. What changed is that the scheduled run produces the files the front page says it produces.

### The ingestion suite runs on the schedule

The ingestion suite has been wired up and runnable for a while and was never in the benchmark matrix, so there has never been a scheduled ingestion result file. The front page describes a full run as fourteen files and counts ingestion in that number, and the run was producing twelve. It is in the matrix now, with its size set in a step rather than in the matrix, so the scheduled size and the harness default stay one number, which is the arrangement ClickBench already uses.

The io input is overridden for this job. The harness refuses a memory mode on this suite and is right to: handing every engine the same Arrow table on a suite that times the CSV reader would mean reading the file before timing the read. That refusal belongs at a terminal and not here, since memory is what the dispatch form defaults to, so a scheduled run would fail one job in four every week to say something the suite already knows. The job forces scan and writes a notice saying it did.

Measured before turning it on. At 10M the four files are about 1.5 GB of CSV, generated in roughly two and a half minutes, and at 1M with three engines and three runs the benchmark takes 47 seconds, which scales to well inside the job timeout.

Closes #69.

## v0.3.5

A patch. Nothing here changes what a published number means and no result file a reader has ever seen reads differently after it. What changed is what a reader sees when they arrive: the READMEs now carry the numbers, and ClickBench is on the front page.

### ClickBench is documented like a suite somebody has to run, and the front page admits it exists

The suite had a README covering the five ways a port of it goes quietly wrong and the five ways our methodology differs from the published table, which is the hard part, and it never said the easy part. It did not say what the suite is for next to the three that were already here, where the data comes from, which engines run it, what the two io modes cost on a table with 105 columns, or which queries are worth looking at first. Those five sections are there now, along with a license note, because a reader arriving at a suite wants to know what it measures before they want to know how a port of it goes wrong.

The repository README had not been told ClickBench landed. Two real public suites and one of our own is now three and one, the badge lists it, the suite table has a fourth row, and a full run is fourteen result files rather than ten. The sentence that said firepanda runs 15 of 15 db-benchmark queries, 0 of 22 TPC-H queries and 5 of 5 ingestion queries is gone, replaced by the reasons for the gaps and a pointer at the generated coverage column, because a count in prose is a count somebody has to remember to edit and this one had been wrong since ClickBench merged. The license section names ClickBench as Apache-2.0 from ClickHouse, the hits dataset as ClickHouse's own under its own terms and downloaded rather than redistributed, and these runs as ours rather than an entry in the published table.

### The READMEs carry the numbers, written by a script rather than typed

Ten result files a week went into a workflow artifact, a step summary and a static page, and not one of those is what somebody looks at when they arrive. They open the repository and they read a README, and what the suite READMEs said was what the suite is and how the port was done, which is the right thing to say and is not a number.

Each suite README now has a block between two markers with one table per machine, size and io mode. Per engine it gives coverage, the median, the ninety ninth percentile over that median, throughput in input rows per second, peak resident set and CPU seconds with the cores those imply, each with its ratio against pandas in the direction the rest of the repository uses, where above one is better. The front page has one row per suite from the largest run of it. `pixi run suite-readme` writes all of it.

Throughput is input rows rather than answer rows, because a query returning ten rows out of a hundred million did not do ten rows of work, and it is taken from the tables the query actually reads rather than from the suite row count, because the ingestion suite's wide file is a tenth of the height of its narrow one. Every number in a table is a geometric mean over the same set of queries, the ones every engine that answered agreed on, and every ratio is taken per query and then averaged rather than by dividing two columns, so an engine cannot improve one by refusing the query it is slowest on. The coverage column is where a refusal shows.

Nothing is aggregated across a machine, a size or an io mode, and each table says which of the four it is from, because a mean over two of them is a number about neither.

### The scheduled run opens a pull request with the numbers rather than pushing them

`results/*.json` is gitignored and the publish job commits nothing back, which is a rule worth keeping: a workflow with a push credential is a different security question than one without, and a number that lands in a commit nobody reviewed is a number nobody checked.

So the weekly run regenerates the blocks from the run it just did and opens a pull request with the two paths it touched. Between runs the README is the last state a human looked at and says which run it came from, which is more honest than a number that could have changed under the reader an hour ago. The step that has the credential is written out in the workflow rather than delegated to a third party action, because it is the only job in this repository that can write to it.

`pixi run suite-readme --check` fails when a block has been edited by hand, which it can tell from the digest in the closing marker with no result files in the checkout at all, and CI runs that. A suite with no result file keeps whatever its block already said rather than being emptied, so running the script on a fresh checkout does not throw away the last reviewed numbers.

### The ClickBench operation declarations are read off the port rather than off the SQL

Every query in this repository declares what it is made of in pandas names, so a reader who sees a query lose can look the operation up in the firepanda-compat cost matrix rather than guessing which part of it was slow. The ClickBench declarations were written before the pandas port existed, off the published SQL, with a comment saying they would be re-derived from the port once there was one. There is one now.

Six entries changed, and every one of them is a place where what the SQL reads like and what pandas does are not the same thing. q1 and q20 count matching rows by summing a boolean mask rather than by filtering a frame and taking its length. q28 pulls its capture group out with a replace and a backreference rather than with an extract, because the query replaces the whole string. q34 adds its constant column to the ten rows that survive the limit, so it is q33 exactly, which is the point of the pair. q35 writes its three derived keys with an assignment rather than with `assign`. And the limit is an `iloc` slice on every query that has one except q17, because the port takes a limit and an offset through one helper and that helper slices.

That last one is the difference between five queries pointing at a missing matrix row and 33 doing it.

### Five holes in the cost matrix, filed rather than listed

The point of declaring operations is that the last column of the table is a hole over there with a query attached to it. ClickBench found five: taking rows at an offset, a minimum over a column, an ungrouped distinct count, a conditional expression producing a group key, and reading the minute out of a timestamp. Each is now an issue on firepanda-compat with the queries that run it and a suggested row, rather than a name in a column here.

`str.extract` came off that list, because the port does not call it. The report also stops printing the deliberate exclusions in the same sentence as the holes, so the count of holes is a count of holes.

### An operation can be covered by a row that measures its neighbour

Coverage is keyed by pandas name and the matrix is keyed by row, and a name can carry two rows with different costs. The matrix already has a literal `str.contains` and a regular expression one for that reason. Two ClickBench operations land on the wrong side of it: q27 and q28 need a byte length where the row measures characters, and q28 runs a regular expression with a capture group where the row replaces a literal, which is a per row trip through Python's `re` against a vectorized kernel.

Those are now listed on their own rather than folded into either the covered set or the holes. Counting them as measured would be claiming more than is known and counting them as missing would be wrong.

## v0.3.4

A patch. Nothing here changes what a published number means and no result file a reader has ever seen reads differently after it. What changed is whether a reader can read the ClickBench report at all, and whether the suite runs without somebody starting it by hand.

### The ClickBench report is six blocks rather than one table of 43 rows

Every other suite gets its grouping for free from the query registry, and ClickBench is one flat list of 43 with no groups in it. So the 43 now sit in six bands, each a contiguous range of ClickBench's own numbering, cut where the workload changes: seven scans, twelve group bys, eight where the work is in the filter or the sort, three over computed values, six with a wide or composite key, and seven page view queries inside a date window. Each band is a heading, a sentence saying what is in it, and a table of between three and twelve rows.

The bands are reading order and not a taxonomy. Almost every query in this suite filters, groups and sorts at once, so a taxonomy would either put most of the suite in one bucket or need a query to be in three, and a range cannot overlap. A test checks the ranges are contiguous, cover all 43 and share nothing.

`--queries scan` now runs one band, which beats rerunning 43 to look at a change in a Parquet reader.

### A reader can tell the three agreement states apart without leaving the table

A ClickBench query can agree, disagree, or be one the statement never determined an answer for, and the third one looked exactly like the second to anybody who had not read the suite README. Each row in the wall clock tables now carries the check behind it: `values` when every engine returned the same answer cell by cell, `shape only` when the statement does not determine which rows come back so the comparison was the row count and the column set. A query the engines genuinely disagreed on is in neither state and in no table, and its own section says so.

A disagreement on a query whose values are never compared is the loud case that used to read as the mild one, since its name was also in the weaker section above. It now says the values were not compared at all, so what differed is the row count or the columns, and the statement left neither of those open.

The list of undetermined queries belongs to a size, since which rows tie at the row a limit cuts on depends on how many rows there are. It was computed at 1M. When the report is describing a run at another size it now says the list was carried over rather than checked, and names the command that computes it for that size.

### The scheduled run does ClickBench

`bench.yml` runs the suite at 10M, which is ten times what CI's verify workflow runs and a tenth of the published size. 100M is the only size comparable to a published ClickBench number and it is a twelve gigabyte download onto a runner with fourteen gigabytes of disk.

The partitions are cached between runs, because a hundred files a week from ClickHouse's bucket for a file that has not changed since 2022 is rude as well as slow. A restored cache is not trusted on the strength of its key: the data step rehashes every file against the manifest and fetches what does not match, so a partial cache costs a download rather than producing a wrong number.

The timeout is fifteen minutes per engine per query rather than the harness default of an hour. An hour is sized for a fifty gigabyte join, and on a suite of 43 queries it means one hung pairing holds the runner for an hour while the other 128 never run.

Which runners the job uses is now a repository variable rather than a line in the workflow, the same arrangement the GPU job already had. The README has said for months that a full run is on two machines and nothing is averaged across them, and registering the second one is now a settings change instead of a pull request. Unset, it is the one hosted runner it has always been.

### The site charts what reading the file cost

Wherever a suite ran in both io modes there is now a third chart over the pair: one engine's scan time divided by its own memory time, which is how much of a scan run went on opening the Parquet rather than on answering the query. It needs two result files to exist, which is why it is a chart and not a column in a table, and ClickBench is where it shows because the hits table is the widest one in the repository by a factor of ten.

The number is per engine against itself and not against another engine. Far above one means the engine computes the answer so fast that reading is the whole query, which is where Polars lands on the narrow ones and is as much a statement about its group by as about its reader.

### repro knows that a different ten rows out of a tie is the same answer

`pixi run repro` compared the answer digest per pairing, which on the twelve undetermined ClickBench queries would report a dozen changed answers on a reproduction that reproduced. An engine is free to return a different ten rows out of a tie on a second run and several of them do, because the order a parallel aggregation finishes in is not fixed. Those queries are now compared on the row count, which the statement does determine, and one that came back with nine rows is still caught.

### The report says how our ClickBench numbers differ from the published ones

ClickHouse publishes a ClickBench results table and a methodology, and ours differs from it in five ways. Somebody is going to put one of our numbers next to one from that table, and the difference between the two is partly the engine and partly this list, so the list is now in the report, above the numbers, rather than in a commit message.

The five: we report the median of ten runs with the interquartile range rather than the minimum of three; we run warm rather than publishing cold, warm and hot separately, because cold cache behaviour is measured by the ingestion suite with the page cache dropped and mixing it into a query suite makes every number in it partly a measurement of the filesystem; we do not report load time or data size on disk, for any suite, and the report says the column is absent rather than leaving it to be read as instant; the published table is one machine type and ours is two, neither of which is that one; and we run 1M and 10M, which are not ClickBench.

A sixth difference is not about the harness. ClickHouse's published entry answers `COUNT(DISTINCT UserID)` with `uniq`, a HyperLogLog estimate, and every engine here counts exactly on all eight queries that touch it. That makes our numbers on those eight worse rather than better, which is why it is the one most likely to be misread.

The report also says in as many words that these are not published ClickBench results and are not submitted to the ClickBench table. Submitting a firepanda entry upstream is the right end state and it needs a firepanda that reads Parquet on its own and runs all 43.

### A partial ClickBench size is labelled where the numbers are

1M and 10M exist for CI and for machines that cannot hold the hundred million row table. A number taken on one of them is not comparable to a published one, so the section heading says so and a line immediately above the table says so again. Not a footnote underneath, which is where a reader has already stopped.

### check-sweep says when the answer behind a margin was checked weakly

An implausible speedup is usually a query that read less data, and the answer check is what rules that out. On the twelve ClickBench queries whose answers the published statements do not determine, that check compares the row count and the columns and not the values, so it rules out less. A margin on one of those is now reported with the reason said out loud rather than reading like a margin on any other row.

### CI installs a locked environment, which it has never been able to do

`.github/actions/setup-pixi` ends with `pixi install --locked`, which refuses to solve and needs the lock file to be there. `pixi.lock` was not committed and was not ignored either, so it sat untracked in every working copy looking deliberate. Every job that used that action failed at the setup step, which is all three jobs in the bench workflow and the whole verify workflow.

That means the release check has never run. v0.3.1, v0.3.2 and v0.3.3 each triggered the verify workflow and each failed before generating any data, so the row by row answer comparison, and since v0.3.3 the check against the hand written ClickBench answers, had never executed on a runner.

The lock is committed now. It covers the three platforms in the manifest, `pixi lock --check` says it was already up to date with `pixi.toml`, and nothing about the environment changes. What changes is that a job can install it, and that an upgrade to pandas or DuckDB arrives as a diff somebody reviewed rather than as whatever the runner solved that morning.

## v0.3.3

A patch, because nothing here changes what a published number means. No result file a reader has ever seen reads differently after this. What changed is what the repository can say about whether the ClickBench numbers are right, which was previously only that the ports agree with each other.

### Five ways a ClickBench port is quietly wrong

`suites/clickbench/README.md` writes down the five traps in this suite, what each one does to a number if you fall into it, and which of them this repository now catches by itself. TPC-H comes with the answers the TPC publishes and `pixi run validate-tpch` checks all 66 implementations against them cell by cell, which found two real problems before anything was published. ClickBench publishes no answers at all, so this suite starts without the strongest check the repository has, and the traps have to be found by reading the queries rather than by running them.

Each of the five produces a plausible table quickly, which is the part worth stating. A port that crashes gets fixed the same day. A port that returns ten rows of the right shape with the wrong rows in them goes into a chart.

Two of the five are decisions a port makes once and lives with, and they are written up rather than tested: bytes against characters in q27 and q28, which is the same two percent both ports already have a test for, and exact against approximate distinct counts, which matters only to somebody comparing our q3, q4, q7, q8, q9, q10, q12 and q22 against a published entry that uses `uniq`. The other three could be undone by a future change with nothing going red, so they are now checked.

### Every loader has to prove the text columns are text

The text columns in `hits.parquet` are `BYTE_ARRAY` with no string logical type, so every engine reading that file makes its own guess about what they are. Measured rather than assumed: without the conversion six of the 43 fail to bind, which is loud, and another nine answer with bytes where they should answer with text, which is not. The row counts are right, the values are right, and the fingerprint hashes bytes and text through the same function, so the agreement check passes and the table looks finished.

So `clickbench.check_text` takes a mapping of every column a loader ended up with to whether that engine is holding it as text, and all three loaders call it before returning. A load that skipped the conversion stops there instead of producing a number. It judges the columns that are actually present rather than demanding all 28, because the eight row fixture carries 25 of the 105 columns and a check that failed on the fixture would have been turned off within a week.

### The manifest counts nulls per column

Eleven queries filter on `SearchPhrase <> ''` or `URL <> ''` or `MobilePhoneModel <> ''`. In this dataset the empty string is what a missing value looks like and there are no nulls at all, in any of the 105 columns, which is now read out of the Parquet footers rather than believed. A reader that helpfully turned empty strings into nulls would change what those eleven filters mean under three valued logic, and q22's `NOT LIKE` is exactly the shape where that stops being a harmless difference.

`pixi run data clickbench` records a null count per column in the dataset manifest. It comes from the footer statistics, so it costs one metadata read rather than a pass over the data, and a reader that started converting shows up as a changed count in the manifest instead of as a changed answer in a table.

### Twelve queries are compared on what the SQL determines

Thirty two of the 43 end `ORDER BY something LIMIT 10`, and q17 has no order by at all. An answer is determined only when the ordering expression can tell the last included row apart from the first excluded one, and on this data it frequently cannot. At 1M, q11 has 3 rows tied at the cut, q22 and q24 have 4, q18 and q30 have 5, q25 has 19, q39 has 177, q38 has 651, q31 has 69,354 because `WatchID` is nearly unique so almost every group has a count of one, and q32 ties everywhere, which means its answer is any ten rows of the table. q40 ties 13 rows at its offset boundary and 10 at its limit boundary.

Those twelve are flagged in `tools/queries.py` with the reason, and both checks read the flag. The fingerprint comparison in `tools/run.py` and the exact check in `tools/verify.py` compare them on their row count, their column names and their column types, all of which the statements do determine, and neither looks at the values. An engine that answered one of them with five rows, or lost a column, or put an integer where everybody else has a date, is still caught.

Both outputs say so. `tools/verify.py` marks those queries as compared on shape alone and lists them at the end, and the report has a section naming them with the reason beside each. Making the ports agree here would mean adding an order by the query does not have and then measuring a different query, so the honest thing is a weaker check that announces itself. The list is a fact about the size rather than about the queries, since which rows tie at row ten depends on how many rows there are, and it is now recomputed rather than trusted, which is the next entry.

### Ten ClickBench queries answered a second time, by hand

The three checks this suite had all say the same thing: the ports agree. None of them says any port is right. TPC-H does not have that problem, because `pixi run validate-tpch` compares all 66 implementations against the validation output the TPC publishes and that check found two real problems before any number went out. ClickBench publishes no answers, so the strongest check the repository has did not exist here.

`tools/clickbench_hand.py` is a fourth implementation of ten of the queries, written from the SQL text in plain Python over the raw Parquet columns. It shares no planner, no dataframe library, no group by kernel and no type conversion with anything it checks. It reads bytes out of the file, decodes them itself and loops. It is slow on purpose and it never runs inside a measurement.

The ten are the ones where a port had to make a decision rather than transcribe one. q4 and q8 count distinct exactly where the published ClickHouse entry estimates. q27 and q28 average a byte length that is not a character length. q28 also has a regular expression anchored at both ends, so a referer that does not match has to come back unchanged rather than empty. q34 groups by a constant. q22 puts a `NOT LIKE` next to an empty string comparison. q23 is the only `SELECT *`, so it is the only place all 105 type conversions have to be right at once. q39 uses a `CASE` as a grouping key and has an OFFSET. q17 and q25 are two the statement does not determine an answer for.

The answers are committed under `suites/clickbench/expected/`, one file per query, each carrying the statement it is about, the columns, the row count, which columns are compared and why, and the answer itself. `pixi run validate-clickbench` regenerates them from the hand implementation and compares, then compares every engine against them, so a change on either side is a diff rather than a number that moved. All three engines reproduce all ten at 1M.

For the queries the statement does not pin an answer for there is still something to check. The values of the ordering key are determined even when which rows carry them is not, so those are compared, and every row an engine returned has to be a row of the full answer, which is what catches an engine that invented a row rather than picking a different legitimate one out of a tie.

The job runs on a release, on the ClickBench row of the verify workflow, beside the exact check.

### The set of undetermined queries is computed rather than typed in

Which statements the data does not determine an answer for is a fact about the size, not about the queries, so a list written down once at 1M says nothing about 10M. `pixi run validate-clickbench` now ranks each answer by the statement's own ordering expression, counts the rows sharing the rank at each cut, and reports what it found against the list in `tools/queries.py`.

Computing it found two errors in the list that was typed in. q26 was flagged and is not undetermined: it orders by `EventTime, SearchPhrase`, the pair separates the boundary rows even though `EventTime` alone does not, and all three engines return the same ten rows. It was being compared on its shape alone for no reason, which is a lost check on ten rows. q40 was recorded as tying 10 rows, which is its tie at the limit boundary. It also ties 13 at its offset boundary, and a statement with an OFFSET cuts twice: a tie at the offset decides which rows are skipped just as a tie at the limit decides which are kept. Both boundaries are checked now, which matters for q38, q39, q40 and q42.

So the set is twelve rather than thirteen, and one query went back to being compared on its values.

### A large_string is the same string

Polars writes `large_string` for every text column and pandas and DuckDB write `string`. The exact check compared Arrow types after widening integers, floats, decimals, dictionaries and dates, and not strings, so it reported a type difference on 22 of the 43 ClickBench queries, every one of them between two answers holding identical values. `verify.widen` now normalises string and binary widths as well, including the view types, and all 43 agree exactly at 1M across the three engines.

### The tolerance question q3 raised, measured

Issue #47 predicted that `AVG(UserID)` over values near 4.3e17 would need a wider tolerance class than the exact check has, because summing a million of them in float64 loses the low bits. It does not, by about five orders of magnitude. The exact integer sum divided by a million is 1.9481948778949197e18. A naive left to right float64 sum lands 4.17e-13 away in relative terms at 1M, a tree sum lands 1.31e-16 away, and DuckDB, pandas and Polars all land within 2e-16 of each other, which is well inside the 1e-9 ACCUMULATION tolerance the suite already uses. The numbers are written into the comment above `DEFAULT_TOLERANCE` instead of a tolerance entry that would never fire, because a dead entry reads like a measurement that was taken and it would not be one.

## v0.3.2

Still a patch, and the rule at the top of the file is still why. Two more engines answer the 43 queries, which is most of the work in the suite, and nothing here publishes a number. No result file a reader has ever seen means anything different after this. The minor comes when all four engines have a cell and the report has a place to put them.

What is worth knowing before then is that both ports were checked against DuckDB on real data rather than only against a fixture, and neither disagrees with DuckDB on a query whose answer the SQL determines. The disagreements are 11 queries for pandas and 10 for Polars, all of them inside the 13 written up in issue #47, which is the set the published statements do not pin an answer for.

### Polars answers all 43 ClickBench queries

`pixi run bench --suite clickbench --engines duckdb,pandas,polars --queries all` runs. `tools/engines/polars_clickbench.py` is 43 `LazyFrame` chains, one per published statement, each ending in `collect`, which is the shape a Polars user writes and also the thing that stops the harness from timing plan construction. A lazy engine that is never asked for an answer has not done any work, and a port that returned the plan would have produced beautiful numbers that meant nothing.

Both io modes work and they are genuinely different paths. Scan mode is `pl.scan_parquet` with the day, second and bytes to text conversions written into the projection, so Polars pushes the column list into the Parquet reader and most of these queries touch three columns out of a hundred and five. Memory mode goes through the same `clickbench.retype` that DuckDB and pandas use and hands Polars a table that is already in Arrow, where there is nothing left to push. The difference between those two numbers on the same query is the clearest measurement in this repository of what projection pushdown over a wide table is worth.

Against the real 1M partition, 33 of the 43 agree with DuckDB exactly. The 10 that differ are all inside the 13 the SQL does not determine an answer for, so nothing in this port disagrees with DuckDB on a query that has an answer. Scan and memory agree with each other everywhere except q17, q22, q38 and q39, with identical shapes on all four, which is the same tie variance and not a conversion difference.

### Polars is not deterministic on the queries the SQL does not determine

Worth writing down because it is a fact about the engine rather than about this port, and issue #47 needs it. The 43 digests were taken twice inside one process and again in a fresh one. Exactly the 10 queries above came back different, and q24, q25 and q26 came back the same every time.

That split is the whole story of `maintain_order=True`. Those three sort raw rows in scan order with nothing upstream that reorders, so a stable sort makes them repeatable. The other 10 sort the output of a `group_by`, which Polars leaves unordered, and a stable sort over an unstable input is still unstable. Forcing order on the grouping would fix it and would cost far more than the determinism is worth on queries that have no one right answer at any setting. So the pandas port is repeatable on all 13 and the Polars port is repeatable on 3 of them, and a run to run comparison of Polars against itself on those 10 will show a difference that is not a regression.

### Three places where the fast way answers a different question

`n_unique`, not `approx_n_unique`, in the eight queries that count something distinct. The approximate one is faster and it is a different question. ClickHouse's own published entry uses `uniq`, which is approximate, and that is one reason its numbers on those rows do not sit next to a DuckDB number on the same query. `n_unique` also counts a null as a value where `COUNT(DISTINCT x)` does not, which does not bite here because the hits table has no nulls in the columns those eight touch, and would on a table that did.

`str.len_bytes`, not `str.len_chars`, for `STRLEN` in q27 and q28. Same trap the pandas port has and the same two percent, and both queries sit behind a `HAVING COUNT(*) > 100000` that no CI sized fixture can reach through the query, so there is a test that runs DuckDB's `STRLEN` beside the port's byte count and a second one asserting the two counts differ on this fixture.

q29 is ninety expressions inside one `select` because the statement asks for ninety sums, and q34 groups by a constant because `GROUP BY 1, URL` groups by a constant. Some planners drop that and some do not. Dropping it by hand would be doing the optimizer's job and then reporting the result as the optimizer's work.

One more difference that is not a choice: Polars uses the regex crate, so the capture group in q28 is `$1` where DuckDB writes `\1`. A test runs both against the fixture and requires the same host back out, including on a referer that does not match the pattern at all, which both engines have to leave alone rather than turn into an empty string.

### The fixture moved out of the pandas tests

`tests/clickbench_fixture.py` now holds the eight row table and the patterns for taking a published statement's last clause back off, and both port test files import it. Copying it would have been less work and the copies would have drifted the first time one of them was edited to catch something, which defeats the point: the value of this fixture is that every engine is compared on exactly the same rows.

### pandas answers all 43 ClickBench queries

`pixi run bench --suite clickbench --engines duckdb,pandas --queries all` runs, and `tools/engines/pandas_clickbench.py` is 43 functions, one per published statement, each taking the loaded tables and handing back a frame. The loader is the same one DuckDB's memory path uses, which matters more than it sounds: the day conversion, the second conversion and the bytes to text conversion are one function called from both engines rather than two implementations that are supposed to agree.

Every answer carries DuckDB's own column names in DuckDB's order. That is not cosmetic here, because `engines.digest` keys its sums and its hashes by column name, so a port that computed the right numbers under its own names would be reported as an engine disagreement and somebody would go looking for a bug in the aggregation.

Against the real 1M partition, 32 of the 43 agree with DuckDB exactly. The other 11 are all inside the 13 the SQL does not determine an answer for, which is written up in full in issue #47, and for 10 of those 11 the ordering column agrees and only the tied payload behind it differs. So nothing in this port disagrees with DuckDB on a query that has an answer.

### The parts of the port that a passing test would not have told us about

`STRLEN` in DuckDB counts bytes. `Series.str.len` counts characters. On the real URL column the two averages are 88.56 and 86.57, which is over two percent, and both queries that use it sit behind a `HAVING COUNT(*) > 100000` that no fixture small enough for CI can reach through the query. The port counts bytes with `pyarrow.compute.binary_length` and there is a test that runs DuckDB's `STRLEN` beside it, plus a second test asserting the two counts differ on this fixture so the first one cannot quietly become a tautology. `Series.str.encode("utf-8").str.len()` is the obvious way to write it in pandas and it raises on an Arrow backed frame, which is a real gap rather than something being routed around for speed.

Every sort passes `kind="stable"`. It does not make pandas agree with DuckDB on the 13 undetermined queries and it is not meant to. It makes the pandas answer to those queries the same on every run over the same data, which is the difference between a reference and a coin flip.

Three queries are slower than they would be if the port were allowed to answer a different question. q29 is ninety separate sums because the statement asks for ninety separate sums. q28 runs Python's `re` once per row because DuckDB's `REGEXP_REPLACE` leaves a non matching string alone and pandas' vectorised replace has to be made to do the same thing. q23 sorts and slices rather than using `nsmallest`, so the row it keeps at a tie is the row a stable sort keeps. All three are the honest reading of the statement, and making a port quick by making it answer a smaller question is the failure this whole suite exists to catch.

### The CI fixture is eight rows and it was made to have teeth

Eight rows is fewer than any `LIMIT` in the suite, so every query returns everything that survives its filter and no query has to break a tie. That takes the 13 undetermined queries off the table and leaves the part that really is a contract, which is the filter, the grouping, the aggregates, the types and the names.

It also takes two things out of range, and both get their own test rather than a note. Five queries page past row one thousand or row ten thousand and two drop every group under a hundred thousand rows, so on eight rows those seven return nothing at all and comparing them is comparing two empty frames. There is a second comparison that strips the last clause off both sides, the SQL by removing the `LIMIT` or the `HAVING` and the port by replacing the three functions it narrows through, and a test asserting that the pattern doing the stripping really does strike 32 statements so it cannot pass by matching nothing. Then, because the digest does not depend on row order, there is a third test that reads the limit, the offset and the sort direction back out of the published statement and checks them against the arguments the port actually passed.

Whether any of that has teeth is a question you answer with numbers, so the port was mutated 34 ways, one at a time, with the whole file restored between each: `nunique` to `count`, `sum` to `max`, `min` to `max`, minute truncation to hour truncation, byte length to character length, a dropped value from an `IN` list, a sign flip, wrong limits, wrong offsets and flipped sort directions. Early rounds left four and then five of those alive, which is the honest state of a fixture written by eye. The values in it and the last of the three tests above are what those survivors were turned into, and the only mutant still standing is `COUNT(*)` written as `COUNT(ResolutionWidth)`, which no fixture shaped like the real table can catch, because neither this one nor the real `hits` has a null in that column.

## v0.3.1

A patch, not a minor, and the rule at the top of the file is why. ClickBench is three twelfths built here: the data can be fetched, the 43 queries are in the registry under the published names, and DuckDB runs them. Nothing publishes a number yet, because a suite with one engine in it has nothing to compare, so no number a reader has ever seen means anything different after this. The minor bump comes when the suite has all four columns and a report to put them in.

Two fixes in here do touch the existing suites and neither moves a published number. `engines.query_map` used to fall through to the db-benchmark callables for any suite it did not recognize, which was harmless right up until a fourth suite existed, and `column_sums` now casts unsafely so a sixty four bit hash column can go into the cross engine digest instead of raising. The sum that digest compares is rounded to nine significant figures, so nothing that agreed before disagrees now.

### DuckDB runs all 43 ClickBench queries, and the SQL is not ours

`pixi run bench --suite clickbench --engines duckdb --queries all` runs. The statements come out of `suites/clickbench/queries.sql`, which is a byte for byte copy of what ClickHouse/ClickBench commits at `duckdb/queries.sql` and has not changed there since November 2022. Nothing in this repository knows what any of them say. That follows what TPC-H already does, where the statements come from DuckDB's own extension rather than from anything typed here, and for the same reason: a benchmark you transcribed is a benchmark you can get wrong in your favour. A test pins the digest of the vendored file and a second one pins that every statement handed to DuckDB is a line of it with the semicolon removed.

The setup is where this suite is easy to get quietly wrong, and it is three separate conversions rather than one. `EventDate` in the file is an unsigned sixteen bit count of days and the published schema calls it a date. `EventTime`, `ClientEventTime` and `LocalEventTime` are counts of seconds and the schema calls them timestamps. Every text column is stored as bytes with no logical type on it. ClickBench's own loader does all three on the way in, so the published numbers are numbers for queries that ran against the converted types, and an engine here that skipped any of it would not be running the benchmark.

Reading the file as it stands was measured rather than assumed. Without the string conversion six of the 43 fail to bind at all and another nine answer with bytes where they should answer with text. The second group is the dangerous one. The row counts are right, the values are right, and the digest this harness compares engines on hashes bytes and text through the same function, so the agreement check would have passed. Without the date and timestamp conversions eight fail and two more answer with integers where they should answer with dates. That is why there is a test asserting the column types directly rather than a test asserting the queries ran.

Both io modes end with a `hits` whose columns have the same types and get there differently. Scan mode puts the conversions in a view's projection, where DuckDB pushes the filter and the column list into the Parquet reader. Memory mode converts in Arrow before registering the table, rather than wrapping the registered table in the same view, because a view would cast a hundred million binary values to text inside every one of the 43 timed queries and land that in the number as if it were query execution.

Two things had to change outside the engine. `engines.query_map` used to fall through to the db-benchmark callables for any suite it did not recognize, so a suite whose wiring was forgotten would have reported an engine that ran ten completely different queries under ClickBench's names; it now refuses. And `table_paths` has a ClickBench branch, because this is the only dataset here that is more than one file per table and its manifest has a row per partition rather than a row under the table name the queries read.

### The cross engine digest can take a full width integer

`column_sums` cast every numeric column to float64 with Arrow's safe cast, which refuses an integer past 2^53. The first three suites never hit it because their integers are small. ClickBench answers with `WatchID`, `UserID`, `URLHash` and `RefererHash`, which are sixty four bit hashes, and the refusal turned q18 and q23 into reported engine failures rather than into measurements. The cast is unsafe now, which sounds worse than it is: the value goes into a sum that is rounded to nine significant figures before anything compares it, so the precision the cast gives up was never being looked at.

### The 43 ClickBench queries are in the registry, under ClickBench's names

`tools/queries.py` has a `CLICKBENCH` tuple now, and `pixi run bench --suite clickbench --queries all` resolves 43 queries. Nothing runs them yet. The registry is what has to exist before any port does, because it is the thing that lets us say four engines ran the same query.

They are `q0` through `q42`, numbered from zero because ClickBench numbers from zero. Its own page pads the checkbox labels so `Q0..Q9` line up with the rest, every published result array is indexed from zero, and somebody reading our q22 next to a published q22 has to be looking at the same query. Worth being explicit about because the milestone issues described these queries by their line number in `queries.sql`, which is one higher throughout, and the registry follows the published numbering rather than the issues.

The order is published order and it is not a progression. The group by suite walks from low cardinality to high and TPC-H walks through the specification, but ClickBench was assembled from a production query log, so it opens with a row count and then stops being a sequence. Reading it as one leads to picking the wrong query to look at.

The `why` field is where the work went. Around half the suite is the same shape with a different key, which is the point rather than a redundancy, and the pairs only pay off if the registry says which query each one is a pair with. q12 and q13 are the same filter and the same key, one counting rows and one counting distinct users, so the gap between them is the cost of distinct counting and nothing else. q16 and q17 are the same group by with and without an ORDER BY, which is the one place an engine that always sorts gets caught. q31 and q32 are the same group by with and without a filter, and dropping the filter is what takes the group count from large to nearly one per row. q33 and q34 differ by a constant added to the grouping key, which a planner should be able to remove entirely.

One group, called `clickbench`, rather than four categories. The queries do not partition: almost every one filters, groups and sorts at the same time, so any taxonomy would either put most of the suite in one bucket or need a query in three of them. Somebody who wants a subset names the queries.

The default size is `1M` and not the published `100M`, which is a departure from the other three suites where the default is the smallest size worth publishing. Only `100M` is comparable with a published number and it is a twelve gigabyte download, so defaulting to it would mean a first run that spends an hour on the network before it says anything.

### What each ClickBench query is made of, read off the SQL for now

The 43 have operation declarations in `tools/operations.py`, and they are provisional in a way nothing else in that table is. Every other entry was read off a pandas implementation that already exists. These were read off the published SQL, because the pandas port is not written yet and the registry cannot land without them: the test that every query declares something walks the whole registry. They get re-derived from the port once it exists, and where the two are likely to disagree is the queries with more than one route through pandas, since a top ten is either `sort_values` then `head` or it is `nlargest`, and a global distinct count is either `nunique` or the length of `drop_duplicates`.

This found six real holes in the firepanda-compat cost matrix. Counting distinct values of a whole column, a minimum over one, reading the minute out of a timestamp, a conditional expression, pulling a capture group out of a regular expression, and taking rows at an offset. None of those has a row over there and all six are operations a published benchmark query runs.

Until now there was exactly one operation with no row and it was excluded on purpose rather than missing, so the report had one paragraph that covered both cases. It does not any more. The two are separated, the exclusions carry their reason in the table rather than in the rendering code, and a hole is described as a hole. Running them together would make the deliberate exclusion look like an excuse for the ones nobody has got to.

One operation is newly excluded on purpose. q0 is `SELECT COUNT(*)` and nothing else, which on a frame that is already in memory reads the length of an index and touches no data, so there is nothing for a cost matrix row to measure. It is declared anyway, because a query declaring nothing looks like a query nobody got to. What q0 actually measures happens in scan mode, where the answer comes either out of Parquet metadata or out of a read, and that is a reader measurement rather than an operation one.

### The hits table can be fetched, and it is the first suite here whose data cannot be generated

`tools/data.py --suite clickbench --size 1M` now downloads the ClickBench hits table. This is the fourth suite in the repository and the first one with no generator behind it at all.

That is worth stating rather than hiding, because it is the reason the suite is worth having. db-benchmark data comes out of a splitmix64 counter stream and the ingestion files come out of the same generator, so both are uniform by construction. TPC-H comes out of `dbgen`, which is at least a real distribution but still a synthetic one. The hits table is a dump of what a real product recorded. It has skewed cardinalities, a URL column with a heavy tail, empty strings standing in for nulls, and 105 columns of which most queries touch three. Everything in this library has been optimized against uniform generated keys, and this is the first data here that has none.

There are three sizes and only one of them is ClickBench. `100M` is the published dataset, 99,997,497 rows across a hundred partitions and about twelve gigabytes on disk. `10M` and `1M` are the first ten and the first one of those partitions. They exist because a suite that can only be run on a machine with a spare hundred gigabytes is a suite that gets run four times a year, and because the agreement check needs a size a normal CI job can pull. A number from a partial size is not comparable to a published one, and the manifest records `is_published_size` so the report can say so on the table rather than in a footnote.

The download is resumable. A hundred files over a slow link will be interrupted at least once, so bytes land in a `.part` file and a restart asks the server to continue from where that file ends. Every partition is hashed after it lands and the digest goes in the manifest, which is what a second run checks before it decides the cache is usable. That matters more here than it does for a generated suite: a truncated download that nobody notices is a wrong answer rather than an error, and a half written Parquet file usually still opens.

Free space is checked before the first byte moves. The sizes come from a HEAD request per partition, which costs a couple of seconds for the full set, and refusing up front is a lot cheaper than filling the disk on the eightieth file. The full size also has its row count checked against the published 99,997,497, since a download that quietly lost a partition would otherwise just look like a faster benchmark.

One detail is load bearing and looks like nothing. The bucket is behind Cloudflare and Cloudflare answers 403 to urllib's default `Python-urllib/3.13` user agent. The file is not restricted in any way, curl fetches it with no headers at all, it is that string alone that is refused. So the downloader sends a user agent naming this repository, and there is a test holding it in place, because without it every download fails with an error that reads like the dataset has been taken down.

Nothing converts to CSV. ClickBench publishes a TSV form, nobody benchmarks against it any more, the ingestion suite is where a reader gets measured, and a seventy gigabyte text file in the cache helps nobody.

Verified end to end at the 1M size: 122,446,530 bytes, 1,000,000 rows, 105 columns, and a second run reuses the cache after checking every digest.

No queries run yet. This is the data and the manifest only.


### The join set now has the character join the public suite has, and j4 and j5 are not the queries they were

The join queries here were five and none of them joined on a text key. Upstream db-benchmark's five are named for their key types: small inner on an integer, medium inner on an integer, medium outer on an integer, medium inner on a character key, big inner on an integer. Only the fourth joins on text. The set here had a big inner and a big left join in the last two places, so it was missing the one query in the public suite that exercises a text key and it had one query the public suite does not have.

That was stated the other way round in this repository until now. The claim was that all five join on text, and it was wrong about the suite it says it runs.

The generator now writes id4, id5 and id6 alongside id1, id2 and id3 on the left table and on each right table, as the same values rendered the way upstream renders them, which is `sprintf("id%.0f", x)`. Note that the naming runs the opposite way from the group by table, where id1 through id3 are the character columns. That is upstream's doing and it is reproduced rather than tidied up, because reproducing it is what lets a number here be read next to a published one.

Because id5 is id2 written out, j4 and j2 pair exactly the same rows and produce the same answer to the last digit, which the agreement check confirms across all four engines. So the pair is a measurement of what a text key costs and of nothing else.

The queries are now j1, j2 and j3 unchanged, j4 as the medium inner join on the character key id5, j5 as the big inner join, and j6 as the big left join. j6 is one more than upstream has and it is kept because it is the only place in the set where an outer join meets a build side too large for any cache.

Two things follow from this and both are worth saying plainly. A j4 or j5 number from before this change is not comparable with one from after it, because they are different queries. And every join query is now measured over a wider left table, seven columns instead of four, since every engine is handed the whole file with no projection, so a j1 number from before this change is not strictly comparable either.

### What the whole frame join shape costs, measured rather than assumed

j1, j2 and j3 run as a pipeline and j4 and j5 run as a whole frame join, and until now there was no number for what the difference is worth on the same query. There is a `--frame-j123=1` flag now that puts the first three back on the route they came off, so both shapes can be run against the same data and the same answer.

At ten million rows on an i9-13900K, seven runs each, medians: j1 is 6.9 ms on the pipeline against 18.5 ms on the whole frame, j2 is 8.2 against 19.3, and j3 is 7.9 against 19.6. That is 2.69x, 2.36x and 2.49x. Peak resident memory is 385 MB against 660 and CPU seconds are 0.83 against 2.52. Both routes produce identical sums.

The reason to have this as a number is that it is a different claim from the chunk sweep below. The sweep compares two chunk sizes inside the pipeline. This compares the pipeline against the shape a caller gets from `DataFrame.join`, which walks the whole column once per phase, probing the key, then pairing the matches, then gathering the columns, with ten million rows of intermediate between each pair and nothing surviving cache from one phase to the next. Three times the CPU for the same answer is what that shape costs, and that is a firepanda number rather than a harness one.

### The driver was handing the join a chunk four times too big, and it cost half the speed

The three pipelined join queries cut the left table into chunks of a hundred and twenty eight thousand rows before the clock starts. That number was picked because it is the number firepanda's own morsel scheduler uses and it was never measured here. Measuring it says it was the wrong number by a factor of four.

Sweeping the chunk on an i9-13900K at ten million rows gives medians of 6.5, 6.2, 8.8, 14.0 and 16.4 ms on j1 for chunks of sixteen, thirty two, sixty four, a hundred and twenty eight and two hundred and fifty six thousand rows. j2 gives 7.9, 8.4, 11.7, 15.9 and 17.7 and j3 gives 7.8, 7.9, 12.1, 15.3 and 18.1. The default is now thirty two thousand rows, which makes j1 2.25x faster than it was, j2 2.00x and j3 1.97x, with no change to any answer.

The CPU seconds move with the wall clock, 0.90 against 2.26 on j1, so this is not a chunk count that was starving the cores. Seven hundred and sixty chunks was already enough to keep thirty two of them busy. It is that a join makes several passes over each chunk, probing the key, bucketing the matches and then gathering the columns, and a chunk has to still be in the core's private cache when the second pass starts. Thirty two thousand rows of a key, a value and the ordinals in between is about a megabyte and fits in an L2. A hundred and twenty eight thousand rows is four megabytes and does not.

Peak resident memory follows the same curve, 384 MB at the new default against 457 MB at the old one and 566 MB at twice that, because every intermediate the thirty two workers hold at once is sized by the chunk.

The driver takes a `--chunk-rows=` flag now so the sweep can be repeated without a rebuild. The harness does not pass it, so published numbers always use the default.

Doubling the other three made it worth asking whether j4 and j5 should move onto the pipeline as well, since the reason they are not on it was measured at the old chunk size. They should not. There is a `--pipeline-j45=1` flag now that puts them on it, and at ten million rows a side the whole frame route is 55.9 and 55.5 ms while the pipeline is 62.2, 85.3, 84.1 and 75.7 ms on j4 and 58.6, 71.3, 86.1 and 77.7 ms on j5 across the same four chunk sizes. Every chunk size loses and the smallest loses least, which is the opposite shape from j1, j2 and j3. That is what it looks like when the thing missing cache is the build table rather than the chunk, and it is the reason already written beside those two queries rather than a new one. Both routes produce the same sums to the last digit.

### The three small side join queries run as a pipeline, and the two big ones do not

j1 through j5 all used to be a whole frame join followed by a reduction over its result. That builds two columns of ten million rows and then reads them back to produce three numbers, which is a hundred and sixty megabytes written and a hundred and sixty read for an answer of one row.

j1, j2 and j3 now run as a `Pipeline` over the left table with a `Join` node and a `Reduce` node behind it. The right table is hashed once, then the driver hands out a chunk of the left table at a time, and each worker probes its chunk, gathers v1 and v2 for the rows that matched, and folds the result into a one row partial before the chunk leaves its cache. Nothing of ten million rows is ever written. The left table is cut down to the two columns a join query reads, and cut into chunks, before the clock starts, because a pipeline consumes its source and the harness runs the same query five times over the same table. That copy is setup in the same sense that generating the table is setup.

j4 and j5 stayed on the whole frame join, and this is a measurement rather than an oversight. Those two join the big table against another table of the same height, and at ten million rows a side on an i9-13900K the whole frame route is a 47.2 ms join and a 3.9 ms reduction while the pipeline is a 14.5 ms build and a 41.3 to 46.7 ms streaming probe. Two reasons and the second is the larger one. The build side is hashed on one thread before a chunk moves, and a probe split into seventy seven chunks is slower per row than one pass over ten million, because the table being probed is forty megabytes and no part of it survives in cache from one chunk to the next. Which plan wins is decided by how big the build side is, and that decision belongs in an optimizer, which firepanda has not got until M4, so for now it is written down in the driver next to the numbers that justify it.

All five queries agree with pandas, polars and DuckDB on the published db-benchmark checksums, unchanged.

Measured at 0.5GB on an i9-13900K against pandas 3.0.5, polars 1.44.1 and DuckDB 1.5.5, five runs each, seconds and peak resident memory.

| query | firepanda | pandas | polars | duckdb |
| --- | --- | --- | --- | --- |
| j1 | 0.014 s, 0.45 GB | 0.318 s, 1.17 GB | 0.027 s, 0.75 GB | 0.017 s, 0.74 GB |
| j2 | 0.015 s, 0.47 GB | 0.348 s, 1.17 GB | 0.028 s, 0.75 GB | 0.021 s, 0.75 GB |
| j3 | 0.014 s, 0.47 GB | 0.340 s, 1.17 GB | 0.038 s, 0.82 GB | 0.014 s, 0.69 GB |
| j4 | 0.061 s, 0.78 GB | 1.251 s, 2.55 GB | 0.195 s, 1.57 GB | 0.090 s, 1.42 GB |
| j5 | 0.055 s, 0.78 GB | 1.475 s, 2.55 GB | 0.237 s, 1.65 GB | 0.084 s, 1.34 GB |

### Peak memory on a machine without a /proc

The firepanda driver read peak resident memory from `/proc/self/status` and CPU time from `/proc/self/stat`. On anything without a `/proc` it reported zero for both and said `ok`. That was written down as a deliberate choice, on the grounds that a machine which cannot report memory can still report time, and it has now been paid for: a run on a Mac produced a result file where the subject engine's entire memory column was zeros, the runner printed `rss 0.00 GB` fifteen times without comment, and nothing in the harness objected. Half of what this repository claims is memory, and a zero that reads as a measurement is worse than a refusal.

`getrusage` is in libc on both platforms and reports the same quantities, so there is one code path now instead of a Linux one and a hole. It is also better than what it replaces on Linux rather than merely equal to it: `/proc/self/stat` reports CPU in USER_HZ ticks, which is ten milliseconds, and `getrusage` reports microseconds, so the caveat about quantizing a two hundred millisecond timed region at ten milliseconds is gone. `ru_maxrss` and `VmHWM` are the same high water mark, so no Linux number moves. The struct is read by byte offset rather than through a declared layout, because the two platforms agree on every offset that matters and differ only in the width of `timeval`'s `tv_usec` and in whether `ru_maxrss` counts bytes or kilobytes, and writing the layout down twice would be the same offsets with more places to get them wrong. `/proc` is still read, for current resident memory and the thread count, which `getrusage` has no field for and which nothing here claims anything about.

`validate-results` now refuses a result that ran and reports no peak memory. A process that ran has a non zero high water mark, so there is no query for which zero is the truth, and the only way to produce one is a probe that did not work. A pairing that did not run still owes nothing but a reason, which is the whole point of publishing refusals rather than dropping the row.

The first numbers this makes possible, db-benchmark at 0.05GB on an M series laptop, firepanda against pandas over all fifteen queries with all four engines agreeing on every answer:

| | median | worst | best |
| --- | --- | --- | --- |
| speed | 7.3x | 2.6x | 28.3x |
| memory | 4.5x | 2.8x | 7.7x |

Against the stated goal of ten times the speed on a tenth of the memory, that is short on both and shorter on memory, and the memory column is worth reading rather than just scoring. firepanda's peak sits between 64 and 211 MB across the fifteen queries where pandas ranges from 287 to 812 MB. The first guess was that the bottom of firepanda's range is fixed runtime cost, and that is wrong: a Mojo binary that does nothing but read its own `getrusage` peaks at 9 MB, so the 64 MB on the join queries is very nearly the data. The 0.05GB dataset is about 50 MB in memory, which means firepanda is holding it in 1.3 times its size and pandas in six times its size. A tenth of pandas at this size would be 31 MB, which is less than the data, so on this suite at this size the memory target is not reachable by any engine that holds its input, and the honest reading of 4.5x is that most of the room pandas leaves has already been taken. The size that would test the claim is one where pandas' overhead rather than the data dominates. 0.05GB is the size a laptop can run, and it is published because it is what was measured.

### An exact answer check, and the first thing it found

The cross engine fingerprint has been wrong three times and each one is written up in `tools/README.md`. This is a fourth thing it gets wrong, except it is not a bug and cannot be fixed: every part of the fingerprint reduces a column on its own, so it knows the multiset of values in each column and nothing at all about which row each value sits on. Two answers holding the same values paired up differently are identical to it, which is precisely what a join on the wrong key produces. It calls that agreement and always will, because a per column reduction cannot see a permutation across columns, and that property is also why it is cheap enough to sit on the timed path and computable by the Mojo driver without an Arrow sort.

`pixi run bench --verify exact` is the answer. Every engine writes its answer to Arrow IPC after the last timed run and never inside one, and `tools/verify.py` reads them back and hands each pair to `fpcompat.compare` from a firepanda-compat checkout, which sorts both sides by every column and compares them row by row and value by value. Nothing on the timed path changed and the fingerprint is still the default, so no published number moves.

The comparison layer is imported rather than vendored. It is the thing that decides whether two answers are the same answer, and a copy of that in a second repository is a second definition of correctness that would not stay in step for long. Point it with `--compat`, or set `FIREPANDA_COMPAT`, or clone it next to this repository, and whichever it finds, that commit goes into the verdict, because a verified answer is only verified against a particular idea of what verified means.

It found something on the first full run. Polars rounds a decimal product back to the scale of its operands, so on the seven TPC-H revenue queries its money columns differ from pandas and DuckDB, which agree with each other, at about five parts in a billion. `l_extendedprice` and `l_discount` are both DECIMAL(15,2), the specification's arithmetic gives their product scale 4, DuckDB keeps the wider scale and pandas is in float64 and keeps everything. All three still reproduce the specification's published validation output, which `validate_tpch.py` checks at a thousand times this size, so this is two defensible readings of decimal multiplication rather than a wrong answer. No check here could see it before: the fingerprint compares column sums at 1e-7, a hundred times looser than the difference. It is now in the known difference registry in `verify.py` with the reason and the tolerance class it needs, and anything larger than that class is still a disagreement.

Two places the exact check is deliberately not exact, each a closed list with a paragraph arguing for it. Floats are compared under the compat tolerance classes, because every answer here is a sum or a mean over millions of rows and a parallel sum that reproduced a serial one bit for bit would not be a parallel sum. And db-benchmark q9's `r2` has a floor of 1e-12 below which a value is compared as zero, because a squared correlation computed from moments subtracts quantities near 1e16, so a true zero comes back as 0.0 from pandas and 1e-35 from Polars and a relative tolerance calls those completely different, which is the correct behaviour for a relative tolerance everywhere the zero is a real answer rather than a cancellation residue.

Verified end to end on all three suites: 15 of 15 db-benchmark queries at 0.05GB, 22 of 22 TPC-H queries at SF1 with the seven Polars ones under the known difference, and 5 of 5 ingestion queries at 1M, which is three CSV readers agreeing value by value on quoted and null heavy files. A release workflow runs the same three, and a result file may now carry a `verification` block, which `validate-results` requires to name the compat commit it was checked against.

### Every query says which operations it is made of

A query here is five or six pandas operations wrapped into one number, so a reader who sees firepanda lose a row has no way to find out which operation inside it lost. The [compat cost matrix](https://github.com/tamnd/firepanda-compat/blob/main/docs/specs/09-resources.md) is a row per operation and answers exactly that, and until now there was no link between the two.

Each query now declares the pandas operations its pandas implementation calls, read off `engines/pandas_engine.py` and `engines/pandas_tpch.py` rather than off the query text, and the report carries a table per suite. `pixi run operations` prints the same thing, and `--query tpch/q9` prints one query with the matching matrix rows named.

The last column of that table is the operations a published query runs that the cost matrix has no row for. On the first run there were 12 of them across the three suites: `DataFrame.assign`, `GroupBy.count`, `GroupBy.head`, `GroupBy.median`, `GroupBy.min`, `GroupBy.std`, `Series.map`, `Series.mean`, `pandas.read_csv`, `str.endswith`, `str.slice` and `str.startswith`. That is a hole over there with a query attached to it, and it is published for the same reason every other loss in this repository is.

Eleven of those twelve have rows now, so the vendored copy of the table is refreshed to the 65 operation version and the last column is down to `pandas.read_csv`. That one stays, and the report says it is a deliberate exclusion rather than a hole: reading a CSV is what the ingestion suite here already measures, on five file shapes against four engines, and the compat corpus is Arrow on disk rather than text. The two that mattered were `GroupBy.median` and `GroupBy.std`, because db-benchmark q6 exists to measure a reduction that has to keep its values per group and the cost matrix had never measured one at all.

### Peak memory is published as a ratio and plotted, not left as raw bytes

The claim is ten times the speed of pandas on a tenth of the memory, and until now the report answered the first half in ratios and the second half in bytes. The memory table has always been there and the scorecard has always carried a memory geometric mean, so nothing new is measured here. What changed is that the numbers are now readable.

Each memory cell carries a ratio against pandas, above one meaning less memory used, which is the same direction as the speed ratios and the same direction the [compat cost matrix](https://github.com/tamnd/firepanda-compat/blob/main/docs/specs/09-resources.md) uses. Absolute bytes are the honest raw number and also the one a reader cannot act on, because whether 845 MB is good depends entirely on what the other engine did on the same query on the same machine, which the table knew and did not say.

Every suite now leads with a pair rather than with a time. On the i9-13900K at 0.5GB in memory mode that reads: firepanda against pandas, 1.14x on time and 1.12x on peak memory, over the 13 queries both ran and all four engines agreed on. Those are not the goal numbers and they are the measured ones.

The site draws two charts per group instead of one, wall clock and peak resident memory, still never across a machine, a suite, a size or an io mode. Two charts rather than two axes on one, because a shared x axis with two scales is read wrong by about half of the people who look at it.

### The join queries stop carrying columns nothing reads

The left join table has four columns and every join query reads two of them, the key and v1. The driver was handing all four to the join, which gathers two columns of ten million rows through the join for a reduction that never looks at them.

Polars and DuckDB both drop those columns without being asked, because both are handed the query as a plan and both push the projection down into it. firepanda has no optimizer until M4, so leaving it in meant firepanda was the only engine of the four executing that work, and the number was measuring a missing optimizer rather than a join. The driver now does it by hand and says so.

pandas is deliberately left alone, because pandas has no optimizer either and carrying the columns is what pandas genuinely does with this query. The pandas column of the join rows therefore includes a cost the other three avoid, which is a difference between the engines and not a handicap applied here.

On the i9-13900K at 0.5GB, ten runs, memory mode, quiet machine, firepanda's join medians go from 0.052, 0.055, 0.048, 0.134 and 0.075 seconds to 0.035, 0.035, 0.034, 0.069 and 0.072. All five answers agree with all three other engines.

### q8 is answered, and the driver narrows after the gather rather than before it

q8 asks for the two largest v3 in each id6 and it was the one db-benchmark query firepanda skipped, because it needed a top-k per group and no such kernel existed. firepanda has one now, so the query is written and the engine's unsupported list is empty. All four engines run all fifteen.

The way the query is written matters more than usual here. The obvious order is to narrow to the two columns the query reads and then take the rows, which is what pandas does and what the first version of this driver did. But `select` copies the columns it keeps, so narrowing first copies twenty million values in order to answer a question about two hundred thousand. Narrowing second copies two hundred thousand. On the i9-13900K at 0.5GB that is 75 ms the first way and 37 ms the second, for the same answer, and the answer's checksum is the same one pandas, Polars and DuckDB produce.

The measured row, ten runs, memory mode, quiet machine, repeated twice with the same result: firepanda 0.037 s, DuckDB 0.071 s, Polars 0.205 s, pandas 2.759 s. Peak resident set is 0.95 GB for firepanda against 2.59 for DuckDB, 1.34 for Polars and 1.65 for pandas.

### The Parquet claim is corrected everywhere it appears

Nine places in this repository said firepanda has no Parquet reader. That stopped being true a while ago and nobody came back to fix the prose, which is exactly the kind of rot a benchmark cannot afford, because the sentences explaining why a comparison is arranged the way it is are the ones a sceptical reader checks first.

What is true is narrower and more interesting. firepanda can open a Parquet file. The way it does it is to hand the file to DuckDB and read DuckDB's vectors back as Arrow. That is a sensible thing for a dataframe library to do, and it is not a thing that can go on a timer in a table where DuckDB is one of the four engines, because the number that came out would be DuckDB's decoder wearing firepanda's name.

So nothing about how the suites run changes. db-benchmark still regenerates its data from the seed rather than reading the Parquet file, and TPC-H is still twenty two refusals in firepanda's column. What changes is the stated reason, from a capability firepanda lacks to a conflict of interest it has, and for TPC-H the list of what is actually missing, which is the twenty two queries in the driver, an ordering comparison on strings, and a load path that does not run through an engine in the table.

The coverage line was stale in the other direction too. It said firepanda runs 13 of 15 db-benchmark queries and named q9 as one of the misses, which the previous entry in this file had already fixed. It is 14 of 15 and the only miss is q8.

### firepanda runs db-benchmark q9

q9 is the squared correlation of v1 and v2 grouped by id2 and id4, and firepanda has been reporting it as unsupported since the suite landed because it had no aggregation that reads two columns at once. firepanda 0.6.24 added one, so the driver now runs it: group by the two keys with a correlation, square the result, and report the key columns and the square, which is what the other three engines report.

That leaves q8 as the only query firepanda skips. It wants a top-k per group and there is still no kernel for it.

Measured on an i9-13900K at ten million rows, seven runs, all four engines in the same invocation, with the cross engine fingerprints agreeing at ten thousand answer rows: pandas 1.445 s at 1.57 GB peak, Polars 0.197 s at 2.15 GB, DuckDB 0.022 s at 1.37 GB, firepanda 0.113 s at 1.10 GB. firepanda is 12.8 times pandas and 1.74 times Polars here, on the lowest peak memory of the four, and DuckDB is five times ahead of everyone.

### The firepanda driver is rebuilt when firepanda changes

The driver was rebuilt when `engines/firepanda/main.mojo` was newer than the binary, and never mind the several hundred files of library it links. So a firepanda release that changed the CSV reader and not the driver left the old binary in place, and the harness went on publishing numbers for a version of firepanda that no longer existed. That is the worst way for a benchmark to be wrong: silently, and in whichever direction the last change happened to go.

The staleness check now looks at every `.mojo` file under the firepanda checkout as well as at the driver. The size of what it was hiding, measured today on an i9-13900K at ten million rows with the two builds four releases apart: `csv_narrow` 0.176 s stale against 0.076 s current, `csv_wide` 0.172 against 0.116, `csv_quoted` 0.354 against 0.107, `csv_nulls` 0.179 against 0.107. Every published firepanda ingestion number since the suite landed should be read as belonging to whatever build was on the machine, not to the ref in the result file, and the fix is what makes that field mean what it says.

That check compared modification times, and it caught the case it was written for and missed the one that matters on a benchmark machine. The machine gets the firepanda checkout as a tarball, tar restores the modification times the files had in the checkout the tarball was made from, and a source last edited a week ago therefore lands looking older than a binary built on the machine yesterday. So the harness reused the binary and published the previous library under the new commit's ref, which is the exact failure the check exists to prevent, arriving through the one door it did not cover.

It cost a real result before it was found. firepanda 0.6.19 changed how the string factorize picks a worker count, and db-benchmark q3 and q7 were reported here and in that release as unmoved, inside their own run to run spread. Rebuilding the driver against each of the two commits by hand and alternating them on an i9-13900K at ten million rows: q3 0.264 s before against 0.132 after, q7 0.250 against 0.126, q10 2.05 against 1.92, q1 0.030 both ways. Both string keyed group by queries halved and the harness said nothing had happened.

The check now hashes the bytes of the driver and of every `.mojo` file under the checkout, path names included so a rename counts, and keeps the digest in a file beside the binary. It is written after a successful build, so a failed compile leaves no digest and the next run tries again rather than trusting whatever binary is sitting there. What is deliberately not in the digest is the Mojo toolchain, because reading its version is a subprocess and this runs on every query, so a stale binary across a compiler upgrade is still possible and still needs `--rebuild`.

### The timed region stops paying for the previous run's teardown

Every timed run in the harness was shaped `answer = run()`, and the rebinding is what releases the answer the run before it produced. That release happens inside the timed region, so run two was charged for freeing run one's result, run three for run two's, and so on. On the ingestion suite, where an answer is a whole ten million row frame, that is gigabytes of free attributed to the wrong thing. The previous answer is dropped before the clock starts now, in `metrics.measure` for the three Python engines and in the firepanda driver's loop for the same reason.

It was charged to all four engines equally, so no published comparison was ever tilted by it, but it was a constant added to every number, and a constant added to everything compresses the differences the suite exists to show. It also added most of the run to run spread: the interquartile range on `csv_narrow` fell from 80 ms to 20 ms once it was gone.

Every ingestion number moves, and every engine's moves down. On an i9-13900K at ten million rows: pandas 0.111 to 0.063, Polars 0.049 to 0.036, DuckDB 0.197 to 0.142, firepanda 0.263 to 0.152. Reported peak RSS falls too, because the process is no longer holding two frames at once at the moment of measurement.

### The firepanda driver stops copying the file for `csv_narrow_typed`

firepanda gained `read_csv_as`, a read of a path with a declared schema, so the driver no longer has to open the file and read the bytes itself. That mattered more than it sounds: doing its own IO gave up firepanda's memory mapping, so the one query in this suite that skips inference was also the only one paying to copy the whole file first. The comment in the driver saying no such overload existed is gone with it.

This changes a published number, and in firepanda's favour, so it is worth being plain about what it is not. The other three engines were already reading the file the way their own users would. This is the firepanda driver catching up to that, not a new allowance.

## v0.3.0

A minor bump, and the reason is the rule at the top of this file. The harness measures something it did not measure before, and the numbers it publishes for the engines it already measured are unaffected, but a suite is a new claim about all four of them and it belongs in a version a reader can name.

### The ingestion suite runs

`read_csv` is the first line of code almost every user writes, and until now the harness had nothing to say about it. It has five queries over four generated files: a four column file with types inferred, the same file with types declared, a fifty column file at a tenth of the rows so the bytes stay level and what moves is the per field cost, a file whose quoted text fields carry delimiters and line feeds and doubled quotes, and a file that is nine tenths empty fields.

It is also the first suite where firepanda is handed the same file as everybody else. db-benchmark regenerates its data from the same seed because firepanda has no Parquet reader, and that regeneration is a claim the agreement check tests rather than a fact. Here there is nothing to regenerate. All four engines opened one file and agreed on all five queries, including the quoted one, which is a stronger statement about firepanda's CSV reader than any timing in the table.

The suite runs in `scan` mode only and `run.py` refuses `--io memory` for it with the reason, because reading the file before timing the read is not a mode, it is a mistake.

### The timed region is the read, and only the read

Every other suite has its engines return an Arrow table, which is how the harness compares answers. Doing that here would have timed the conversion as part of the read, and the conversion is neither small nor the same size for everyone: on the fifty column file it cost Polars about seventy times what the read did, because Polars stores text as views into a buffer and Arrow wants it offset encoded. The first run of this suite reported Polars as the slowest CSV reader in the table when it is the fastest. Each engine now returns its own frame and the harness converts afterwards, outside the timing.

### pandas cannot read the quoted file with the pyarrow engine

pandas gets the pyarrow engine everywhere in this harness, because it is multithreaded, most people do not know it is there, and benchmarking against the slower configuration of a competitor is the kind of thing that gets noticed. On a file with a line feed inside a quoted field it fails outright with a parse error: pyarrow's reader has `newlines_in_values` off by default and pandas does not expose it. The harness falls back to the default C engine, which reads the file correctly and takes about sixty times longer, and records the fallback in a note the report prints beside the number. A measurement taken under a different configuration is still a measurement, but it is not the same one as its neighbours in the row.

### Cold runs are actually cold

The first run of every ingestion measurement is taken after the file has been dropped from the page cache with `posix_fadvise`, which needs no privilege and no separate command, and it is reported separately from the warm median. That the drop took is checked rather than asserted: the block reads recorded against the cold sample come to the size of the file, and on a machine where the kernel refuses, every measurement in the run carries a note saying the cold column is warm.

### Reading a whole file needed a cheaper agreement check

The existing text digest is a 64 bit FNV over every value computed in Python, which is right for a group by answer with as many rows as there are groups and wrong for an answer with as many rows as the file. Ingestion answers are compared on the row count, the null count of every column including the numeric ones, the sum of every numeric column and the total byte length of every text column. That is weaker and the weakness is worth naming: it does not catch two values swapped between rows. What it does catch is the class of mistake a CSV reader actually makes, which is losing a row, splitting a quoted field on the delimiter inside it, dropping the escape from a doubled quote, or reading an empty field as an empty string instead of a null.

## v0.2.0

A minor bump, and the reason is the rule at the top of this file. Two changes here alter what a published number means. firepanda's peak resident memory on the group by queries was measured against a narrower table than the other three engines were given, and it now is not, so those numbers moved and a v0.1.0 memory figure is not comparable with a v0.2.0 one. And a result file now names the build that produced it, which every file before this one either failed to do or did wrongly.

### A result file names the build that produced it

The commit and the toolchain were read out of an `env.json` and out of nothing else, and `pixi run bench` does not write one, so every file produced outside CI carried an empty `firepanda_ref`. Six did. Four others carried a commit from a checkout that had been replaced two releases earlier, which is worse, because it attributes the numbers to the wrong build and says so with the same confidence as a correct one. The run probes the machine it is running on, and an `env.json` is now only consulted for the fields the probe could not fill.

`validate_results.py` rejects an empty `firepanda_ref` or `mojo_version` on any file where firepanda ran. It only asks when firepanda ran, because a pandas against Polars run on a machine with no Mojo installed owes neither field.

### firepanda runs thirteen of the fifteen db-benchmark queries

q1, q2, q3, q7 and q10 were reported as unsupported because they group by a string column and firepanda could not hold one in a `DataFrame`. It can now, so the driver implements them and the five empty cells are filled with measurements. What is left is q8, which needs a top-k per group, and q9, which needs a correlation. Both say so in the table.

The driver builds id1, id2 and id3 the way `tools/data.py` does, which is `"id"` and the draw with no one added, while the integer keys beside them are one based. That asymmetry is in the h2oai generator and reproducing it is the whole point: a text column that differs from the Parquet file by one would fail the agreement check for a reason nobody would guess at.

### The driver digests text columns

An answer with a string key was previously fingerprinted on its row count and its numeric sums alone, because the Mojo driver sent no text digests and the harness will not compare what one side did not send. The driver now computes the same summed 64 bit FNV-1a the harness computes, so all four engines are compared on the group keys as well as on the totals. At ten million rows, in both io modes, all four agree on all thirteen.

### firepanda holds the whole table now, and its memory numbers moved

The driver used to generate only the columns a query reads, so q4 was measured against a four column table while pandas, Polars and DuckDB were handed the whole nine column one. That flattered firepanda's peak resident memory on every group by query. It now builds all nine columns for every group by query, which is what the other three engines are given, and its memory numbers on q4, q5 and q6 are correspondingly higher than in v0.1.0. They are comparable now and they were not before.

### Latency, beyond the median

Every measurement carries `p90_s` and `p99_s` alongside the median and the interquartile range, and the report has two new tables: the ninety ninth percentile with its ratio to the median, and CPU seconds per run with how many cores that came to. An engine four times faster on sixteen cores and one four times faster on one core are not the same result and wall clock cannot tell them apart. At five runs a p99 is the slowest warm run, and `_percentile` says so rather than interpolating a value between two runs that were never observed.

### Engine versions

pandas 3.0.5, Polars 1.44.1, DuckDB 1.5.5, pyarrow 25.0.0. `pixi update` moves none of them: those are the newest builds in the channel as of this entry.

### Result files are no longer kept in the repository

Result files are no longer kept in the repository. The eight files that were committed for v0.1.0 have been removed from the history, `results/*.json` is ignored, and the benchmark workflow no longer pushes a results commit back to the branch. A run's files reach the site through the workflow artifact instead, and the artifact is the copy to download if you want to replay a published number with `pixi run repro`.

The publish job lost `contents: write` and its push token along with the commit step. It now only downloads the artifacts, builds the site and deploys it.

One thing did get worse and it is worth naming. The history chart on the site covered every run that had ever been committed, and it now covers what the current run produced. Restoring it means keeping the files somewhere that is not the source tree, an orphan branch or a release asset, and that is not done here.

## v0.1.0

The first version where both public suites run end to end with every engine present.

### Suites

db-benchmark and TPC-H both complete. Every query in both suites runs against pandas, Polars, DuckDB and firepanda, in two io modes: `memory` hands every engine the same Arrow table, and `scan` lets an engine that can push a projection into the Parquet file do it. Those two are separate measurements and the harness will not mix them.

TPC-H data and query text come from DuckDB's `tpch` extension, and every implementation is checked against `tpch_answers()` before any timing happens. `pixi run validate-tpch` passes 66 of 66 at SF1.

### Correctness

Answers are compared across engines by row count, per column sums and per text column digests. Numbers compare at a relative tolerance of 1e-7, which is loose enough for the float noise that cannot be avoided here and tight enough to catch a dropped group. Text columns carry a summed 64 bit FNV-1a and compare exactly, because a relative tolerance on a value near two to the sixty four is the same as not comparing at all.

`pixi run check-sweep --crashes-only` is a hard gate in CI. An engine that raised is a hole in the table rather than a slow result, and twice now a harness bug has been published as a column of skips that read like a missing feature.

### Result files

Named `<date>-<host>-<suite>-<size>-<io>.json`. The host is in the name because two machines running the same thing on the same day used to produce the same file name, and the second one copied into `results/` replaced the first with nothing noticing.

Every file names every engine version, the Mojo toolchain and the firepanda commit. `firepanda_ref` is read from the `FIREPANDA_REF` environment variable or a `GIT_REF` file, so it is populated on the benchmark machines, which receive the checkout as a tarball with `.git` excluded.

Eight files were produced for this version: db-benchmark at 0.5GB and TPC-H at SF1, each in both io modes, on an AMD EPYC VPS and on an i9-13900K. Zero disagreements and zero crashes in all eight. They were committed here originally and are not in the tree any more, for the reason under Unreleased.

### Site

One history chart per machine, suite, size and io mode. Nothing is drawn across two of those, because nothing is comparable across them.

### Fixed

DuckDB failed all five db-benchmark joins. The tables are called `left` and `right_small` and LEFT is a reserved word, so the parser error pointed at the alias rather than at the table.

DuckDB failed every query in both suites in `scan` mode. The Parquet view was created with a bound parameter, and DuckDB refuses to prepare a DDL statement carrying one.

TPC-H q20 failed on all three engines, on a guard that refused to fingerprint an answer with no numeric column. Q20 legitimately answers with two string columns.

TPC-H q3, q7, q15 and q19 were reported as disagreements on a run that had just matched the published answers. Agreement was hash equality and the hash rounds to nine significant figures before hashing, while pandas has to carry money in float64 because Arrow refuses the precision the exact arithmetic needs.

String key columns were absent from the fingerprint entirely, so two engines that grouped by different keys and landed on the same group count and the same totals were recorded as agreeing.

### CI

Every action is pinned to a commit hash. Every checkout sets `persist-credentials: false`, and the one job that pushes gets the token on the single command that needs it. Every dispatch input reaches a shell as an environment variable rather than as a template expansion. `zizmor --persona regular` is clean.
