#!/usr/bin/env python3
"""Ten ClickBench queries computed a second time, by hand, in plain Python.

TPC-H has `tpch_answers()`, the validation output the TPC publishes with the
specification, and `validate_tpch.py` checks all 66 implementations against it cell
by cell. That check found two real problems before any number was published.
ClickBench publishes nothing of the sort, so the strongest check this repository
has does not exist for this suite and something has to stand in for it.

The cross engine fingerprint does not stand in for it. Four engines agreeing means
four ports made the same decision, and if the decision was wrong the agreement
hides it. DuckDB runs the published SQL unmodified so it is the closest thing to an
authority here, but it is also one of the four engines in the table, which is
exactly the difference from TPC-H: there, an engine that agrees with the other
three and disagrees with the published answers is wrong, and here there is nothing
to disagree with.

So this is a fourth implementation of ten of the queries, written from the SQL text
and not from any of the three ports, in plain Python over the raw Parquet columns.
It shares no code path with anything it checks: no SQL planner, no dataframe
library, no group by kernel, no shared type conversion. It reads bytes out of the
file, decodes them itself and loops. It is slow on purpose and it never runs inside
a measurement.

The ten are the ones where a port had to make a decision rather than transcribe
one: an exact distinct count (q4, q8), a byte length that is not a character length
(q27, q28), a regular expression with a capture group (q28), a group by on a
constant (q34), a `NOT LIKE` next to an empty string comparison (q22), a `SELECT *`
that has to carry all 105 columns through the type conversions (q23), a `CASE` used
as a grouping key (q39), and two where the statement does not determine which rows
come back (q17, q25).

What comes out is a `Hand`, which carries the whole answer before the limit is
applied, so the validator can also work out for itself which rows the statement
actually pins down. See `tools/validate_clickbench.py`, which is what runs this.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clickbench

# The epoch both stored time columns count from. `EventDate` is an unsigned count
# of days and the three `*EventTime` columns are signed counts of seconds, and the
# conversion is done here rather than borrowed from `clickbench.retype` so that a
# mistake in that function cannot be reproduced by the thing checking it.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class Hand:
    """One query's answer, computed by hand, before the limit is applied.

    Attributes:
        columns: The answer's column names, which are the names the SQL gives them.
        rows: The whole answer in the statement's order, with no limit and no
            offset taken off it.
        key: The positions of the columns the ORDER BY sorts on. Empty when the
            statement has no ORDER BY, which is q17 and only q17.
        offset: The OFFSET the statement takes.
        limit: The LIMIT the statement takes, or None when it has none.
    """

    columns: tuple[str, ...]
    rows: list[tuple]
    key: tuple[int, ...] = ()
    offset: int = 0
    limit: int | None = None
    _members: set | None = field(default=None, repr=False, compare=False)

    def answer(self) -> list[tuple]:
        """The rows the statement returns.

        Returns:
            The answer after the offset and the limit.
        """
        if self.limit is None:
            return self.rows[self.offset :]
        return self.rows[self.offset : self.offset + self.limit]

    def keys_of(self, rows: list[tuple]) -> list[tuple]:
        """Projects rows onto the ordering key.

        Args:
            rows: The rows.

        Returns:
            The key columns of each row, in the same order.
        """
        return [tuple(row[position] for position in self.key) for row in rows]

    def boundaries(self) -> list[int]:
        """The positions in the full answer where the statement cuts.

        A statement with an OFFSET cuts twice, and a tie at the first cut decides
        which rows are skipped just as a tie at the second decides which are kept.
        Both are boundaries and both are checked.

        Returns:
            The cut positions, as counts of rows before the cut.
        """
        if self.limit is None:
            return []
        cuts = [self.offset] if self.offset else []
        cuts.append(self.offset + self.limit)
        return [cut for cut in cuts if 0 < cut < len(self.rows)]

    def ties(self) -> dict[int, int]:
        """How many rows share the ordering key across each boundary.

        Returns:
            A mapping from the cut position to the size of the tied group, for the
            boundaries where the key cannot tell the last kept row from the first
            dropped one. Empty when the statement determines its answer.
        """
        if not self.limit:
            return {}
        if not self.key:
            # q17. No ORDER BY, so every boundary ties with everything.
            return {self.limit: len(self.rows)}
        keys = self.keys_of(self.rows)
        found = {}
        for cut in self.boundaries():
            if keys[cut - 1] != keys[cut]:
                continue
            found[cut] = keys.count(keys[cut])
        return found

    def determined(self) -> bool:
        """Whether the statement pins down which rows come back.

        Returns:
            True when it does.
        """
        return not self.ties()

    def contains(self, row: tuple) -> bool:
        """Whether a row is somewhere in the full answer.

        The check that catches an engine which invented a row rather than picking a
        different legitimate one out of a tie. Weaker than comparing the answer and
        the only thing left when the answer is not determined.

        Args:
            row: The row.

        Returns:
            Whether it is a row of the answer before the limit.
        """
        if self._members is None:
            self._members = set(self.rows)
        return row in self._members


class Hits:
    """The raw hits table, with the type conversions done here rather than borrowed.

    Every accessor returns plain Python. That is the point: a list of `int` and a
    list of `str` cannot carry a dtype mistake, and the conversions below are
    written out again so that this does not agree with the ports by sharing their
    reading of the file.
    """

    def __init__(self, path: Path | str):
        """Reads the partition.

        Args:
            path: One Parquet file, or a directory holding the partitions.
        """
        root = Path(path)
        files = sorted(root.glob("hits_*.parquet")) if root.is_dir() else [root]
        self.table = pq.read_table(files)
        self.rows = self.table.num_rows
        self.names = list(self.table.schema.names)
        self._cache: dict[str, list] = {}

    def raw(self, name: str) -> list:
        """The column as the file stores it.

        Args:
            name: The column name.

        Returns:
            The values, cached.
        """
        if name not in self._cache:
            self._cache[name] = self.table.column(name).to_pylist()
        return self._cache[name]

    def ints(self, name: str) -> list[int]:
        """An integer column.

        Args:
            name: The column name.

        Returns:
            The values.
        """
        return self.raw(name)

    def bytes_(self, name: str) -> list[bytes]:
        """A text column as the bytes the file holds, which is what STRLEN counts.

        Args:
            name: The column name.

        Returns:
            The values.
        """
        return self.raw(name)

    def text(self, name: str) -> list[str]:
        """A text column decoded.

        The first of the five traps, done by hand. The file stores these as
        BYTE_ARRAY with no logical type on it, so nothing in the file says they are
        text and a reader that believes the file gets bytes. They are UTF-8 and the
        published schema says VARCHAR.

        Args:
            name: The column name.

        Returns:
            The values.
        """
        key = f"text:{name}"
        if key not in self._cache:
            column = self.raw(name)
            self._cache[key] = [
                value.decode("utf-8") if isinstance(value, bytes) else value for value in column
            ]
        return self._cache[key]

    def dates(self, name: str) -> list[date]:
        """A date column, stored as an unsigned count of days since the epoch.

        Args:
            name: The column name.

        Returns:
            The values.
        """
        key = f"date:{name}"
        if key not in self._cache:
            self._cache[key] = [date.fromordinal(EPOCH.toordinal() + v) for v in self.raw(name)]
        return self._cache[key]

    def times(self, name: str) -> list[datetime]:
        """A timestamp column, stored as a signed count of seconds since the epoch.

        Args:
            name: The column name.

        Returns:
            The values, without a time zone, which is what the published schema says.
        """
        key = f"time:{name}"
        if key not in self._cache:
            self._cache[key] = [
                datetime.fromtimestamp(v, tz=UTC).replace(tzinfo=None) for v in self.raw(name)
            ]
        return self._cache[key]


def q4(hits: Hits) -> Hand:
    """SELECT COUNT(DISTINCT UserID) FROM hits.

    Exactly distinct, which is the fifth trap. ClickHouse's published entry answers
    this with `uniq`, a HyperLogLog estimate, and a set has no such opinion.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    return Hand(("count(DISTINCT UserID)",), [(len(set(hits.ints("UserID"))),)])


