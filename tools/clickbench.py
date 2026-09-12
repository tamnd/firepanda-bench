#!/usr/bin/env python3
"""The hits table, downloaded rather than generated, because there is no generator.

Every other suite here makes its own data. db-benchmark comes out of a splitmix64
counter stream, which is what lets firepanda produce the same bytes without owning
a Parquet decoder, and the ingestion files come out of the same generator. TPC-H
already broke that by coming from `dbgen`, and `tpch.py` handles it by asking
DuckDB's extension to run the generator. ClickBench breaks it harder: the hits
table is a dump of what a real product recorded, there is no seed and no
generator anywhere, and the only way to get it is to download it.

That is not a footnote. It is the reason this suite finds things the other three
cannot. A generator produces uniform key distributions, tidy types and no missing
data unless somebody works at making it not, and everything in this library has
been optimized against exactly that. The hits table has skewed cardinalities, a
`URL` column with a heavy tail, empty strings standing in for nulls, and 105
columns of which most queries touch three.

The files come in two forms and this uses the partitioned one. There is a single
`hits.parquet` of 14,779,976,446 bytes holding all 99,997,497 rows, and there are
one hundred `hits_{0..99}.parquet` of about 122 MB each holding the same rows cut
a hundred ways. Downloading one partition is what makes a CI size possible at
all: a job that has to pull fourteen gigabytes before it can check that four
engines agree is a job nobody will keep green.

Nothing here converts to CSV. ClickBench publishes a TSV form, nobody benchmarks
against it any more, the ingestion suite is where a reader gets measured, and a
seventy gigabyte text file in the cache helps nothing.

Usage:
    python tools/clickbench.py --size 1M
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

# Where the partitions live. This is the `hits_compatible` form, which is the one
# every engine in the published table reads, rather than the ClickHouse native
# form which is a different schema.
BASE_URL = "https://datasets.clickhouse.com/hits_compatible/athena_partitioned"

# How many partitions each size takes, from the front. The names are row counts
# rather than byte counts, matching the ingestion suite, because what varies
# between these is how many rows there are and not what shape they are in.
#
# Only `100M` is ClickBench. The other two exist because a suite that can only be
# run on one machine with a spare hundred gigabytes is a suite that gets run four
# times a year, and because the verify job needs a size it can pull inside a
# normal CI run. A number from a partial size is not comparable to a published
# one and the report labels it on the table rather than in a footnote.
SIZES = {"1M": 1, "10M": 10, "100M": 100}

# How a path to this dataset is written, since it is the only one here that is more
# than one file per table. `table_paths` hands an engine `hits_*.parquet` rather than
# a single name, DuckDB reads that pattern as it stands, and everything else expands
# it with `partitions` below so that all four engines read the same files in the same
# order.
PARTITION_GLOB = "{table}_*.parquet"

# What the file stores against what the published schema says it holds.
#
# `EventDate` is an unsigned sixteen bit count of days since the epoch and the
# schema calls it a DATE. The three `*EventTime` columns are signed sixty four bit
# counts of seconds and the schema calls them TIMESTAMP. Every text column is
# BYTE_ARRAY with no logical type on it, so a reader that believes the file gets
# binary where the schema says VARCHAR.
#
# None of that is a detail of the Parquet encoding that a reader may reasonably
# ignore. ClickBench's own DuckDB loader converts all four integer columns on the
# way in and passes `binary_as_string`, and the published numbers are numbers for
# queries that ran against the converted types. An engine here that skips any of it
# is not running the benchmark.
#
# Only `EventDate` and `EventTime` are named by the 43 queries. The other two are
# converted anyway because q23 selects all 105 columns, which puts their type in an
# answer that four engines have to agree on.
DATE_COLUMN = "EventDate"
TIMESTAMP_COLUMNS = ("EventTime", "ClientEventTime", "LocalEventTime")

# The 28 columns the file stores as BYTE_ARRAY and the published schema calls
# VARCHAR, listed rather than derived so that an engine can be asked whether it
# ended up with text without being asked what the file said.
#
# This is the first of the five traps in `suites/clickbench/README.md` and the
# reason it is worth a list is that getting it wrong is quiet. Six of the 43
# statements fail to bind against binary, which is loud, and another nine answer
# with bytes where they should answer with text, which is not: the row counts are
# right, the values are right, and the cross engine fingerprint hashes bytes and
# text through the same function, so the agreement check passes and the table
# looks finished. So every engine's loader hands its own idea of which columns are
# text to `check_text` and the run stops there rather than at a number nobody can
# see is wrong.
TEXT_COLUMNS = (
    "Title",
    "URL",
    "Referer",
    "FlashMinor2",
    "UserAgentMinor",
    "MobilePhoneModel",
    "Params",
    "SearchPhrase",
    "PageCharset",
    "OriginalURL",
    "HitColor",
    "BrowserLanguage",
    "BrowserCountry",
    "SocialNetwork",
    "SocialAction",
    "SocialSourcePage",
    "ParamOrderID",
    "ParamCurrency",
    "OpenstatServiceName",
    "OpenstatCampaignID",
    "OpenstatAdID",
    "OpenstatSourceID",
    "UTMSource",
    "UTMMedium",
    "UTMCampaign",
    "UTMContent",
    "UTMTerm",
    "FromTag",
)

# The published row count of the whole dataset, for checking that a full download
# is actually the full dataset. Partial sizes have no published count, so their
# row counts are measured and recorded rather than checked.
FULL_ROWS = 99_997_497

# How much room to insist on beyond the size of the files themselves. A download
# that fills the disk leaves a truncated file behind and the next run has to be
# told to start over, so it is worth refusing early.
HEADROOM_BYTES = 2 << 30

# Read size for downloading and for hashing. Large enough that the syscall
# overhead disappears, small enough that a failed download does not lose much.
CHUNK_BYTES = 1 << 22

# The bucket sits behind Cloudflare and Cloudflare answers 403 to urllib's default
# `Python-urllib/3.13`. Nothing about the file is restricted, curl fetches it
# without any header at all, it is the user agent alone that is refused. So this
# says who we are, which is what the header is for, and the 403 goes away.
USER_AGENT = "firepanda-bench (+https://github.com/tamnd/firepanda-bench)"


def partition_url(index: int) -> str:
    """Returns the URL of one partition.

    Args:
        index: Which partition, from zero.

    Returns:
        The URL.
    """
    return f"{BASE_URL}/hits_{index}.parquet"


def remote_size(url: str) -> int:
    """Asks the server how large a file is, without downloading it.

    This runs for every partition before the first byte of any of them is pulled,
    which is what makes the free space check worth having. A hundred HEAD requests
    cost a couple of seconds and refusing after eighty files have landed costs a
    lot more than that.

    Args:
        url: The file URL.

    Returns:
        The size in bytes.

    Raises:
        SystemExit: If the server cannot be reached or does not say.
    """
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            length = response.headers.get("Content-Length")
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {url}: {exc}") from exc
    if not length:
        raise SystemExit(f"{url} did not report a size, so the disk check cannot run")
    return int(length)


def download(url: str, path: Path, expected: int) -> None:
    """Fetches one file, resuming a partial download if there is one.

    A hundred files over a slow link will be interrupted at least once, so the
    bytes land in a `.part` file and a restart asks the server to continue from
    where that file ends. A server that will not do ranges is handled by starting
    again, which is slower and is not wrong.

    Args:
        url: The file URL.
        path: Where the finished file goes.
        expected: How many bytes the server said there are.

    Raises:
        SystemExit: If the download fails or ends at the wrong length.
    """
    part = path.with_suffix(path.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > expected:
        # A leftover from an earlier run against a different file. Starting over
        # is the only safe reading of this.
        part.unlink()
        have = 0

    if have == expected:
        part.replace(path)
        return

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    mode = "wb"
    if have:
        request.add_header("Range", f"bytes={have}-")
        mode = "ab"

    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            if have and response.status != 206:
                # The server ignored the range and is sending the whole file.
                have = 0
                mode = "wb"
            with open(part, mode) as handle:
                while True:
                    block = response.read(CHUNK_BYTES)
                    if not block:
                        break
                    handle.write(block)
    except urllib.error.URLError as exc:
        raise SystemExit(f"downloading {url} failed: {exc}") from exc

    landed = part.stat().st_size
    if landed != expected:
        raise SystemExit(
            f"{url} was {expected} bytes and {landed} arrived. The partial file is "
            f"at {part} and another run will resume from it."
        )
    part.replace(path)


def digest(path: Path) -> str:
    """Returns the SHA-256 of a file.

    Args:
        path: The file.

    Returns:
        The digest as hex.
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            hasher.update(block)
    return hasher.hexdigest()


