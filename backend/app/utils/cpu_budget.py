"""CPU budget of this process: the container quota, not the host core count.

``os.cpu_count()`` reports every host core even inside a container limited by a
cgroup CPU quota (Kubernetes ``limits.cpu``, ``docker run --cpus``). Sizing threads or
helper pools from it oversubscribes the quota and gets the container CFS-throttled.
"""

import math
import os

CGROUP_CPU_MAX = "/sys/fs/cgroup/cpu.max"  # cgroup v2


def _cgroup_quota_cpus() -> int | None:
    """Return the cgroup v2 CPU quota rounded up, or None when unlimited/unreadable."""
    try:
        with open(CGROUP_CPU_MAX) as f:
            quota, period = f.read().split()[:2]
        if quota == "max":
            return None
        q, p = int(quota), int(period)
        if q <= 0 or p <= 0:
            return None
        return max(1, math.ceil(q / p))
    except (OSError, ValueError):
        return None


def effective_cpu_count() -> int:
    """Min of CPU affinity and cgroup quota, falling back to ``os.cpu_count()``; at least 1."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    quota = _cgroup_quota_cpus()
    if quota is not None:
        cpus = min(cpus, quota)
    return max(1, cpus)


def parse_thread_count_setting(raw: str) -> int | None:
    """Parse a deployment thread-count setting that is ``auto`` or a positive integer.

    Args:
        raw: The raw value. Surrounding whitespace and case are ignored.

    Returns:
        ``None`` for ``auto``, otherwise the positive integer.

    Raises:
        ValueError: For anything else (zero, negative, non-numeric), so a typo fails
            loudly at startup rather than silently meaning ``auto``.
    """
    value = raw.strip().lower()
    if value == "auto":
        return None
    try:
        count = int(value)
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"{raw!r} is not valid: use 'auto' or a positive integer")
    return count
