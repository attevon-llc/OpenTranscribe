"""``/metrics`` aggregates every API worker in prometheus_client's multiprocess mode.

Scale foundations plan 3.5/3.8. Workers are real subprocesses sharing one
``PROMETHEUS_MULTIPROC_DIR``; the parent reads the directory the way a scrape does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families

from app.core import metrics_multiprocess
from app.core.config import settings

_BACKEND = Path(__file__).resolve().parents[2]

_WORKER = textwrap.dedent(
    """
    import os, sys
    from app.core.metrics import http_requests_in_flight, http_requests_total
    http_requests_total.labels(method="GET", route="/x", status="200").inc(3)
    http_requests_in_flight.inc()
    if sys.argv[1] == "hold":
        print("ready", flush=True)
        sys.stdin.readline()
    if sys.argv[1] == "report":
        from app.core.metrics_multiprocess import render_all
        sys.stdout.write(render_all().decode())
    # _exit skips atexit: the worker dies the way a SIGKILLed one does.
    sys.stdout.flush()
    os._exit(0)
    """
)


def _env(directory: Path) -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("PROMETHEUS_")},
        "PROMETHEUS_MULTIPROC_DIR": str(directory),
        "PYTHONPATH": str(_BACKEND),
    }


def _worker(directory: Path, mode: str = "die") -> str:
    done = subprocess.run(
        [sys.executable, "-c", _WORKER, mode],
        env=_env(directory),
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return done.stdout


def _samples(page: str, name: str) -> dict[tuple[tuple[str, str], ...], float]:
    found = {}
    for family in text_string_to_metric_families(page):
        for sample in family.samples:
            if sample.name == name:
                found[tuple(sorted(sample.labels.items()))] = sample.value
    return found


def _value(page: str, name: str) -> float:
    return sum(_samples(page, name).values())


def test_render_all_sums_counters_and_live_gauges_across_workers(tmp_path, monkeypatch):
    live = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, "hold"],
            env=_env(tmp_path),
            cwd=_BACKEND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for proc in live:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "ready"
        monkeypatch.setattr(settings, "PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        page = metrics_multiprocess.render_all().decode()
    finally:
        for proc in live:
            proc.communicate("\n", timeout=60)

    assert _value(page, "http_requests_total") == 6
    assert _value(page, "http_requests_in_flight") == 2


def test_a_new_worker_sweeps_the_gauges_of_workers_that_died(tmp_path, monkeypatch):
    _worker(tmp_path)
    _worker(tmp_path)
    page = _worker(tmp_path, mode="report")

    # Two dead workers each left in_flight=1. Without the import-time sweep this reads 3.
    assert _value(page, "http_requests_in_flight") == 1
    assert _value(page, "http_requests_total") == 9, "counters keep a dead worker's totals"


def test_without_the_directory_render_all_uses_the_plain_registry(monkeypatch):
    monkeypatch.setattr(settings, "PROMETHEUS_MULTIPROC_DIR", "")
    assert not metrics_multiprocess.enabled()
    assert b"http_requests_in_flight" in metrics_multiprocess.render_all()
