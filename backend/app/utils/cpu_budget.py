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
