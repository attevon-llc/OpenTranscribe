"""Per-sample, per-file and per-batch result records, plus the percentile helper."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VramSample:
    timestamp: float
    used_mb: int
    total_mb: int
    free_mb: int
    utilization_pct: int
    temp_c: int


@dataclass
class CpuSample:
    """One docker-stats sample aggregated across the monitored worker containers."""

    timestamp: float
    cpu_pct: float  # summed CPU% across containers (100% == one core)
    mem_mb: float  # summed RSS across containers


@dataclass
class FileResult:
    uuid: str
    filename: str
    duration_s: float
    task_id: str = ''
    wall_start: float = 0.0
    wall_end: float = 0.0
    wall_elapsed: float = 0.0
    status: str = ''
    stages: dict = field(default_factory=dict)
    vram_profile: dict | None = None


@dataclass
class BatchResult:
    batch_size: int
    wall_start: float = 0.0
    wall_end: float = 0.0
    wall_elapsed: float = 0.0
    file_results: list[FileResult] = field(default_factory=list)
    vram_samples: list[VramSample] = field(default_factory=list)
    vram_peak_mb: int = 0
    vram_avg_mb: float = 0.0
    cpu_samples: list[CpuSample] = field(default_factory=list)
    cpu_pct_peak: float = 0.0
    cpu_pct_avg: float = 0.0
    ram_mb_peak: float = 0.0
    ram_mb_avg: float = 0.0


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in 0..100). Empty -> 0.0."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (pct / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac
