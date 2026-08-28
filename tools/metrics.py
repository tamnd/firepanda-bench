#!/usr/bin/env python3
"""What a measurement is, and everything we record about one.

A benchmark that reports a single wall clock number is not enough to make a claim
with. The claim this project wants to make is ten times faster and ten times
leaner, and the second half of that is invisible to a stopwatch. So a measurement
here carries wall clock, user and system CPU separately, peak resident memory,
resident memory sampled through the run so a spike between the start and the end
is not missed, page faults split into the kind that cost a memory access and the
kind that cost a disk read, context switches split into the kind a thread asked
for and the kind it was given, block input and output, and the number of threads
the process had running at its widest.

Most of that comes from `getrusage`, which the kernel maintains for free. The
resident sampler is the one thing that costs something, and it costs a read of
one small file every few milliseconds on a thread that is asleep the rest of the
time.

The measurement runs inside the worker process rather than around it, because
`getrusage` on a child only reports totals after the child has exited and a peak
that includes the interpreter starting up and the dataset loading is not the peak
of the query. The worker measures itself, and the parent measures the worker.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field

# `ru_maxrss` is kilobytes on Linux and bytes on macOS. Getting this wrong makes a
# memory comparison wrong by a factor of a thousand in a direction that flatters
# whichever platform the author happened to use.
_RSS_UNIT = 1 if sys.platform == "darwin" else 1024

# How often the resident set sampler wakes up. Five milliseconds is short enough to
# catch the copy at the end of a group by and long enough that the sampler itself
# does not show up in the CPU numbers.
SAMPLE_INTERVAL_S = 0.005


@dataclass
class Sample:
    """One run of one query, measured from inside the process that ran it."""

    wall_s: float
    """Wall clock, from a monotonic counter."""

    cpu_user_s: float
    """CPU spent in user code. Divided by wall clock this is the parallelism."""

    cpu_sys_s: float
    """CPU spent in the kernel. Large means allocation or page faults, not work."""

    peak_rss_bytes: int
    """The high water mark of resident memory for the whole process."""

    rss_delta_bytes: int
    """Resident memory the query added and did not give back."""

    rss_peak_during_bytes: int
    """The largest resident set the sampler saw while this run was in flight."""

    minor_faults: int
    """Page faults served from memory. The cost of touching a fresh allocation."""

    major_faults: int
    """Page faults that went to disk. On a warm run these should be zero."""

    voluntary_switches: int
    """Context switches the process asked for, by waiting on something."""

    involuntary_switches: int
    """Context switches the scheduler imposed. Oversubscription shows up here."""

    block_reads: int
    """Block input operations the kernel counted."""

    block_writes: int
    """Block output operations the kernel counted."""

    threads_peak: int
    """The most threads the process had at once, sampled alongside the memory."""

    cold: bool
    """Whether this was the first run, before anything was warm.

    For the ingestion suite it means more than that: the file is evicted from the
    page cache before this run, so it is cold in the sense that matters for a
    reader. `block_reads` on this sample is what says whether the eviction took.
    """


@dataclass
class Measurement:
    """Every run of one query on one engine, plus what it produced."""

    engine: str
    query: str
    ok: bool
    samples: list[Sample] = field(default_factory=list)
    load_s: float = 0.0
    """Time to get the dataset into the engine. Not part of any query timing."""

    rows_out: int = 0
    cols_out: int = 0
    checksum: str = ""
    """A digest of the answer, for putting in a table. Not what agreement is decided on."""

    sums: dict[str, float] = field(default_factory=dict)
    """The sum of every numeric column of the answer, by column name.

    The digest above is a hash, and a hash has no tolerance. That is fine when
    every engine carries a query in exact decimal and wrong the moment one of them
    cannot: Arrow-backed pandas has to do TPC-H money in float64, and a float sum
    of six million line items lands a few parts in a billion away from the exact
    one. Rounded to nine significant figures that difference straddles a boundary
    perhaps one query in five, and the run then reports a disagreement on four
    queries that all three engines had just matched against the specification's
    published answer.

    So the numbers travel next to the hash and agreement is decided on them, with
    a relative tolerance. The hash is still what a report prints, because a reader
    comparing two runs wants one short string rather than twelve long ones.
    """

    hashes: dict[str, int] = field(default_factory=dict)
    """An order independent digest of every text column of the answer, by name.

    Text gets its own field because it needs the opposite treatment. A sum can be
    compared with a tolerance; a hash near two to the fifty three cannot, since a
    relative tolerance that admits float noise would admit almost any hash. These
    are compared exactly, and they are the only thing standing between the harness
    and two engines that grouped by different string keys, arrived at the same
    number of groups and the same totals, and were recorded as agreeing.
    """

    note: str = ""
    """Why this is missing, slow or surprising. Read by the report."""

    def summary(self) -> dict:
        """Reduces the samples to the numbers a table shows.

        Returns:
            A mapping with the median and spread of every recorded quantity.
        """
        if not self.samples:
            return {
                "median_s": None,
                "iqr_s": None,
                "peak_rss_bytes": None,
                "cache": "none",
            }
        walls = sorted(s.wall_s for s in self.samples)
        warm = [s for s in self.samples if not s.cold] or self.samples
        warm_walls = sorted(s.wall_s for s in warm)
        return {
            "median_s": statistics.median(warm_walls),
            "iqr_s": _iqr(warm_walls),
            "p90_s": _percentile(warm_walls, 90),
            "p99_s": _percentile(warm_walls, 99),
            "min_s": warm_walls[0],
            "max_s": warm_walls[-1],
            "cold_s": self.samples[0].wall_s,
            "all_runs_s": walls,
            "cpu_user_s": statistics.median(s.cpu_user_s for s in warm),
            "cpu_sys_s": statistics.median(s.cpu_sys_s for s in warm),
            "parallelism": _parallelism(warm),
            "peak_rss_bytes": max(s.peak_rss_bytes for s in self.samples),
            "rss_peak_during_bytes": max(s.rss_peak_during_bytes for s in self.samples),
            "rss_delta_bytes": statistics.median(s.rss_delta_bytes for s in warm),
            "minor_faults": statistics.median(s.minor_faults for s in warm),
            "major_faults": statistics.median(s.major_faults for s in warm),
            "voluntary_switches": statistics.median(s.voluntary_switches for s in warm),
            "involuntary_switches": statistics.median(s.involuntary_switches for s in warm),
            "block_reads": sum(s.block_reads for s in self.samples),
            "block_writes": sum(s.block_writes for s in self.samples),
            "threads_peak": max(s.threads_peak for s in self.samples),
            "cache": "warm",
            "runs": len(self.samples),
        }


def _iqr(values: list[float]) -> float:
    """Returns the interquartile range of an already sorted list.

    A single number with no spread is not a measurement, and this is the spread we
    publish.

    Args:
        values: The sorted samples.

    Returns:
        The distance between the third and first quartiles. Zero for one sample.
    """
    if len(values) < 4:
        return values[-1] - values[0] if values else 0.0
    quantiles = statistics.quantiles(values, n=4, method="inclusive")
    return quantiles[2] - quantiles[0]


def _percentile(values: list[float], rank: int) -> float:
    """Returns a percentile of an already sorted list by nearest rank.

    Ten runs cannot resolve a ninety ninth percentile and this does not pretend
    otherwise: at ten samples `p99_s` is the slowest run, and it is reported under
    that name anyway so a reader comparing engines has the tail in the same column
    rather than having to reconstruct it from `all_runs_s`. The interpolating
    definition would be worse here, because it would invent a value between two
    runs and give the number an air of precision it has not got. Raising `--runs`
    is what makes these two mean what they say.

    Args:
        values: The sorted samples.
        rank: The percentile wanted, from 0 to 100.

    Returns:
        The sample at that rank.
    """
    if not values:
        return 0.0
    index = math.ceil(rank / 100 * len(values)) - 1
    return values[max(0, min(index, len(values) - 1))]


def _parallelism(samples: list[Sample]) -> float:
    """Returns CPU seconds per wall second, which is how many cores were busy.

    An engine that is ten times faster because it used sixteen cores and one that
    is ten times faster on one core are both ten times faster, and they are not
    the same result. This is the column that tells them apart.

    Args:
        samples: The runs to average over.

    Returns:
        Total CPU divided by total wall clock.
    """
    wall = sum(s.wall_s for s in samples)
    if wall <= 0:
        return 0.0
    cpu = sum(s.cpu_user_s + s.cpu_sys_s for s in samples)
    return cpu / wall


class ResidentSampler:
    """Watches resident memory and thread count on a background thread.

    `getrusage` gives a peak for the whole process since it started, which cannot
    say whether the peak belonged to loading the data or to running the query.
    This can, by sampling and by being started and stopped around the part that
    matters.

    On a system with no `/proc` the sampler falls back to the `getrusage` peak,
    which is honest but coarse, and the result file records which one it used.
    """

    def __init__(self, interval_s: float = SAMPLE_INTERVAL_S):
        """Constructs a stopped sampler.

        Args:
            interval_s: How long to sleep between samples.
        """
        self.interval_s = interval_s
        self.peak_rss = 0
        self.peak_threads = 0
        self.available = os.path.exists("/proc/self/status")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _read(self) -> tuple[int, int]:
        """Reads the current resident set and thread count.

        Returns:
            Resident bytes and thread count, both zero if unavailable.
        """
        try:
            with open("/proc/self/status", "rb") as handle:
                rss = 0
                threads = 0
                for line in handle:
                    if line.startswith(b"VmRSS:"):
                        rss = int(line.split()[1]) * 1024
                    elif line.startswith(b"Threads:"):
                        threads = int(line.split()[1])
                return rss, threads
        except OSError:
            return 0, 0

    def _loop(self) -> None:
        """Samples until asked to stop."""
        while not self._stop.is_set():
            rss, threads = self._read()
            self.peak_rss = max(self.peak_rss, rss)
            self.peak_threads = max(self.peak_threads, threads)
            self._stop.wait(self.interval_s)

    def __enter__(self) -> ResidentSampler:
        """Starts sampling.

        Returns:
            The sampler.
        """
        self.peak_rss = 0
        self.peak_threads = 0
        if self.available:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Stops sampling and joins the thread."""
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1.0)
            self._thread = None


