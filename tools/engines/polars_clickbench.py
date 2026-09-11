"""The 43 ClickBench queries in Polars.

Polars is the bar. Its published ClickBench entry is competitive with the column
stores, which is not true of every dataframe library in that table, so this is the
port that says what a good answer looks like rather than the one that says what
the cost of a bad one is.

Every query is a `LazyFrame` chain ending in `collect`. That is what a Polars user
writes and it is what stops the harness from timing plan construction: a lazy
engine that is never asked for an answer has not done any work. In scan mode the
source is `pl.scan_parquet`, so the projection goes into the Parquet reader and
most of these queries touch three columns out of a hundred and five. In memory
mode the table is already in Arrow and the projection has nothing to push into.
The gap between those two numbers for the same query is the clearest measurement
in this repository of what projection pushdown over a wide table is worth.

The rule is the one the other ports follow. Idiomatic Polars, no cast the query
did not ask for, and where there are two idiomatic ways to say a thing the faster
one is fine. Four places where the rule had to be applied rather than followed:

`n_unique`, not `approx_n_unique`, in the eight queries that count something
distinct. The approximate one is faster and it is answering a different question.
ClickHouse's own published entry uses `uniq`, which is approximate, and that is
one reason its numbers on those rows are not comparable with a DuckDB number on
the same query. Everything here is exact and the report says so once, plainly.
Worth knowing that `n_unique` counts a null as a value where SQL's
`COUNT(DISTINCT x)` does not. The hits table has no nulls in any column these
eight touch, so the two agree here, and on a table that did they would not.

`str.len_bytes`, not `str.len_chars`, for `STRLEN` in q27 and q28. On this data
the two differ by more than two percent, so the character count is not a slower
route to the right answer, it is the wrong answer.

q29 is ninety expressions inside one `select`, which is the shape Polars is built
for and should be one pass over the column. Writing it as ninety separate `select`
calls would be measuring something else.

q34 groups by a constant, because `SELECT 1, URL, COUNT(*) ... GROUP BY 1, URL`
groups by a constant. Some planners remove it and some do not, and removing it by
hand here would be doing the optimizer's job and then reporting the result as the
optimizer's work.

Sorts pass `maintain_order=True`, which is the same call the pandas port makes and
for a narrower reason. It makes q23 through q26 deterministic, because those four
sort raw rows in scan order and nothing upstream of them reorders. It does not make
the grouped queries deterministic, because `group_by` is left unordered and a
stable sort over an unstable input is still unstable. Forcing order on the group by
would fix that and would cost far more than the determinism is worth, since thirteen
of these queries do not have one right answer at any setting.
"""

from __future__ import annotations

import datetime

import polars as pl

# The published filter bounds. A date column compares against a `datetime.date`
# and not against the string the SQL carries it as.
JULY_FIRST = datetime.date(2013, 7, 1)
JULY_LAST = datetime.date(2013, 7, 31)
JULY_14 = datetime.date(2013, 7, 14)
JULY_15 = datetime.date(2013, 7, 15)

# q28's pattern, copied out of the published statement rather than rewritten.
# DuckDB runs it through RE2 and Polars runs it through the regex crate, which
# agree on a pattern with one capture group and no backtracking. The replacement
# is spelled `$1` because that is the regex crate's spelling of a group reference.
REFERER_PATTERN = r"^https?://(?:www\.)?([^/]+)/.*$"
REFERER_REPLACEMENT = "$1"


def top(
    frame: pl.LazyFrame,
    by: str | list[str],
    rows: int,
    offset: int = 0,
    descending: bool = True,
) -> pl.LazyFrame:
    """Sorts an answer and takes the rows a `LIMIT` and `OFFSET` would take.

    Args:
        frame: The answer before the limit.
        by: The ordering column or columns.
        rows: How many rows the limit takes.
        offset: How many rows the offset skips.
        descending: Whether the sort is descending.

    Returns:
        The rows that survive.
    """
    return frame.sort(by, descending=descending, maintain_order=True).slice(offset, rows)