def describe(path: Path) -> dict:
    """Reads what the manifest records about one partition.

    The row count comes out of the Parquet footer rather than out of a read, so
    this costs a seek rather than a pass over the file.

    Args:
        path: The partition.

    Returns:
        The manifest entry, without the digest.

    Raises:
        SystemExit: If the file is not readable as Parquet, which is what a
            truncated download looks like.
    """
    import pyarrow.parquet as pq

    try:
        metadata = pq.ParquetFile(path).metadata
    except Exception as exc:
        raise SystemExit(
            f"{path} is not readable as Parquet, which usually means the download "
            f"was truncated. Delete it and run again: {exc}"
        ) from exc
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "rows": metadata.num_rows,
        "columns": metadata.num_columns,
    }


def partitions(pattern: str) -> list[str]:
    """Expands a partition pattern into the files it names, in partition order.

    Sorted numerically rather than lexically, because `sorted` puts `hits_10`
    before `hits_2` and at the hundred partition size that silently reorders the
    rows. No query in the suite has an answer that depends on row order, so the
    wrong order would not show up as a wrong answer; it would show up as two
    engines disagreeing on a query where both of them are right.

    Args:
        pattern: A glob, as `table_paths` writes it.

    Returns:
        The partition paths, ordered by partition number.

    Raises:
        SystemExit: If the pattern matches nothing, which means the dataset was
            never downloaded.
    """
    import re

    directory = Path(pattern).parent
    found = list(directory.glob(Path(pattern).name))
    if not found:
        raise SystemExit(
            f"no partitions at {pattern}. Run: python tools/data.py --suite clickbench"
        )

    def number(path: Path) -> int:
        """Reads the partition number out of a file name.

        Args:
            path: The partition.

        Returns:
            The number, or -1 for a name with no number in it.
        """
        match = re.search(r"_(\d+)\.parquet$", path.name)
        return int(match.group(1)) if match else -1

    return [str(path) for path in sorted(found, key=number)]