def evict_page_cache(paths: list[str]) -> bool:
    """Asks the kernel to forget the pages of some files, so a read is really cold.

    A cold cache number and a warm cache one answer different questions, and for a
    file reader both are worth having: warm measures the parser, cold measures the
    parser and the storage together. What makes the distinction hard is that by
    the time a benchmark runs, the file it is about to read has just been written
    or just been read, and is entirely in the page cache. Calling the first run
    cold does not make it cold.

    `POSIX_FADV_DONTNEED` drops a file's clean pages without any privilege, which
    is the part that matters: dropping the whole cache needs root, and a benchmark
    that has to be run as root is a benchmark nobody runs. The pages have to be
    clean first, so the file is flushed before it is dropped.

    This does not claim to have succeeded. The kernel is entitled to ignore the
    hint, another process may be holding the same pages, and macOS has no
    equivalent call at all. The evidence that a run really was cold is in the
    measurement rather than here: a cold run that actually went to the device has
    a `block_reads` count in the thousands and a warm one has none.

    Args:
        paths: The files to drop.

    Returns:
        Whether the call was available and raised nothing for every file.
    """
    if not hasattr(os, "posix_fadvise"):
        return False
    dropped = True
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            dropped = False
            continue
        try:
            # A read only descriptor may refuse the flush on some filesystems, and
            # a file this harness generated and has not touched since is clean
            # anyway, so a refusal is not a reason to skip the eviction.
            with contextlib.suppress(OSError):
                os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            dropped = False
        finally:
            os.close(fd)
    return dropped