def first(frame: pl.LazyFrame, rows: int) -> pl.LazyFrame:
    """Takes the rows a `LIMIT` with no `ORDER BY` under it takes.

    Only q17 has one. It is a function rather than a `head` call inline for the
    same reason `top` and `having` are: they are the three places a published
    statement narrows its answer after the grouping is done, and keeping each in
    one place is what lets a test run every query with the narrowing taken off and
    compare the whole answer against DuckDB.

    Args:
        frame: The answer before the limit.
        rows: How many rows the limit takes.

    Returns:
        The rows that survive.
    """
    return frame.head(rows)


def having(frame: pl.LazyFrame, column: str, minimum: int) -> pl.LazyFrame:
    """Drops the groups a `HAVING COUNT(*) > n` drops.

    Args:
        frame: The grouped answer.
        column: The column holding the group's row count.
        minimum: The count a group has to exceed.

    Returns:
        The groups that survive.
    """
    return frame.filter(pl.col(column) > minimum)


def counter62(start: datetime.date, end: datetime.date) -> pl.Expr:
    """Builds the filter the last seven queries all start with.

    Args:
        start: The first date the range includes.
        end: The last date the range includes.

    Returns:
        The predicate.
    """
    return (
        (pl.col("CounterID") == 62) & (pl.col("EventDate") >= start) & (pl.col("EventDate") <= end)
    )


