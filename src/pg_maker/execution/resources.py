"""Small Linux resource preflight, adapted from pg_workload's resource checks."""

import os
import shutil
from pathlib import Path

from pg_maker.errors import PreconditionError


def check_resources(directory: Path, limits: dict, *, jobs: int = 1, extra_bytes: int = 0) -> dict:
    disk = shutil.disk_usage(directory).free
    needed = limits["min_free_disk_bytes"] + extra_bytes
    if disk < needed:
        raise PreconditionError(
            "insufficient free disk space",
            details={"available_bytes": disk, "required_bytes": needed},
        )
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.split()[0]) * 1024
    available = values["MemAvailable"]
    for base in (Path("/sys/fs/cgroup"),):
        maximum, current = base / "memory.max", base / "memory.current"
        if maximum.is_file() and current.is_file():
            limit = maximum.read_text().strip()
            if limit != "max":
                available = min(available, max(0, int(limit) - int(current.read_text())))
    # The container cap is a maximum, not an advance reservation of all its memory.
    minimum = min(limits["memory_bytes"], 256 * 1024 * 1024)
    if available < minimum:
        raise PreconditionError(
            "insufficient available memory",
            details={"available_bytes": available, "required_bytes": minimum},
        )
    cpus = len(os.sched_getaffinity(0))
    if jobs > cpus:
        raise PreconditionError(
            "build jobs exceed available CPUs", details={"requested": jobs, "available": cpus}
        )
    return {"free_disk_bytes": disk, "available_memory_bytes": available, "available_cpus": cpus}