def measure(
    run, runs: int, cold_first: bool = True, before_cold=None
) -> tuple[list[Sample], object]:
    """Runs a callable several times and records everything about each run.

    The callable must consume its own result. A lazy engine that is never asked
    for an answer has not done any work, and every engine adapter here ends its
    query by forcing materialization for exactly that reason.

    Args:
        run: A callable taking no arguments and returning the query's answer.
        runs: How many times to run it.
        cold_first: Whether to mark the first run as the cold one.
        before_cold: A callable run once before the first run and outside its
            timing, for a suite that has to arrange for the first run to be cold.

    Returns:
        The samples and the answer from the last run.
    """
    samples: list[Sample] = []
    answer = None
    for index in range(runs):
        if index == 0 and before_cold is not None:
            before_cold()
        # The previous run's answer is released here rather than by the
        # assignment below, which happens inside the timed region. On a suite
        # whose answers are gigabytes, freeing the last one was being charged to
        # this one, and it is charged to every engine, so it does not favour any
        # of them and it does compress the differences between them.
        answer = None
        before = resource.getrusage(resource.RUSAGE_SELF)
        sampler = ResidentSampler()
        with sampler:
            started = time.perf_counter()
            answer = run()
            wall = time.perf_counter() - started
        after = resource.getrusage(resource.RUSAGE_SELF)
        peak = after.ru_maxrss * _RSS_UNIT
        samples.append(
            Sample(
                wall_s=wall,
                cpu_user_s=after.ru_utime - before.ru_utime,
                cpu_sys_s=after.ru_stime - before.ru_stime,
                peak_rss_bytes=peak,
                rss_delta_bytes=(after.ru_maxrss - before.ru_maxrss) * _RSS_UNIT,
                rss_peak_during_bytes=sampler.peak_rss or peak,
                minor_faults=after.ru_minflt - before.ru_minflt,
                major_faults=after.ru_majflt - before.ru_majflt,
                voluntary_switches=after.ru_nvcsw - before.ru_nvcsw,
                involuntary_switches=after.ru_nivcsw - before.ru_nivcsw,
                block_reads=after.ru_inblock - before.ru_inblock,
                block_writes=after.ru_oublock - before.ru_oublock,
                threads_peak=sampler.peak_threads,
                cold=cold_first and index == 0,
            )
        )
    return samples, answer