def q8(hits: Hits) -> Hand:
    """SELECT RegionID, COUNT(DISTINCT UserID) AS u FROM hits GROUP BY RegionID ...

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    groups: dict[int, set] = {}
    for region, user in zip(hits.ints("RegionID"), hits.ints("UserID"), strict=True):
        groups.setdefault(region, set()).add(user)
    rows = [(region, len(users)) for region, users in groups.items()]
    rows.sort(key=lambda row: -row[1])
    return Hand(("RegionID", "u"), rows, key=(1,), limit=10)


def q17(hits: Hits) -> Hand:
    """SELECT UserID, SearchPhrase, COUNT(*) FROM hits GROUP BY UserID, SearchPhrase LIMIT 10.

    No ORDER BY, so any ten groups are a correct answer and the only thing this can
    check is that the ten an engine returned are real groups with the right counts.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    counts: dict[tuple, int] = {}
    for user, phrase in zip(hits.ints("UserID"), hits.text("SearchPhrase"), strict=True):
        counts[(user, phrase)] = counts.get((user, phrase), 0) + 1
    rows = [(user, phrase, count) for (user, phrase), count in counts.items()]
    return Hand(("UserID", "SearchPhrase", "count_star()"), rows, limit=10)


def q22(hits: Hits) -> Hand:
    """SELECT SearchPhrase, MIN(URL), MIN(Title), COUNT(*) AS c, COUNT(DISTINCT UserID) ...

    Three of the five traps in one statement. `Title LIKE '%Google%'` is a substring
    test on text that is bytes in the file, `URL NOT LIKE '%.google.%'` is the
    negated comparison that stops being harmless the moment an empty string becomes
    a null, and `SearchPhrase <> ''` is the comparison it would stop being harmless
    for. MIN over text is a byte order minimum, which is what Python's comparison on
    `str` gives for UTF-8.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    groups: dict[str, dict] = {}
    columns = zip(
        hits.text("SearchPhrase"),
        hits.text("Title"),
        hits.text("URL"),
        hits.ints("UserID"),
        strict=True,
    )
    for phrase, title, url, user in columns:
        if "Google" not in title or ".google." in url or phrase == "":
            continue
        group = groups.setdefault(phrase, {"url": url, "title": title, "c": 0, "users": set()})
        group["url"] = min(group["url"], url)
        group["title"] = min(group["title"], title)
        group["c"] += 1
        group["users"].add(user)
    rows = [(phrase, g["url"], g["title"], g["c"], len(g["users"])) for phrase, g in groups.items()]
    rows.sort(key=lambda row: -row[3])
    names = ("SearchPhrase", "min(URL)", "min(Title)", "c", "count(DISTINCT UserID)")
    return Hand(names, rows, key=(3,), limit=10)


def q23(hits: Hits) -> Hand:
    """SELECT * FROM hits WHERE URL LIKE '%google%' ORDER BY EventTime LIMIT 10.

    The only query that returns all 105 columns, so it is the only one where every
    type conversion has to be right at once rather than only the three columns a
    query touches.

    The filter runs first and the columns are materialized for the matching rows
    only. Decoding a hundred and five columns of a million rows into Python objects
    to keep ninety five of them is not a better check, it is the same check and an
    hour of somebody's afternoon.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    matched = [i for i, url in enumerate(hits.text("URL")) if "google" in url]
    values = []
    for name in hits.names:
        if name == "EventDate":
            column = hits.dates(name)
        elif name.endswith("EventTime"):
            column = hits.times(name)
        elif name in set(clickbench.TEXT_COLUMNS):
            column = hits.text(name)
        else:
            column = hits.raw(name)
        values.append([column[i] for i in matched])
    rows = [tuple(column[i] for column in values) for i in range(len(matched))]
    time_at = hits.names.index("EventTime")
    rows.sort(key=lambda row: row[time_at])
    return Hand(tuple(hits.names), rows, key=(time_at,), limit=10)