def q0(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts every row.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return t["hits"].select(pl.len().alias("count_star()")).collect()


def q1(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts the rows with an ad engine set.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return (
        t["hits"]
        .filter(pl.col("AdvEngineID") != 0)
        .select(pl.len().alias("count_star()"))
        .collect()
    )


def q2(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Takes three scalar aggregates over the whole table in one query.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return (
        t["hits"]
        .select(
            pl.col("AdvEngineID").sum().alias("sum(AdvEngineID)"),
            pl.len().alias("count_star()"),
            pl.col("ResolutionWidth").mean().alias("avg(ResolutionWidth)"),
        )
        .collect()
    )


def q3(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Averages a sixty four bit identifier, which is a scan and nothing else.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return t["hits"].select(pl.col("UserID").mean().alias("avg(UserID)")).collect()


def q4(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts distinct users over the whole table.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return t["hits"].select(pl.col("UserID").n_unique().alias("count(DISTINCT UserID)")).collect()


def q5(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts distinct search phrases, which is the same shape as q4 over text.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return (
        t["hits"]
        .select(pl.col("SearchPhrase").n_unique().alias("count(DISTINCT SearchPhrase)"))
        .collect()
    )


def q6(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Takes the first and last date in the table.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return (
        t["hits"]
        .select(
            pl.col("EventDate").min().alias("min(EventDate)"),
            pl.col("EventDate").max().alias("max(EventDate)"),
        )
        .collect()
    )


def q7(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Groups by ad engine and orders by the count, with no limit on the end.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return (
        t["hits"]
        .filter(pl.col("AdvEngineID") != 0)
        .group_by("AdvEngineID")
        .agg(pl.len().alias("count_star()"))
        .sort("count_star()", descending=True, maintain_order=True)
        .collect()
    )


def q8(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts distinct users per region.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = t["hits"].group_by("RegionID").agg(pl.col("UserID").n_unique().alias("u"))
    return top(grouped, "u", 10).collect()


def q9(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Takes four aggregates per region, one of them a distinct count.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .group_by("RegionID")
        .agg(
            pl.col("AdvEngineID").sum().alias("sum(AdvEngineID)"),
            pl.len().alias("c"),
            pl.col("ResolutionWidth").mean().alias("avg(ResolutionWidth)"),
            pl.col("UserID").n_unique().alias("count(DISTINCT UserID)"),
        )
    )
    return top(grouped, "c", 10).collect()


def q10(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts distinct users per phone model, over the rows that name one.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("MobilePhoneModel") != "")
        .group_by("MobilePhoneModel")
        .agg(pl.col("UserID").n_unique().alias("u"))
    )
    return top(grouped, "u", 10).collect()


def q11(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same as q10 with the phone added to the key, which multiplies the groups.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("MobilePhoneModel") != "")
        .group_by("MobilePhone", "MobilePhoneModel")
        .agg(pl.col("UserID").n_unique().alias("u"))
    )
    return top(grouped, "u", 10).collect()


def q12(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts rows per search phrase, which is the high cardinality text group by.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("SearchPhrase") != "")
        .group_by("SearchPhrase")
        .agg(pl.len().alias("c"))
    )
    return top(grouped, "c", 10).collect()


def q13(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same group by as q12 counting distinct users, so the pair is the cost of distinct.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("SearchPhrase") != "")
        .group_by("SearchPhrase")
        .agg(pl.col("UserID").n_unique().alias("u"))
    )
    return top(grouped, "u", 10).collect()


def q14(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts rows per engine and phrase, a two column key with one side small.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("SearchPhrase") != "")
        .group_by("SearchEngineID", "SearchPhrase")
        .agg(pl.len().alias("c"))
    )
    return top(grouped, "c", 10).collect()


def q15(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts rows per user, which is nearly one group per row.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = t["hits"].group_by("UserID").agg(pl.len().alias("count_star()"))
    return top(grouped, "count_star()", 10).collect()


def q16(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts rows per user and phrase, sorted.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = t["hits"].group_by("UserID", "SearchPhrase").agg(pl.len().alias("count_star()"))
    return top(grouped, "count_star()", 10).collect()


def q17(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same group by as q16 with no sort, which catches an engine that always sorts.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = t["hits"].group_by("UserID", "SearchPhrase").agg(pl.len().alias("count_star()"))
    return first(grouped, 10).collect()


def q18(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Groups by user, minute of the hour and phrase.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .with_columns(pl.col("EventTime").dt.minute().alias("m"))
        .group_by("UserID", "m", "SearchPhrase")
        .agg(pl.len().alias("count_star()"))
    )
    return top(grouped, "count_star()", 10).collect()


def q19(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Selects one user's rows by equality on a sixty four bit key.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return t["hits"].filter(pl.col("UserID") == 435090932899640449).select("UserID").collect()


def q20(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts the rows whose URL contains a substring.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return (
        t["hits"]
        .filter(pl.col("URL").str.contains("google", literal=True))
        .select(pl.len().alias("count_star()"))
        .collect()
    )


def q21(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Groups the matching rows by phrase and takes the smallest URL in each group.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("URL").str.contains("google", literal=True) & (pl.col("SearchPhrase") != ""))
        .group_by("SearchPhrase")
        .agg(pl.col("URL").min().alias("min(URL)"), pl.len().alias("c"))
    )
    return top(grouped, "c", 10).collect()


def q22(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same shape as q21 with a negated match and a distinct count added.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(
            pl.col("Title").str.contains("Google", literal=True)
            & ~pl.col("URL").str.contains(".google.", literal=True)
            & (pl.col("SearchPhrase") != "")
        )
        .group_by("SearchPhrase")
        .agg(
            pl.col("URL").min().alias("min(URL)"),
            pl.col("Title").min().alias("min(Title)"),
            pl.len().alias("c"),
            pl.col("UserID").n_unique().alias("count(DISTINCT UserID)"),
        )
    )
    return top(grouped, "c", 10).collect()


def q23(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Sorts the matching rows across all 105 columns and takes ten.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    frame = t["hits"].filter(pl.col("URL").str.contains("google", literal=True))
    return top(frame, "EventTime", 10, descending=False).collect()


def q24(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Takes ten search phrases by event time, projecting a column it does not order by.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    frame = t["hits"].filter(pl.col("SearchPhrase") != "")
    return top(frame, "EventTime", 10, descending=False).select("SearchPhrase").collect()


def q25(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Takes the ten smallest search phrases, which is a sort on the projected column.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    frame = t["hits"].filter(pl.col("SearchPhrase") != "")
    return top(frame, "SearchPhrase", 10, descending=False).select("SearchPhrase").collect()


def q26(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same as q24 with the phrase added as a tie break on the sort.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    frame = t["hits"].filter(pl.col("SearchPhrase") != "")
    return (
        top(frame, ["EventTime", "SearchPhrase"], 10, descending=False)
        .select("SearchPhrase")
        .collect()
    )


def q27(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Averages the byte length of the URL per counter, over the busy counters only.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("URL") != "")
        .group_by("CounterID")
        .agg(
            pl.col("URL").str.len_bytes().mean().alias("l"),
            pl.len().alias("c"),
        )
    )
    return top(having(grouped, "c", 100000), "l", 25).collect()


def q28(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Pulls the host out of the referer with a capture group and groups by it.

    This is the one query where Polars has a large structural advantage over the
    pandas port. The regex crate runs the substitution over the column, where
    pandas hands every row to Python's `re`.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("Referer") != "")
        .with_columns(
            pl.col("Referer").str.replace(REFERER_PATTERN, REFERER_REPLACEMENT).alias("k")
        )
        .group_by("k")
        .agg(
            pl.col("Referer").str.len_bytes().mean().alias("l"),
            pl.len().alias("c"),
            pl.col("Referer").min().alias("min(Referer)"),
        )
    )
    return top(having(grouped, "c", 100000), "l", 25).collect()


def q29(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Sums the same column ninety times with ninety different constants added.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    sums = [pl.col("ResolutionWidth").sum().alias("sum(ResolutionWidth)")]
    sums += [
        (pl.col("ResolutionWidth") + offset).sum().alias(f"sum((ResolutionWidth + {offset}))")
        for offset in range(1, 90)
    ]
    return t["hits"].select(sums).collect()


def q30(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Three aggregates per engine and client address.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("SearchPhrase") != "")
        .group_by("SearchEngineID", "ClientIP")
        .agg(
            pl.len().alias("c"),
            pl.col("IsRefresh").sum().alias("sum(IsRefresh)"),
            pl.col("ResolutionWidth").mean().alias("avg(ResolutionWidth)"),
        )
    )
    return top(grouped, "c", 10).collect()


def q31(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same as q30 keyed on the watch identifier, which is nearly unique.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(pl.col("SearchPhrase") != "")
        .group_by("WatchID", "ClientIP")
        .agg(
            pl.len().alias("c"),
            pl.col("IsRefresh").sum().alias("sum(IsRefresh)"),
            pl.col("ResolutionWidth").mean().alias("avg(ResolutionWidth)"),
        )
    )
    return top(grouped, "c", 10).collect()


def q32(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same as q31 with the filter taken off, so the group count goes to the row count.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .group_by("WatchID", "ClientIP")
        .agg(
            pl.len().alias("c"),
            pl.col("IsRefresh").sum().alias("sum(IsRefresh)"),
            pl.col("ResolutionWidth").mean().alias("avg(ResolutionWidth)"),
        )
    )
    return top(grouped, "c", 10).collect()


def q33(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Counts rows per URL, which is the widest text key in the table.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = t["hits"].group_by("URL").agg(pl.len().alias("c"))
    return top(grouped, "c", 10).collect()


def q34(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same as q33 with a constant added to the key, which is a no op a planner may remove.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .with_columns(pl.lit(1, dtype=pl.Int32).alias("1"))
        .group_by("1", "URL")
        .agg(pl.len().alias("c"))
    )
    return top(grouped, "c", 10).collect()


def q35(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Groups by the client address and three shifted copies of it.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .with_columns(
            (pl.col("ClientIP") - 1).alias("(ClientIP - 1)"),
            (pl.col("ClientIP") - 2).alias("(ClientIP - 2)"),
            (pl.col("ClientIP") - 3).alias("(ClientIP - 3)"),
        )
        .group_by("ClientIP", "(ClientIP - 1)", "(ClientIP - 2)", "(ClientIP - 3)")
        .agg(pl.len().alias("c"))
    )
    return top(grouped, "c", 10).collect()


def q36(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Page views per URL for one counter over July.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(
            counter62(JULY_FIRST, JULY_LAST)
            & (pl.col("DontCountHits") == 0)
            & (pl.col("IsRefresh") == 0)
            & (pl.col("URL") != "")
        )
        .group_by("URL")
        .agg(pl.len().alias("PageViews"))
    )
    return top(grouped, "PageViews", 10).collect()


def q37(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """The same as q36 keyed on the page title.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(
            counter62(JULY_FIRST, JULY_LAST)
            & (pl.col("DontCountHits") == 0)
            & (pl.col("IsRefresh") == 0)
            & (pl.col("Title") != "")
        )
        .group_by("Title")
        .agg(pl.len().alias("PageViews"))
    )
    return top(grouped, "PageViews", 10).collect()


def q38(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Page views per link URL, paged past the first thousand.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(
            counter62(JULY_FIRST, JULY_LAST)
            & (pl.col("IsRefresh") == 0)
            & (pl.col("IsLink") != 0)
            & (pl.col("IsDownload") == 0)
        )
        .group_by("URL")
        .agg(pl.len().alias("PageViews"))
    )
    return top(grouped, "PageViews", 10, offset=1000).collect()


def q39(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Page views by traffic source, with the referer blanked out for paid traffic.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    source = (
        pl.when((pl.col("SearchEngineID") == 0) & (pl.col("AdvEngineID") == 0))
        .then(pl.col("Referer"))
        .otherwise(pl.lit(""))
        .alias("Src")
    )
    grouped = (
        t["hits"]
        .filter(counter62(JULY_FIRST, JULY_LAST) & (pl.col("IsRefresh") == 0))
        .with_columns(source, pl.col("URL").alias("Dst"))
        .group_by("TraficSourceID", "SearchEngineID", "AdvEngineID", "Src", "Dst")
        .agg(pl.len().alias("PageViews"))
    )
    return top(grouped, "PageViews", 10, offset=1000).collect()


def q40(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Page views per URL hash and date, for the rows referred by one page.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(
            counter62(JULY_FIRST, JULY_LAST)
            & (pl.col("IsRefresh") == 0)
            & pl.col("TraficSourceID").is_in([-1, 6])
            & (pl.col("RefererHash") == 3594120000172545465)
        )
        .group_by("URLHash", "EventDate")
        .agg(pl.len().alias("PageViews"))
    )
    return top(grouped, "PageViews", 10, offset=100).collect()


def q41(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Page views per window size, for one page, paged past the first ten thousand.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(
            counter62(JULY_FIRST, JULY_LAST)
            & (pl.col("IsRefresh") == 0)
            & (pl.col("DontCountHits") == 0)
            & (pl.col("URLHash") == 2868770270353813622)
        )
        .group_by("WindowClientWidth", "WindowClientHeight")
        .agg(pl.len().alias("PageViews"))
    )
    return top(grouped, "PageViews", 10, offset=10000).collect()


def q42(t: dict[str, pl.LazyFrame]) -> pl.DataFrame:
    """Page views per minute across two days, ordered by the minute rather than the count.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .filter(
            counter62(JULY_14, JULY_15)
            & (pl.col("IsRefresh") == 0)
            & (pl.col("DontCountHits") == 0)
        )
        .with_columns(pl.col("EventTime").dt.truncate("1m").alias("M"))
        .group_by("M")
        .agg(pl.len().alias("PageViews"))
    )
    return top(grouped, "M", 10, offset=1000, descending=False).collect()


QUERIES = {
    "q0": q0,
    "q1": q1,
    "q2": q2,
    "q3": q3,
    "q4": q4,
    "q5": q5,
    "q6": q6,
    "q7": q7,
    "q8": q8,
    "q9": q9,
    "q10": q10,
    "q11": q11,
    "q12": q12,
    "q13": q13,
    "q14": q14,
    "q15": q15,
    "q16": q16,
    "q17": q17,
    "q18": q18,
    "q19": q19,
    "q20": q20,
    "q21": q21,
    "q22": q22,
    "q23": q23,
    "q24": q24,
    "q25": q25,
    "q26": q26,
    "q27": q27,
    "q28": q28,
    "q29": q29,
    "q30": q30,
    "q31": q31,
    "q32": q32,
    "q33": q33,
    "q34": q34,
    "q35": q35,
    "q36": q36,
    "q37": q37,
    "q38": q38,
    "q39": q39,
    "q40": q40,
    "q41": q41,
    "q42": q42,
}
