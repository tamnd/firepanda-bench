#!/usr/bin/env python3
"""Records the machine and the toolchain, so a result file can be argued with.

A benchmark number that does not come with the machine it ran on is a rumour.
This writes down everything that would change the number and nothing that would
not: the CPU and its cores, whether the turbo and the frequency governor were
left where a laptop puts them, how much memory there was and how much of it was
free, the versions of every engine, and the commit of the firepanda checkout that
was measured.

Two of those are worth calling out because they are the usual reason two people
get different numbers on the same hardware. A frequency governor set to
`powersave` costs a third of the throughput on some parts, and transparent huge
pages being on or off moves a hash join by more than most optimizations do. Both
are recorded rather than changed, because a benchmark that tunes the machine
under itself is measuring the tuning.

Usage:
    python tools/env_report.py --out results/env.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics

import engines as engine_registry

ROOT = Path(__file__).resolve().parent.parent


def read_first_line(path: str) -> str:
    """Reads one line out of a sysfs or proc file.

    Args:
        path: The file.

    Returns:
        The line stripped, or empty if it is not readable.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def kernel_settings() -> dict:
    """Reads the kernel settings that move a benchmark.

    Returns:
        A mapping of setting name to value, with empty strings where the setting
        does not exist on this machine.
    """
    governor = read_first_line("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    huge_pages = read_first_line("/sys/kernel/mm/transparent_hugepage/enabled")
    turbo = read_first_line("/sys/devices/system/cpu/intel_pstate/no_turbo")
    swappiness = read_first_line("/proc/sys/vm/swappiness")
    return {
        "scaling_governor": governor,
        "transparent_hugepage": huge_pages,
        # The file holds 1 when turbo is disabled, which reads backwards, so it
        # is recorded under a name that says what it means.
        "turbo_enabled": {"0": True, "1": False}.get(turbo),
        "swappiness": swappiness,
    }


def memory_state() -> dict:
    """Reads how much memory was free when the run started.

    A run that starts with the page cache full of the dataset and one that starts
    cold are different measurements, and this is what tells them apart after the
    fact.

    Returns:
        A mapping of the interesting `/proc/meminfo` fields, in bytes.
    """
    wanted = {
        "MemTotal": "total_bytes",
        "MemAvailable": "available_bytes",
        "Cached": "cached_bytes",
    }
    found: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key = line.split(":", 1)[0]
                if key in wanted:
                    found[wanted[key]] = int(line.split()[1]) * 1024
    except OSError:
        pass
    return found


def mojo_version() -> str:
    """Returns the Mojo toolchain version from the firepanda checkout.

    Returns:
        The version line, or empty if Mojo is not installed.
    """
    try:
        import engines.firepanda_engine as firepanda_engine

        home = firepanda_engine.firepanda_home()
    except (Exception, SystemExit):
        return ""
    try:
        completed = subprocess.run(
            [
                "pixi",
                "run",
                "--manifest-path",
                str(home / "pixi.toml"),
                "mojo",
                "--version",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""


def engine_versions() -> dict[str, str]:
    """Asks every known engine for its version.

    Returns:
        A mapping from engine name to version, empty where the engine is not
        installed on this machine.
    """
    found = {}
    for name in engine_registry.KNOWN:
        try:
            found[name] = engine_registry.load_engine(name).version()
        # SystemExit and not Exception, because an engine reports a missing
        # install by exiting, which reads fine from a command line and is fatal
        # here. Describing a machine is allowed to come back with a blank.
        except (Exception, SystemExit):
            found[name] = ""
    return found


def describe() -> dict:
    """Builds the whole environment record.

    Returns:
        The record.
    """
    record = {
        "host": platform.node(),
        "machine": metrics.machine_facts(),
        "kernel": kernel_settings(),
        "memory": memory_state(),
        "engines": engine_versions(),
        "mojo_version": mojo_version(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else [],
    }
    try:
        import engines.firepanda_engine as firepanda_engine

        record["firepanda_ref"] = firepanda_engine.git_ref()
    except (Exception, SystemExit):
        record["firepanda_ref"] = ""
    return record


def main(argv: list[str] | None = None) -> int:
    """Writes the environment record.

    Args:
        argv: The arguments, or None for `sys.argv`.

    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "env.json")
    args = parser.parse_args(argv)

    record = describe()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