def q25(hits: Hits) -> Hand:
    """SELECT SearchPhrase FROM hits WHERE SearchPhrase <> '' ORDER BY SearchPhrase LIMIT 10.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    rows = [(phrase,) for phrase in hits.text("SearchPhrase") if phrase != ""]
    rows.sort()
    return Hand(("SearchPhrase",), rows, key=(0,), limit=10)


def q27(hits: Hits) -> Hand:
    """SELECT CounterID, AVG(STRLEN(URL)) AS l, COUNT(*) AS c ... HAVING COUNT(*) > 100000 ...

    The second trap. `STRLEN` is a byte count in DuckDB, so this averages the length
    of the encoded bytes and not the number of characters. On the real URL column
    those two averages are 88.56 and 86.57, which is over two percent, and the
    wrong one produces a perfectly plausible number.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    total: dict[int, list[int]] = {}
    for counter, url in zip(hits.ints("CounterID"), hits.bytes_("URL"), strict=True):
        if not url:
            continue
        seen = total.setdefault(counter, [0, 0])
        seen[0] += len(url)
        seen[1] += 1
    rows = [(c, s / n, n) for c, (s, n) in total.items() if n > 100000]
    rows.sort(key=lambda row: -row[1])
    return Hand(("CounterID", "l", "c"), rows, key=(1,), limit=25)