def published_type(field):
    """Returns the type the published schema gives one column of the file.

    The three conversions, in one place, so that the function which converts the
    data and the function which says what the data should look like cannot drift
    apart. Binary becomes text, the day count becomes a date and the second counts
    become microsecond timestamps. Microseconds rather than seconds because that is
    what DuckDB's TIMESTAMP is, and an engine answering in seconds and one
    answering in microseconds would be made to look like a disagreement about the
    answer.

    Args:
        field: The Arrow field, as the file declares it.

    Returns:
        The type the column has once the loader has finished, which for most of the
        105 is the type it already had.
    """
    import pyarrow as pa

    if pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type):
        return pa.string()
    if field.name == DATE_COLUMN:
        return pa.date32()
    if field.name in TIMESTAMP_COLUMNS:
        return pa.timestamp("us")
    return field.type


def published_schema(path) -> dict:
    """Reads a partition's footer and returns the schema the queries expect.

    Out of the footer rather than out of a read, because a schema is the one thing
    a Parquet file will tell you without decoding a row, and this is asked on the
    hundred million row size as readily as on the small one.

    It is here rather than written down as a table of 105 names and types because
    a transcription of a schema is a second copy of it, and the copy is what goes
    stale. This is derived from the file every time it is asked.

    Args:
        path: The partition.

    Returns:
        Column name to the Arrow type it should end up at, in file order.
    """
    import pyarrow.parquet as pq

    return {field.name: published_type(field) for field in pq.read_schema(path)}


