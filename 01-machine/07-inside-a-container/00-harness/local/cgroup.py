"""
Reading the numbers the kernel actually enforces -- and saying so honestly
when there are none.

WHAT THIS DEMONSTRATES
  The single habit this whole topic is trying to build: never ask the
  runtime how big the machine is, ask the cgroup. Every function here has
  the same shape -- return the enforced number, or None, and never guess.

  Nothing in the Python standard library reads cpu.max. That is not an
  oversight you can work around with a better API; there is no better API.
  This file is the twenty lines everyone ends up writing.

WHAT TO LOOK FOR IN THE OUTPUT
  On macOS every cgroup function here returns None, because Darwin has no
  cgroupfs at all. That is the correct answer, not a failure -- and it is
  why the container experiments in this topic must run inside a Linux
  container rather than on the host.

RUN
    python3 cgroup.py
"""
from __future__ import annotations

import os
import platform
import sys

CGROUP_V2_ROOT = "/sys/fs/cgroup"


def _read(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def have_cgroup_v2() -> bool:
    """cgroup v2 puts cpu.max at the root of a unified hierarchy."""
    return os.path.exists(f"{CGROUP_V2_ROOT}/cpu.max")


def cpu_quota() -> float | None:
    """CPUs of *bandwidth*, i.e. what `--cpus=` bought you. None = no ceiling.

    v2: cpu.max is "$QUOTA $PERIOD" or "max $PERIOD".
    v1: two files, and a quota of -1 means unlimited.
    """
    raw = _read(f"{CGROUP_V2_ROOT}/cpu.max")
    if raw:
        quota, _, period = raw.partition(" ")
        if quota == "max":
            return None
        return int(quota) / int(period or 100000)

    quota_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota_v1 and period_v1 and int(quota_v1) > 0:
        return int(quota_v1) / int(period_v1)
    return None


def cpu_period_us() -> int | None:
    raw = _read(f"{CGROUP_V2_ROOT}/cpu.max")
    if raw:
        _, _, period = raw.partition(" ")
        return int(period) if period else 100000
    period_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    return int(period_v1) if period_v1 else None


def cpu_weight() -> int | None:
    """cpu.weight (v2, 1..10000) or the v1 shares it was translated from.

    Docker's --cpu-shares lands here. It only ever costs you something when
    some other cgroup wants the CPU at the same moment.
    """
    raw = _read(f"{CGROUP_V2_ROOT}/cpu.weight")
    if raw:
        return int(raw)
    shares = _read("/sys/fs/cgroup/cpu/cpu.shares")
    return int(shares) if shares else None


def cpuset_effective() -> str | None:
    """Which physical CPUs we may run on. Narrows, never freezes."""
    return _read(f"{CGROUP_V2_ROOT}/cpuset.cpus.effective") or _read(
        "/sys/fs/cgroup/cpuset/cpuset.cpus"
    )


def cpu_stat() -> dict[str, int] | None:
    """The file to read first, every time. Key: nr_throttled / nr_periods."""
    raw = _read(f"{CGROUP_V2_ROOT}/cpu.stat")
    if not raw:
        return None
    out: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        try:
            out[key] = int(value)
        except ValueError:
            continue
    return out


def throttle_ratio(stat: dict[str, int] | None) -> float | None:
    if not stat or not stat.get("nr_periods"):
        return None
    return stat["nr_throttled"] / stat["nr_periods"]


def memory_limit() -> int | None:
    raw = _read(f"{CGROUP_V2_ROOT}/memory.max")
    if raw and raw != "max":
        return int(raw)
    v1 = _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    # v1 spells "unlimited" as a number near 2^63, not as a word.
    if v1 and int(v1) < 2**62:
        return int(v1)
    return None


def memory_high() -> int | None:
    raw = _read(f"{CGROUP_V2_ROOT}/memory.high")
    return int(raw) if raw and raw != "max" else None


def memory_events() -> dict[str, int] | None:
    raw = _read(f"{CGROUP_V2_ROOT}/memory.events")
    if not raw:
        return None
    out: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        try:
            out[key] = int(value)
        except ValueError:
            continue
    return out


def pressure(resource: str = "cpu") -> str | None:
    """PSI. Catches throttling AND plain host contention, which cpu.stat does not."""
    return _read(f"{CGROUP_V2_ROOT}/{resource}.pressure")


def host_memory_bytes() -> int | None:
    """What /proc/meminfo would tell a container: the HOST's memory."""
    if sys.platform == "darwin":
        try:
            import subprocess

            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]))
        except Exception:
            return None
    raw = _read("/proc/meminfo")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    return None


def runtime_cpu_answers() -> dict[str, int | None]:
    """Every answer Python can give to "how many CPUs", none of them enforced."""
    answers: dict[str, int | None] = {"os.cpu_count()": os.cpu_count()}
    if hasattr(os, "process_cpu_count"):  # 3.13+
        answers["os.process_cpu_count()"] = os.process_cpu_count()
    if hasattr(os, "sched_getaffinity"):  # Linux only
        answers["len(sched_getaffinity(0))"] = len(os.sched_getaffinity(0))
    return answers


def environment_banner() -> str:
    """Print this before any measurement, so nobody misreads the context."""
    if have_cgroup_v2():
        quota = cpu_quota()
        return (
            f"  cgroup v2 present. cpu.max -> "
            + (f"{quota:.2f} CPU" if quota else "no ceiling")
            + f", cpu.weight -> {cpu_weight()}, cpuset -> {cpuset_effective()}"
        )
    return (
        f"  no cgroupfs on {platform.system()} {platform.machine()}. "
        "Every cgroup reading below is None, correctly.\n"
        "  Darwin has no cgroups at all -- the container experiments in this\n"
        "  topic must run inside a Linux container, not on this host."
    )


if __name__ == "__main__":
    print("cgroup / runtime view of this machine")
    print(environment_banner())
    print()
    for name, value in runtime_cpu_answers().items():
        print(f"  {name:<28} {value}")
    print(f"  {'cpu_quota() [enforced]':<28} {cpu_quota()}")
    print(f"  {'cpu_period_us()':<28} {cpu_period_us()}")
    print(f"  {'cpu_stat()':<28} {cpu_stat()}")
    print(f"  {'memory_limit()':<28} {memory_limit()}")
    mem = host_memory_bytes()
    print(f"  {'host memory':<28} {mem and f'{mem / 2**30:.1f} GiB'}")