def q28(hits: Hits) -> Hand:
    """SELECT REGEXP_REPLACE(Referer, ...) AS k, AVG(STRLEN(Referer)) AS l, ... MIN(Referer) ...

    The pattern is anchored at both ends, so a referer that is not an http URL with
    a path does not match and comes back unchanged rather than empty. That case is
    most of the interesting behaviour here and an implementation that returns the
    empty string for it gets a different set of groups.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    pattern = re.compile(r"^https?://(?:www\.)?([^/]+)/.*$")
    groups: dict[str, dict] = {}
    for referer, raw in zip(hits.text("Referer"), hits.bytes_("Referer"), strict=True):
        if referer == "":
            continue
        key = pattern.sub(r"\1", referer)
        group = groups.setdefault(key, {"bytes": 0, "c": 0, "min": referer})
        group["bytes"] += len(raw)
        group["c"] += 1
        group["min"] = min(group["min"], referer)
    rows = [
        (key, g["bytes"] / g["c"], g["c"], g["min"]) for key, g in groups.items() if g["c"] > 100000
    ]
    rows.sort(key=lambda row: -row[1])
    return Hand(("k", "l", "c", "min(Referer)"), rows, key=(1,), limit=25)


def q34(hits: Hits) -> Hand:
    """SELECT 1, URL, COUNT(*) AS c FROM hits GROUP BY 1, URL ORDER BY c DESC LIMIT 10.

    `GROUP BY 1` is a group by a constant, which does nothing except appear in the
    plan. Some planners drop it and some do not, and dropping it by hand would be
    doing the optimizer's job and then reporting the result as the optimizer's work,
    so the constant stays in the grouping key here too.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    counts: dict[tuple, int] = {}
    for url in hits.text("URL"):
        counts[(1, url)] = counts.get((1, url), 0) + 1
    rows = [(one, url, count) for (one, url), count in counts.items()]
    rows.sort(key=lambda row: -row[2])
    return Hand(("1", "URL", "c"), rows, key=(2,), limit=10)


def q39(hits: Hits) -> Hand:
    """SELECT TraficSourceID, ..., CASE WHEN ... THEN Referer ELSE '' END AS Src, ...

    A CASE expression used as a grouping key, and an OFFSET, which gives the
    statement two boundaries where a tie matters instead of one.

    Args:
        hits: The table.

    Returns:
        The answer.
    """
    first, last = date(2013, 7, 1), date(2013, 7, 31)
    counts: dict[tuple, int] = {}
    columns = zip(
        hits.ints("CounterID"),
        hits.dates("EventDate"),
        hits.ints("IsRefresh"),
        hits.ints("TraficSourceID"),
        hits.ints("SearchEngineID"),
        hits.ints("AdvEngineID"),
        hits.text("Referer"),
        hits.text("URL"),
        strict=True,
    )
    for counter, day, refresh, traffic, search, advert, referer, url in columns:
        if counter != 62 or refresh != 0 or day < first or day > last:
            continue
        source = referer if search == 0 and advert == 0 else ""
        key = (traffic, search, advert, source, url)
        counts[key] = counts.get(key, 0) + 1
    rows = [(*key, count) for key, count in counts.items()]
    rows.sort(key=lambda row: -row[5])
    names = ("TraficSourceID", "SearchEngineID", "AdvEngineID", "Src", "Dst", "PageViews")
    return Hand(names, rows, key=(5,), offset=1000, limit=10)


# The ten, and nothing else. Not all 43, because forty three of these is a day of
# nobody's time well spent, and not zero, because a suite where no second
# implementation has ever produced a single answer is a suite that can be wrong in
# a way no check here would notice.
HAND = {
    "q4": q4,
    "q8": q8,
    "q17": q17,
    "q22": q22,
    "q23": q23,
    "q25": q25,
    "q27": q27,
    "q28": q28,
    "q34": q34,
    "q39": q39,
}