def retype(table):
    """Puts a partition into the types the published schema says it has.

    The file is honest about what it stores and the schema is what the queries were
    written against, so somebody has to reconcile the two. ClickBench does it in its
    loader; this does it here, once, so that pandas, Polars and DuckDB all start
    from the same values rather than from three readings of the same file.

    What each column converts to is `published_type`, which is also what the schema
    test checks the engines against, so there is one statement of the rules and not
    two.

    Args:
        table: The partition as Arrow, straight out of the Parquet reader.

    Returns:
        The same rows under the published schema.
    """
    import pyarrow as pa

    original = table.schema
    for index, field in enumerate(original):
        wanted = published_type(field)
        if wanted == field.type:
            continue
        column = table.column(field.name)
        if field.name == DATE_COLUMN:
            # Through int32 because Arrow will not cast uint16 straight to a date32
            # and a date32 is an int32 count of days, which is what the column
            # already is.
            converted = column.cast(pa.int32()).cast(wanted)
        elif field.name in TIMESTAMP_COLUMNS:
            converted = column.cast(pa.timestamp("s")).cast(wanted)
        else:
            converted = column.cast(wanted)
        table = table.set_column(index, pa.field(field.name, converted.type), converted)
    return table


def check_text(kinds: dict, engine: str) -> None:
    """Refuses a load that did not end up with text in the text columns.

    Called by every engine's ClickBench loader with one entry per column it
    loaded, saying whether that engine is holding the column as text. It is the
    cheapest test in this repository and it stands where the failure it catches is
    otherwise invisible.

    Judged against the columns that are actually there rather than against all 28,
    so the eight row fixture the port tests run on, which carries 25 of the 105
    columns, is checked by the same function the real table is.

    Args:
        kinds: Column name to whether the engine holds it as text.
        engine: Which engine is asking, so the message says whose load is wrong.

    Raises:
        SystemExit: If any text column arrived as something else.
    """
    wrong = [name for name in TEXT_COLUMNS if name in kinds and not kinds[name]]
    if not wrong:
        return
    raise SystemExit(
        f"{engine} loaded the hits table with {len(wrong)} of the text columns "
        f"still unconverted: {', '.join(wrong[:5])}. The file stores them as "
        "BYTE_ARRAY with no logical type, ClickBench's own loader converts them, "
        "and an engine that skips it answers nine of the 43 with bytes that the "
        "cross engine fingerprint cannot tell from text."
    )


def null_counts(path: Path) -> dict:
    """Returns how many nulls each column of a partition holds.

    Out of the Parquet footer's per column statistics rather than out of a read,
    so this costs the same seek `describe` already pays and not a pass over twelve
    gigabytes.

    It is recorded because of the third trap in `suites/clickbench/README.md`. In
    this dataset the empty string is what a missing value looks like and there are
    no nulls at all, and eleven queries filter on `<> ''`. A reader that started
    converting empty strings to nulls would change what those filters mean under
    three valued logic, and the first place it would show up is here, as a column
    that used to have no nulls and now has millions.

    Args:
        path: The partition.

    Returns:
        The count per column name, for every column the footer carries statistics
        for. A column whose statistics do not record a null count is left out
        rather than reported as zero.
    """
    import pyarrow.parquet as pq

    metadata = pq.ParquetFile(path).metadata
    counts: dict[str, int] = {}
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        for position in range(group.num_columns):
            column = group.column(position)
            statistics = column.statistics
            if statistics is None or not statistics.has_null_count:
                continue
            counts[column.path_in_schema] = (
                counts.get(column.path_in_schema, 0) + statistics.null_count
            )
    return counts


def check_room(needed: int, out: Path) -> None:
    """Refuses before the first byte if the disk cannot hold the files.

    Args:
        needed: How many bytes the files come to.
        out: The directory they go in.

    Raises:
        SystemExit: If there is not enough room.
    """
    out.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(out).free
    if free < needed + HEADROOM_BYTES:
        raise SystemExit(
            f"{out} has {free / 1e9:.1f} GB free and this needs "
            f"{(needed + HEADROOM_BYTES) / 1e9:.1f} GB, which is the files plus "
            f"{HEADROOM_BYTES / 1e9:.0f} GB of room to work in."
        )


def verify(files: dict, recorded: dict) -> bool:
    """Checks that the files on disk are the ones the manifest describes.

    A truncated download that is never noticed is a wrong answer rather than an
    error, which is the whole reason the digests are recorded.

    Args:
        files: What is on disk now, keyed by partition name.
        recorded: What the manifest says.

    Returns:
        Whether everything matches.
    """
    for name, entry in recorded.items():
        path = Path(entry["path"])
        if not path.exists():
            print(f"  {name} is missing")
            return False
        if path.stat().st_size != entry["bytes"]:
            print(f"  {name} is {path.stat().st_size} bytes and should be {entry['bytes']}")
            return False
        if name in files and files[name] != entry.get("sha256"):
            print(f"  {name} does not match the recorded digest")
            return False
    return True