def machine_facts() -> dict:
    """Describes the machine, because timings only compare within one.

    Returns:
        A mapping naming the host, its cores, its memory and its kernel.
    """
    facts = {
        # The host name, so a result file can be named after the machine that
        # produced it. Two machines running the same suite at the same size in the
        # same io mode on the same day collide otherwise.
        "host": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "python": platform.python_version(),
        "logical_cores": os.cpu_count() or 0,
    }
    try:
        facts["physical_cores"] = _physical_cores()
    except Exception:
        facts["physical_cores"] = 0
    try:
        facts["ram_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        facts["ram_bytes"] = 0
    if os.path.exists("/proc/cpuinfo"):
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("model name"):
                    facts["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    return facts


def _physical_cores() -> int:
    """Counts physical cores rather than hardware threads.

    Hyperthreads are not cores and a per core throughput number computed against
    the logical count is off by two on most machines.

    Returns:
        The physical core count, or zero if it cannot be determined.
    """
    if sys.platform == "darwin":
        out = subprocess.run(
            ["sysctl", "-n", "hw.physicalcpu"], capture_output=True, text=True, check=True
        )
        return int(out.stdout.strip())
    if os.path.exists("/proc/cpuinfo"):
        seen = set()
        physical_id = None
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("physical id"):
                    physical_id = line.split(":", 1)[1].strip()
                elif line.startswith("core id"):
                    seen.add((physical_id, line.split(":", 1)[1].strip()))
        if seen:
            return len(seen)
    return 0


def dump(measurement: Measurement) -> str:
    """Serializes a measurement for a worker process to print.

    Args:
        measurement: The measurement.

    Returns:
        One line of JSON.
    """
    return json.dumps(asdict(measurement))


def load(line: str) -> Measurement:
    """Reads back what `dump` wrote.

    Args:
        line: The JSON line.

    Returns:
        The measurement, with its samples as `Sample` objects.

    Raises:
        ValueError: If the line is not the JSON a worker produces.
    """
    doc = json.loads(line)
    samples = [Sample(**s) for s in doc.pop("samples", [])]
    return Measurement(samples=samples, **doc)
