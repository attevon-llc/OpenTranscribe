"""``/metrics/queues`` in multiprocess mode serves only this scrape's queue gauges."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]

_SCRIPT = textwrap.dedent(
    """
    from app.api.endpoints import metrics as page
    from app.core.metrics import celery_queue_depth, http_requests_total

    http_requests_total.labels(method="GET", route="/x", status="200").inc()
    page.update_queue_depths = lambda: celery_queue_depth.labels(queue="cpu").set(7)
    print(page.queue_metrics().body.decode())
    """
)


def test_queue_page_has_only_the_queue_gauges_with_this_scrapes_values(tmp_path):
    env = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        "PYTHONPATH": str(_BACKEND),
    }
    done = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        env=env,
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert 'celery_queue_depth{queue="cpu"} 7.0' in done.stdout
    assert "http_requests_total" not in done.stdout
