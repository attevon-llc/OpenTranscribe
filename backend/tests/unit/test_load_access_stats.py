"""access_stats.py reports time-to-response-start beside the total the access log records.

A route that queues FastAPI BackgroundTasks logs a total that includes the post-response work, so
the harness keeps the two apart (backend/app/middleware/observability.py writes ``ttfb=``).
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "scripts" / "perf" / "load" / "tools" / "access_stats.py"


def _run(tmp_path: Path, lines: list[str]) -> list[dict[str, str]]:
    with gzip.open(tmp_path / "backend.log.gz", "wt") as fh:
        fh.write("\n".join(lines) + "\n")
    spec = importlib.util.spec_from_file_location("access_stats_under_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    argv = sys.argv
    sys.argv = ["access_stats.py", str(tmp_path)]
    try:
        assert module.main() == 0
    finally:
        sys.argv = argv
    with (tmp_path / "access_stats.csv").open() as fh:
        return list(csv.DictReader(fh))


def test_start_time_is_reported_apart_from_the_total_for_a_background_task_route(tmp_path):
    rows = _run(
        tmp_path,
        [
            "INFO:access:[x] PUT /api/files/{file_uuid}/segments 200 900.0ms user=1 dbq=4 ttfb=40.0ms",
            "INFO:access:[x] PUT /api/files/{file_uuid}/segments 200 800.0ms user=1 dbq=4 ttfb=60.0ms",
            "INFO:access:[x] GET /api/tags 200 5.0ms user=1 dbq=1 ttfb=4.0ms",
            "INFO:access:[x] GET /api/old 200 7.0ms user=1 dbq=1",  # a log from before ttfb
        ],
    )

    by_route = {r["route"]: r for r in rows}
    put = by_route["/api/files/{file_uuid}/segments"]
    assert float(put["server_p95_ms"]) >= 800.0
    assert float(put["start_p95_ms"]) <= 60.0
    assert by_route["/api/old"]["start_p50_ms"] == ""
