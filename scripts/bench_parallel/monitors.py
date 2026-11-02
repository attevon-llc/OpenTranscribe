"""Background samplers for GPU VRAM (nvidia-smi) and container CPU/RAM (docker stats)."""

from __future__ import annotations

import subprocess
import threading
import time

from bench_parallel.config import GPU_DEVICE_ID, STATS_SAMPLE_INTERVAL, VRAM_SAMPLE_INTERVAL
from bench_parallel.models import CpuSample, VramSample


class VramMonitor:
    """Background thread that samples GPU VRAM via nvidia-smi."""

    def __init__(self, gpu_id: int = GPU_DEVICE_ID):
        self.gpu_id = gpu_id
        self.samples: list[VramSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            sample = self._sample()
            if sample:
                self.samples.append(sample)
            self._stop.wait(VRAM_SAMPLE_INTERVAL)

    def _sample(self) -> VramSample | None:
        try:
            result = subprocess.run(
                [
                    'nvidia-smi',
                    f'--id={self.gpu_id}',
                    '--query-gpu=memory.used,memory.total,memory.free,utilization.gpu,temperature.gpu',
                    '--format=csv,noheader,nounits',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                parts = [p.strip() for p in result.stdout.strip().split(',')]
                return VramSample(
                    timestamp=time.time(),
                    used_mb=int(parts[0]),
                    total_mb=int(parts[1]),
                    free_mb=int(parts[2]),
                    utilization_pct=int(parts[3]),
                    temp_c=int(parts[4]),
                )
        except Exception:
            pass
        return None

    def get_results(self) -> tuple[list[VramSample], int, float]:
        """Returns (samples, peak_used_mb, avg_used_mb)."""
        if not self.samples:
            return [], 0, 0.0
        peak = max(s.used_mb for s in self.samples)
        avg = sum(s.used_mb for s in self.samples) / len(self.samples)
        return self.samples, peak, avg


class DockerStatsMonitor:
    """Background thread sampling CPU% and RAM of the worker containers via `docker stats`.

    Captures the host-side compute cost (CPU-bound preprocess/postprocess, RAM
    pressure) alongside the GPU profile, so the whitepaper can attribute
    bottlenecks. Sampling is a single short-lived `docker stats --no-stream`
    call per interval — negligible overhead, never perturbs the pipeline.
    """

    def __init__(self, containers: list[str]):
        self.containers = [c for c in containers if c]
        self.samples: list[CpuSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if not self.containers:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)

    def _run(self):
        while not self._stop.is_set():
            sample = self._sample()
            if sample:
                self.samples.append(sample)
            self._stop.wait(STATS_SAMPLE_INTERVAL)

    def _sample(self) -> CpuSample | None:
        try:
            result = subprocess.run(
                [
                    'docker',
                    'stats',
                    '--no-stream',
                    '--format',
                    '{{.CPUPerc}}\t{{.MemUsage}}',
                    *self.containers,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            cpu_total = 0.0
            mem_total = 0.0
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('\t')
                if len(parts) < 2:
                    continue
                cpu_total += _parse_cpu_pct(parts[0])
                mem_total += _parse_mem_usage(parts[1])
            return CpuSample(timestamp=time.time(), cpu_pct=cpu_total, mem_mb=mem_total)
        except Exception:
            return None

    def get_results(self) -> tuple[list[CpuSample], float, float, float, float]:
        """Returns (samples, cpu_peak, cpu_avg, ram_peak_mb, ram_avg_mb)."""
        if not self.samples:
            return [], 0.0, 0.0, 0.0, 0.0
        cpu_peak = max(s.cpu_pct for s in self.samples)
        cpu_avg = sum(s.cpu_pct for s in self.samples) / len(self.samples)
        ram_peak = max(s.mem_mb for s in self.samples)
        ram_avg = sum(s.mem_mb for s in self.samples) / len(self.samples)
        return self.samples, cpu_peak, cpu_avg, ram_peak, ram_avg


def _parse_cpu_pct(text: str) -> float:
    """'123.45%' -> 123.45."""
    try:
        return float(text.strip().rstrip('%'))
    except ValueError:
        return 0.0


def _parse_mem_usage(text: str) -> float:
    """'1.5GiB / 62GiB' -> 1536.0 (MB), taking the used side only."""
    used = text.split('/')[0].strip()
    num = ''.join(ch for ch in used if ch.isdigit() or ch == '.')
    unit = used[len(num) :].strip().lower()
    try:
        val = float(num)
    except ValueError:
        return 0.0
    if unit.startswith('gi') or unit.startswith('gb'):
        return val * 1024
    if unit.startswith('ki') or unit.startswith('kb'):
        return val / 1024
    if unit.startswith('b'):
        return val / (1024 * 1024)
    # mib / mb (and bare numbers) default to MB
    return val
