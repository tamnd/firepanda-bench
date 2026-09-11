"""The 43 ClickBench queries in pandas.

This is the port that is supposed to hurt. pandas is the API firepanda is
replacing and the audience it is addressing, ClickBench is a hundred million rows
of real web traffic, and several of these queries take minutes. Two or three may
not finish inside the timeout at the published size. Both outcomes go in the
table, because a reader deciding whether to leave pandas is better served by a
number that is bad than by a blank cell.

The rule is the one the TPC-H port already follows. Idiomatic pandas, written the
way the documentation would have you write it, no cast the query did not ask for,
and where there are two idiomatic ways to say the same thing the faster one is
fine. Nothing here reaches under the frame for a numpy trick or pre-casts a key to
categorical to make a group by cheaper, because the person reading the table is
asking what happens when they write this query in pandas, not what happens when
somebody optimizing for a benchmark writes it.

Four places where that rule had to be applied rather than followed:

`STRLEN` in q27 and q28 is a byte count, and `Series.str.len` is a character
count. On this data they differ by more than two percent, so `.str.len` is not a
slower version of the right answer, it is the wrong answer. The documented pandas
way to get bytes is `.str.encode("utf-8").str.len()` and on an Arrow backed
Series it raises, because Arrow has no `utf8_length` kernel for binary. What is
left is a per element Python loop or the Arrow kernel that is already sitting
under the Series, and this takes the kernel. Choosing the Python loop would have
been putting a thumb on the scale: it is not more honest to make pandas slower at
something pandas can do quickly.

q23 is `SELECT *` with an `ORDER BY` and a `LIMIT`, so it sorts every surviving
row across 105 columns. pandas has `nsmallest`, which is cheaper and is not what
the query says. The sort is what the query says.

q29 sums ninety expressions over the same column, which in pandas is ninety full
passes. There is no vectorized form of that a normal person would write, so it is
ninety passes.

q28 needs a regular expression with a capture group, which is `Series.str.replace`
with `regex=True`, which is Python's `re` module called once per row. It may be
the slowest single cell in the whole published table across all four engines.

Every sort passes `kind="stable"`. That costs a little and it buys one thing worth
having: a pandas answer that is the same on every run over the same data. It does
not make pandas agree with DuckDB. Thirteen of these queries end with a limit
across a tie and therefore do not have one right answer at all, which is not
something a port can fix and is tracked separately.
"""

from __future__ import annotations

import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

# The published filter bounds, built once. Comparing an Arrow backed date32 column
# against a `datetime.date` is the comparison pandas supports; comparing it
# against the string the SQL carries is not.
JULY_FIRST = datetime.date(2013, 7, 1)
JULY_LAST = datetime.date(2013, 7, 31)
JULY_14 = datetime.date(2013, 7, 14)
JULY_15 = datetime.date(2013, 7, 15)

# q28's pattern, copied out of the published statement rather than rewritten. RE2
# is what DuckDB runs it through and `re` is what pandas runs it through, and on
# a pattern with no backtracking and one capture group the two agree.
REFERER_PATTERN = r"^https?://(?:www\.)?([^/]+)/.*$"


def named(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.DataFrame:
    """Puts DuckDB's own output names on an answer, positionally.

    The harness digests an answer into a sum per numeric column and a hash per
    text column, both keyed by column name, so two engines that computed the same
    answer under different column names are reported as disagreeing. DuckDB runs
    the published SQL and names its output whatever that SQL implies, which for
    an unaliased aggregate is something like `count_star()` or `sum(IsRefresh)`.
    Nobody would write those names in pandas, so the queries below use short
    working names and this puts the published ones on at the end.

    Positional rather than a mapping, because the column order also has to match
    and a rename would not have caught a port that produced the right columns in
    the wrong order.

    Args:
        frame: The answer.
        names: DuckDB's output names, in DuckDB's order.

    Returns:
        The same frame, renamed.

    Raises:
        ValueError: If the answer does not have as many columns as there are
            names, which means the port dropped or gained one.
    """
    if len(frame.columns) != len(names):
        raise ValueError(
            f"answer has {len(frame.columns)} columns and {len(names)} names were "
            f"given: {list(frame.columns)} against {list(names)}"
        )
    frame = frame.copy()
    frame.columns = list(names)
    return frame


def byte_length(column: pd.Series) -> pd.Series:
    """Returns the length of each value in bytes.

    This is `STRLEN` in the two queries that use it, and it is bytes rather than
    characters. See the note in the module docstring for why it is not
    `Series.str.len` and why it is not the documented `.str.encode` route either.

    Args:
        column: An Arrow backed text column.

    Returns:
        The byte length of each value, aligned with the input.
    """
    lengths = pc.binary_length(pa.array(column))
    return pd.Series(pd.arrays.ArrowExtensionArray(lengths), index=column.index)


def top(
    frame: pd.DataFrame,
    by: str | list[str],
    rows: int,
    offset: int = 0,
    ascending: bool = False,
) -> pd.DataFrame:
    """Sorts an answer and takes the rows a `LIMIT` and `OFFSET` would take.

    Args:
        frame: The answer before the limit.
        by: The ordering column or columns.
        rows: How many rows the limit takes.
        offset: How many rows the offset skips.
        ascending: Whether the sort is ascending.

    Returns:
        The rows that survive.
    """
    ordered = frame.sort_values(by, ascending=ascending, kind="stable")
    return ordered.iloc[offset : offset + rows]


def first(frame: pd.DataFrame, rows: int) -> pd.DataFrame:
    """Takes the rows a `LIMIT` with no `ORDER BY` under it takes.

    Only q17 has one. It is a function rather than a `head` call inline for the
    same reason `top` and `having` are: they are the three places a published
    statement narrows its answer after the grouping is done, and keeping them in
    one place each is what lets a test run every query with the narrowing taken
    off and compare the whole answer against DuckDB.

    Args:
        frame: The answer before the limit.
        rows: How many rows the limit takes.

    Returns:
        The rows that survive.
    """
    return frame.head(rows)


def having(frame: pd.DataFrame, column: str, minimum: int) -> pd.DataFrame:
    """Drops the groups a `HAVING COUNT(*) > n` drops.

    Args:
        frame: The grouped answer.
        column: The column holding the group's row count.
        minimum: The count a group has to exceed.

    Returns:
        The groups that survive.
    """
    return frame[frame[column] > minimum]


def counter62(hits: pd.DataFrame, start: datetime.date, end: datetime.date) -> pd.Series:
    """Builds the filter the last seven queries all start with.

    Args:
        hits: The table.
        start: The first date the range includes.
        end: The last date the range includes.

    Returns:
        A boolean mask.
    """
    return (hits["CounterID"] == 62) & (hits["EventDate"] >= start) & (hits["EventDate"] <= end)


def q0(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts every row.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return pd.DataFrame({"count_star()": [len(t["hits"])]})


def q1(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts the rows with an ad engine set.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    return pd.DataFrame({"count_star()": [int((hits["AdvEngineID"] != 0).sum())]})


def q2(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Takes three scalar aggregates over the whole table in one query.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    return pd.DataFrame(
        {
            "sum(AdvEngineID)": [int(hits["AdvEngineID"].sum())],
            "count_star()": [len(hits)],
            "avg(ResolutionWidth)": [float(hits["ResolutionWidth"].mean())],
        }
    )


def q3(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Averages a sixty four bit identifier, which is a scan and nothing else.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return pd.DataFrame({"avg(UserID)": [float(t["hits"]["UserID"].mean())]})


def q4(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts distinct users over the whole table.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    return pd.DataFrame({"count(DISTINCT UserID)": [int(t["hits"]["UserID"].nunique())]})


def q5(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts distinct search phrases, which is the same shape as q4 over text.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    phrases = t["hits"]["SearchPhrase"]
    return pd.DataFrame({"count(DISTINCT SearchPhrase)": [int(phrases.nunique())]})


def q6(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Takes the first and last date in the table.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    dates = t["hits"]["EventDate"]
    return pd.DataFrame(
        {"min(EventDate)": [dates.min()], "max(EventDate)": [dates.max()]},
        dtype="date32[day][pyarrow]",
    )


def q7(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by ad engine and orders by the count, with no limit on the end.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["AdvEngineID"] != 0]
    counts = frame.groupby("AdvEngineID", sort=False, observed=True).size().reset_index(name="c")
    return named(
        counts.sort_values("c", ascending=False, kind="stable"), ("AdvEngineID", "count_star()")
    )


def q8(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts distinct users per region.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    grouped = hits.groupby("RegionID", sort=False, observed=True)["UserID"].nunique()
    return top(grouped.reset_index(name="u"), "u", 10)


def q9(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Takes four aggregates per region, one of them a distinct count.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .groupby("RegionID", as_index=False, sort=False, observed=True)
        .agg(
            s=("AdvEngineID", "sum"),
            c=("ResolutionWidth", "size"),
            a=("ResolutionWidth", "mean"),
            u=("UserID", "nunique"),
        )
    )
    return named(
        top(grouped, "c", 10),
        ("RegionID", "sum(AdvEngineID)", "c", "avg(ResolutionWidth)", "count(DISTINCT UserID)"),
    )


def q10(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts distinct users per phone model, over a key that is mostly empty.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["MobilePhoneModel"] != ""]
    grouped = frame.groupby("MobilePhoneModel", sort=False, observed=True)["UserID"].nunique()
    return top(grouped.reset_index(name="u"), "u", 10)


def q11(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """The same as q10 with the phone as a second key.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["MobilePhoneModel"] != ""]
    keys = ["MobilePhone", "MobilePhoneModel"]
    grouped = frame.groupby(keys, sort=False, observed=True)["UserID"].nunique()
    return top(grouped.reset_index(name="u"), "u", 10)


def q12(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts rows per search phrase.

    Pairs with q13, which is the same group by counting distinct users instead, so
    the gap between the two is the cost of distinct counting and nothing else.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["SearchPhrase"] != ""]
    counts = frame.groupby("SearchPhrase", sort=False, observed=True).size()
    return top(counts.reset_index(name="c"), "c", 10)


def q13(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts distinct users per search phrase.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["SearchPhrase"] != ""]
    grouped = frame.groupby("SearchPhrase", sort=False, observed=True)["UserID"].nunique()
    return top(grouped.reset_index(name="u"), "u", 10)


def q14(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts rows per search engine and phrase.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["SearchPhrase"] != ""]
    counts = frame.groupby(["SearchEngineID", "SearchPhrase"], sort=False, observed=True).size()
    return top(counts.reset_index(name="c"), "c", 10)


def q15(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by user, which is a key with nearly as many values as there are rows.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    counts = t["hits"].groupby("UserID", sort=False, observed=True).size()
    return named(top(counts.reset_index(name="c"), "c", 10), ("UserID", "count_star()"))


def q16(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by user and phrase, and sorts the result.

    Pairs with q17, which is the same group by with the sort taken off.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    counts = t["hits"].groupby(["UserID", "SearchPhrase"], sort=False, observed=True).size()
    return named(
        top(counts.reset_index(name="c"), "c", 10), ("UserID", "SearchPhrase", "count_star()")
    )


def q17(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """The same group by as q16 with no ordering at all, so any ten groups are correct.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    counts = t["hits"].groupby(["UserID", "SearchPhrase"], sort=False, observed=True).size()
    return named(
        first(counts.reset_index(name="c"), 10), ("UserID", "SearchPhrase", "count_star()")
    )


def q18(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by user, phrase and the minute pulled out of the timestamp.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[["UserID", "EventTime", "SearchPhrase"]].assign(
        m=lambda f: f["EventTime"].dt.minute
    )
    counts = frame.groupby(["UserID", "m", "SearchPhrase"], sort=False, observed=True).size()
    return named(
        top(counts.reset_index(name="c"), "c", 10),
        ("UserID", "m", "SearchPhrase", "count_star()"),
    )


def q19(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Selects one user's rows by an exact match on a sixty four bit key.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    return hits.loc[hits["UserID"] == 435090932899640449, ["UserID"]]


def q20(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts the rows whose URL contains a substring.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    matches = t["hits"]["URL"].str.contains("google", regex=False)
    return pd.DataFrame({"count_star()": [int(matches.sum())]})


def q21(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups a substring filtered table by phrase and takes the smallest URL.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["URL"].str.contains("google", regex=False) & (hits["SearchPhrase"] != "")]
    grouped = frame.groupby("SearchPhrase", as_index=False, sort=False, observed=True).agg(
        u=("URL", "min"), c=("URL", "size")
    )
    return named(top(grouped, "c", 10), ("SearchPhrase", "min(URL)", "c"))


def q22(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Two substring filters, one of them negated, and a minimum over two text columns.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[
        hits["Title"].str.contains("Google", regex=False)
        & ~hits["URL"].str.contains(".google.", regex=False)
        & (hits["SearchPhrase"] != "")
    ]
    grouped = frame.groupby("SearchPhrase", as_index=False, sort=False, observed=True).agg(
        u=("URL", "min"), i=("Title", "min"), c=("URL", "size"), n=("UserID", "nunique")
    )
    return named(
        top(grouped, "c", 10),
        ("SearchPhrase", "min(URL)", "min(Title)", "c", "count(DISTINCT UserID)"),
    )


def q23(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Sorts a filtered table across all 105 columns and takes ten rows.

    `nsmallest` would be cheaper and would not be this query. The query sorts.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["URL"].str.contains("google", regex=False)]
    return top(frame, "EventTime", 10, ascending=True)


def q24(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Orders by a column it does not return.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits.loc[hits["SearchPhrase"] != "", ["SearchPhrase", "EventTime"]]
    return top(frame, "EventTime", 10, ascending=True)[["SearchPhrase"]]


def q25(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Orders by the text column it returns.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits.loc[hits["SearchPhrase"] != "", ["SearchPhrase"]]
    return top(frame, "SearchPhrase", 10, ascending=True)


def q26(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Orders by two columns, one of which it does not return.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits.loc[hits["SearchPhrase"] != "", ["SearchPhrase", "EventTime"]]
    return top(frame, ["EventTime", "SearchPhrase"], 10, ascending=True)[["SearchPhrase"]]


def q27(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Averages the byte length of a URL per counter, with a HAVING on the count.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits.loc[hits["URL"] != "", ["CounterID", "URL"]]
    frame = frame.assign(n=byte_length(frame["URL"]))
    grouped = frame.groupby("CounterID", as_index=False, sort=False, observed=True).agg(
        l=("n", "mean"), c=("n", "size")
    )
    return top(having(grouped, "c", 100000), "l", 25)


def q28(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by a capture group pulled out of the referer with a regular expression.

    This runs Python's `re` once per row, and on the published size it may be the
    slowest cell in the table for any engine.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits.loc[hits["Referer"] != "", ["Referer"]]
    frame = frame.assign(
        k=frame["Referer"].str.replace(REFERER_PATTERN, r"\1", regex=True),
        n=byte_length(frame["Referer"]),
    )
    grouped = frame.groupby("k", as_index=False, sort=False, observed=True).agg(
        l=("n", "mean"), c=("n", "size"), m=("Referer", "min")
    )
    return named(top(having(grouped, "c", 100000), "l", 25), ("k", "l", "c", "min(Referer)"))


def q29(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Sums ninety expressions over one column, which in pandas is ninety passes.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    column = t["hits"]["ResolutionWidth"]
    answer = {"sum(ResolutionWidth)": [int(column.sum())]}
    for offset in range(1, 90):
        answer[f"sum((ResolutionWidth + {offset}))"] = [int((column + offset).sum())]
    return pd.DataFrame(answer)


def q30(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by search engine and client address.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["SearchPhrase"] != ""]
    grouped = frame.groupby(
        ["SearchEngineID", "ClientIP"], as_index=False, sort=False, observed=True
    ).agg(c=("ResolutionWidth", "size"), s=("IsRefresh", "sum"), a=("ResolutionWidth", "mean"))
    return named(
        top(grouped, "c", 10),
        ("SearchEngineID", "ClientIP", "c", "sum(IsRefresh)", "avg(ResolutionWidth)"),
    )


def q31(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """The same as q30 keyed on the watch identifier, which is nearly unique.

    Pairs with q32, which is this query with the filter taken off.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    frame = hits[hits["SearchPhrase"] != ""]
    grouped = frame.groupby(["WatchID", "ClientIP"], as_index=False, sort=False, observed=True).agg(
        c=("ResolutionWidth", "size"), s=("IsRefresh", "sum"), a=("ResolutionWidth", "mean")
    )
    return named(
        top(grouped, "c", 10),
        ("WatchID", "ClientIP", "c", "sum(IsRefresh)", "avg(ResolutionWidth)"),
    )


def q32(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """q31 with no filter, so the group count goes to nearly one per row.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    grouped = (
        t["hits"]
        .groupby(["WatchID", "ClientIP"], as_index=False, sort=False, observed=True)
        .agg(c=("ResolutionWidth", "size"), s=("IsRefresh", "sum"), a=("ResolutionWidth", "mean"))
    )
    return named(
        top(grouped, "c", 10),
        ("WatchID", "ClientIP", "c", "sum(IsRefresh)", "avg(ResolutionWidth)"),
    )


def q33(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by the URL, which is the widest and most skewed key in the table.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    counts = t["hits"].groupby("URL", sort=False, observed=True).size()
    return top(counts.reset_index(name="c"), "c", 10)


def q34(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """q33 with a constant added to the key, which a planner should be able to drop.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    counts = t["hits"].groupby("URL", sort=False, observed=True).size()
    answer = top(counts.reset_index(name="c"), "c", 10)
    answer.insert(0, "1", 1)
    return answer


def q35(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by an address and three expressions over it that carry no information.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    frame = t["hits"][["ClientIP"]].copy()
    for offset in (1, 2, 3):
        frame[f"m{offset}"] = frame["ClientIP"] - offset
    keys = ["ClientIP", "m1", "m2", "m3"]
    counts = frame.groupby(keys, sort=False, observed=True).size()
    return named(
        top(counts.reset_index(name="c"), "c", 10),
        ("ClientIP", "(ClientIP - 1)", "(ClientIP - 2)", "(ClientIP - 3)", "c"),
    )


def q36(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Page views per URL for one counter over one month.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    mask = (
        counter62(hits, JULY_FIRST, JULY_LAST)
        & (hits["DontCountHits"] == 0)
        & (hits["IsRefresh"] == 0)
        & (hits["URL"] != "")
    )
    counts = hits[mask].groupby("URL", sort=False, observed=True).size()
    return top(counts.reset_index(name="PageViews"), "PageViews", 10)


def q37(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """q36 keyed on the title instead of the URL.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    mask = (
        counter62(hits, JULY_FIRST, JULY_LAST)
        & (hits["DontCountHits"] == 0)
        & (hits["IsRefresh"] == 0)
        & (hits["Title"] != "")
    )
    counts = hits[mask].groupby("Title", sort=False, observed=True).size()
    return top(counts.reset_index(name="PageViews"), "PageViews", 10)


def q38(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """The first of the queries that page into the result rather than taking the top.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    mask = (
        counter62(hits, JULY_FIRST, JULY_LAST)
        & (hits["IsRefresh"] == 0)
        & (hits["IsLink"] != 0)
        & (hits["IsDownload"] == 0)
    )
    counts = hits[mask].groupby("URL", sort=False, observed=True).size()
    return top(counts.reset_index(name="PageViews"), "PageViews", 10, offset=1000)


def q39(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by five keys, one of which is a conditional expression.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    mask = counter62(hits, JULY_FIRST, JULY_LAST) & (hits["IsRefresh"] == 0)
    rows = hits[mask]
    direct = (rows["SearchEngineID"] == 0) & (rows["AdvEngineID"] == 0)
    frame = rows[["TraficSourceID", "SearchEngineID", "AdvEngineID"]].assign(
        Src=rows["Referer"].where(direct, ""), Dst=rows["URL"]
    )
    keys = ["TraficSourceID", "SearchEngineID", "AdvEngineID", "Src", "Dst"]
    counts = frame.groupby(keys, sort=False, observed=True).size()
    return top(counts.reset_index(name="PageViews"), "PageViews", 10, offset=1000)


def q40(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by a URL hash and a date, under a filter on a referer hash.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    mask = (
        counter62(hits, JULY_FIRST, JULY_LAST)
        & (hits["IsRefresh"] == 0)
        & hits["TraficSourceID"].isin([-1, 6])
        & (hits["RefererHash"] == 3594120000172545465)
    )
    counts = hits[mask].groupby(["URLHash", "EventDate"], sort=False, observed=True).size()
    return top(counts.reset_index(name="PageViews"), "PageViews", 10, offset=100)


def q41(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Groups by window size, under a filter on a URL hash, and pages in ten thousand rows.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    mask = (
        counter62(hits, JULY_FIRST, JULY_LAST)
        & (hits["IsRefresh"] == 0)
        & (hits["DontCountHits"] == 0)
        & (hits["URLHash"] == 2868770270353813622)
    )
    keys = ["WindowClientWidth", "WindowClientHeight"]
    counts = hits[mask].groupby(keys, sort=False, observed=True).size()
    return top(counts.reset_index(name="PageViews"), "PageViews", 10, offset=10000)


def q42(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Counts page views per minute over two days, ordered by the minute itself.

    Args:
        t: The loaded tables.

    Returns:
        The answer.
    """
    hits = t["hits"]
    mask = (
        counter62(hits, JULY_14, JULY_15) & (hits["IsRefresh"] == 0) & (hits["DontCountHits"] == 0)
    )
    frame = hits[mask][["EventTime"]].assign(M=lambda f: f["EventTime"].dt.floor("min"))
    counts = frame.groupby("M", sort=False, observed=True).size()
    return top(counts.reset_index(name="PageViews"), "M", 10, offset=1000, ascending=True)


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