def build(size: str, root: Path, force: bool, skip_digest: bool = False) -> Path:
    """Downloads the partitions a size needs and writes a manifest beside them.

    Args:
        size: The size name.
        root: The data root.
        force: Whether to fetch files that are already there.
        skip_digest: Whether to trust the byte counts instead of rehashing. The
            full size is twelve gigabytes and hashing it costs half a minute, so
            a scheduled run that has already checked once can skip it.

    Returns:
        The manifest path.

    Raises:
        SystemExit: If the size is unknown or the download cannot be completed.
    """
    if size not in SIZES:
        raise SystemExit(f"unknown ClickBench size '{size}'. Known: {', '.join(SIZES)}")

    count = SIZES[size]
    out = root / "clickbench" / size
    manifest_path = out / "manifest.json"

    if manifest_path.exists() and not force:
        recorded = json.loads(manifest_path.read_text())
        print(f"{manifest_path} exists, checking it before reusing it")
        present = {}
        if not skip_digest:
            for name, entry in recorded["files"].items():
                path = Path(entry["path"])
                if path.exists():
                    present[name] = digest(path)
        if verify(present, recorded["files"]):
            print("  every file is the one recorded. Pass --force to fetch again.")
            return manifest_path
        print("  the cache does not match the manifest, fetching what is missing")

    sizes = {}
    print(f"asking for the size of {count} partition{'s' if count > 1 else ''}")
    for index in range(count):
        sizes[f"hits_{index}"] = remote_size(partition_url(index))
    total = sum(sizes.values())
    print(f"  {total / 1e9:.2f} GB")
    check_room(total, out)

    files = {}
    started = time.perf_counter()
    fetched = 0
    for index in range(count):
        name = f"hits_{index}"
        path = out / f"{name}.parquet"
        if path.exists() and path.stat().st_size == sizes[name] and not force:
            print(f"  {name} is already here")
        else:
            began = time.perf_counter()
            download(partition_url(index), path, sizes[name])
            took = time.perf_counter() - began
            fetched += sizes[name]
            rate = sizes[name] / took / 1e6 if took else 0
            print(f"  {name} {sizes[name] / 1e6:7.1f} MB in {took:5.1f} s  {rate:6.1f} MB/s")
        entry = describe(path)
        entry["sha256"] = digest(path)
        files[name] = entry

    # Summed across the partitions rather than kept per file, because 105 columns
    # times a hundred partitions is ten thousand entries of a number that is the
    # same everywhere, and what anybody would ever ask this manifest is whether the
    # dataset has nulls at all.
    nulls: dict[str, int] = {}
    for index in range(count):
        for column, value in null_counts(out / f"hits_{index}.parquet").items():
            nulls[column] = nulls.get(column, 0) + value

    rows = sum(entry["rows"] for entry in files.values())
    if count == SIZES["100M"] and rows != FULL_ROWS:
        raise SystemExit(
            f"the full dataset is {FULL_ROWS:,} rows and {rows:,} arrived, so "
            "something is missing. Run again with --force."
        )

    manifest = {
        "suite": "clickbench",
        "size": size,
        "rows": rows,
        "partitions": count,
        "bytes": sum(entry["bytes"] for entry in files.values()),
        "columns": next(iter(files.values()))["columns"],
        "source": BASE_URL,
        "generator": "none, this dataset is downloaded and cannot be regenerated",
        "is_published_size": size == "100M",
        # One number per column, from the footers. Every one of them is zero on
        # this dataset, which is the point: the empty string is what a missing
        # value looks like here, eleven queries filter on `<> ''`, and a reader
        # that started turning empty strings into nulls would change what those
        # filters mean. This is where that shows up.
        "nulls": nulls,
        "downloaded_s": round(time.perf_counter() - started, 3),
        "downloaded_bytes": fetched,
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {manifest_path}  {rows:,} rows in {count} file(s)")
    return manifest_path
