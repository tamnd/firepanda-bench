"""The eight row hits table both ClickBench port tests run against.

Shared rather than copied, because the value of this fixture is that every engine
is compared on exactly the same rows, and two copies drift the moment one of them
is edited to catch something.

Eight rows is fewer than any `LIMIT` in the suite takes, so every query returns
everything that survives its filter and no query has to choose between rows that
tie on the ordering expression. That matters: thirteen of the 43 do not have a
determined answer at a real size, and against those thirteen a cross engine
comparison is measuring which way each engine broke a tie rather than whether
either is right. Eight rows takes the ties off the table and leaves the part that
is actually a contract, which is the filter, the grouping, the aggregates, the
types and the column names.

What it does not cover is the top ten selection itself, and two aggregates hidden
behind a `HAVING COUNT(*) > 100000` that no eight row table can satisfy. The
patterns at the bottom of this file are what the port tests use to take those
clauses back off the published statement so the rest of the answer is comparable.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# Eight rows in the types the real file has, which is not the types the published
# schema has: the date is a count of days, the timestamps are counts of seconds
# and every text column is bytes with no logical type. Both engines convert on the
# way in and this fixture is written the raw way so that both conversions are
# under test rather than bypassed.
#
# The values are chosen so nothing is trivially empty, and then chosen again so
# that breaking a filter or an aggregate on purpose actually changes an answer.
# Two rows carry the user identifier q19 looks for. Two carry the referer hash q40
# filters on, with one of each of the two traffic source values q40 accepts, so
# dropping one of them is visible. Two carry the URL hash q41 filters on and both
# survive its other three conditions. One URL contains `.google.` so q22's negated
# filter has something to exclude and one referer does not match q28's pattern at
# all. Two rows land in the same group with the refresh flag set on both, so a sum
# over that flag is not the same number as a maximum. Two rows are the same user in
# the same region, so a distinct count is not the same number as a count. Several
# URLs and referers carry Cyrillic, so a byte length is not a character length.
ROWS = 8

FIXTURE = {
    "WatchID": pa.array([9123146090114127052, 2, 3, 9123146090114127052, 5, 6, 7, 8], pa.int64()),
    "Title": [
        "Google поиск",
        "Google",
        "Магазин",
        "Google новости",
        "Google карта",
        "",
        "Магазин",
        "Новости",
    ],
    # Seconds since the epoch, each one inside the day its own EventDate names.
    # Rows three and seven are the two rows q42 keeps that land in the same hour and
    # in different minutes, so truncating to the hour instead of to the minute puts
    # them in one group and the row count of the answer changes.
    "EventTime": pa.array(
        [
            1372636800,
            1373763660,
            1373846430,
            1375236000,
            1375236060,
            1373846490,
            1373846500,
            1373328200,
        ],
        pa.int64(),
    ),
    "ClientEventTime": pa.array([1372636801] * ROWS, pa.int64()),
    "LocalEventTime": pa.array([1372636802] * ROWS, pa.int64()),
    # 15887 is 2013-07-01, 15900 is 2013-07-14, 15901 is 2013-07-15 and 15917 is
    # 2013-07-31, which are the four boundaries the last seven queries filter on.
    "EventDate": pa.array([15887, 15900, 15901, 15917, 15917, 15901, 15901, 15895], pa.uint16()),
    "CounterID": pa.array([62, 62, 62, 62, 62, 7, 62, 62], pa.int32()),
    "ClientIP": pa.array([1000, -2000, 1000, 1000, 4000, 1000, 5000, -2000], pa.int32()),
    "RegionID": pa.array([1, 2, 1, 3, 2, 1, 4, 2], pa.int32()),
    # Three users appear on more than one row, which is what makes a distinct count
    # a different number from a count. They repeat inside a region for q8, inside a
    # phone model for q10 and q11 and inside a search phrase for q13, so all four of
    # those would count the same user twice if they counted rows instead of users.
    "UserID": pa.array(
        [435090932899640449, 100, 200, 435090932899640449, 300, 200, 200, 300], pa.int64()
    ),
    "URL": [
        "http://example.ru/google/страница",
        "http://www.google.com/search",
        "http://shop.ru/тур",
        "",
        "http://x.ru/.google./a",
        "http://example.ru/google/другая",
        "http://shop.ru/тур",
        "http://other.ru/page",
    ],
    "Referer": [
        "http://www.example.ru/a/b",
        "https://go.mail.ru/search?q=x",
        "",
        "http://example.ru/x",
        "мусор",
        "",
        "https://www.other.org/страница",
        "http://example.ru/y",
    ],
    "IsRefresh": pa.array([1, 0, 0, 1, 0, 0, 0, 0], pa.int16()),
    "ResolutionWidth": pa.array([1024, 1280, 1920, 800, 1024, 1366, 1920, 1280], pa.int16()),
    "MobilePhone": pa.array([0, 1, 0, 2, 1, 0, 3, 1], pa.int16()),
    "MobilePhoneModel": ["", "iPhone", "", "Galaxy", "iPhone", "", "Nokia", "iPhone"],
    "TraficSourceID": pa.array([-1, 6, 6, 2, 0, 6, -1, 6], pa.int16()),
    "SearchEngineID": pa.array([0, 3, 0, 0, 0, 0, 0, 2], pa.int16()),
    "SearchPhrase": [
        "тур в турцию",
        "",
        "купить",
        "тур в турцию",
        "карта",
        "тур в турцию",
        "купить",
        "новости",
    ],
    "AdvEngineID": pa.array([0, 2, 0, 0, 3, 0, 0, 1], pa.int16()),
    "WindowClientWidth": pa.array([1000, 1200, 1900, 780, 1000, 1300, 1900, 1200], pa.int16()),
    "WindowClientHeight": pa.array([700, 800, 1000, 500, 700, 900, 1000, 800], pa.int16()),
    "IsLink": pa.array([1, 0, 1, 0, 0, 0, 1, 0], pa.int16()),
    "IsDownload": pa.array([0, 0, 0, 1, 0, 0, 0, 0], pa.int16()),
    "DontCountHits": pa.array([0, 0, 0, 0, 1, 0, 0, 0], pa.int16()),
    "RefererHash": pa.array(
        [10, 11, 12, 13, 14, 15, 3594120000172545465, 3594120000172545465], pa.int64()
    ),
    "URLHash": pa.array(
        [20, 2868770270353813622, 22, 23, 24, 25, 2868770270353813622, 27], pa.int64()
    ),
}


def hits_file(directory: Path) -> str:
    """Writes the fixture as a partition of the hits table.

    Named `hits_0.parquet` and handed back as a glob, because that is the shape
    both engines are given for this suite: the real dataset is a hundred files and
    nothing downstream has a path to a single one.

    Args:
        directory: Where to write it.

    Returns:
        The glob matching the partition.
    """
    columns = {}
    for name, values in FIXTURE.items():
        if isinstance(values, list):
            columns[name] = pa.array([v.encode() for v in values], pa.binary())
        else:
            columns[name] = values
    pq.write_table(pa.table(columns), directory / "hits_0.parquet")
    return str(directory / "hits_*.parquet")


NAMES = [f"q{index}" for index in range(43)]


# What the published statements do to an answer after the grouping is finished.
# Five queries page in past row one thousand or row ten thousand and two drop every
# group with fewer than a hundred thousand rows, so on an eight row fixture those
# seven return nothing at all and comparing them compares two empty frames. Taking
# the narrowing off both sides puts the filter, the grouping and the aggregates
# back under test.
#
# The SQL is not being rewritten to make a port look better. It is the same
# statement with its last clause removed, on both sides, and each port is narrowed
# by exactly three functions so removing it there is removing the same thing.
NARROWING = re.compile(r"\s+HAVING COUNT\(\*\) > \d+|\s+LIMIT \d+(\s+OFFSET \d+)?$")


# The other half of the last clause, which a cross engine comparison cannot see.
# An eight row fixture returns everything, so a query that took the wrong number of
# rows, paged from the wrong place or sorted the wrong way round still produces the
# same answer, and `engines.digest` does not depend on row order anyway. These two
# patterns pull the number, the offset and the direction out of the published
# statement so a test can check them against the arguments a port actually passes.
TAIL = re.compile(r"\s+LIMIT (\d+)(?:\s+OFFSET (\d+))?;?$")
ORDERING = re.compile(r"\bORDER BY (.+?)\s+LIMIT")


def record(real, calls):
    """Wraps a helper so a test can see what it was called with.

    Args:
        real: The helper being wrapped.
        calls: The list to append to, one entry per call.

    Returns:
        A stand in that records the call by argument name and then does the real
        work, so the query it is wrapped into still returns a real answer.
    """
    signature = inspect.signature(real)

    def wrapper(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        calls.append((real.__name__, bound.arguments))
        return real(*args, **kwargs)

    return wrapper
