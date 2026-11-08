"""``/metrics`` reports how much of the API's database pool is in use.

A request that needs a connection while every one is checked out waits up to the pool
timeout (30 s) and then fails. Under concurrent load that saturation, not slow SQL, is often
the cause of a latency cliff, and nothing exported it: the load-test runbook
(``scripts/perf/load/README.md``) samples these gauges to tell the two apart.
"""

from __future__ import annotations

import re

from app.core.config import settings
from app.db.base import engine


def _sample(body: str, name: str) -> float:
    match = re.search(rf"^{name} (\S+)$", body, re.MULTILINE)
    assert match is not None, f"{name} is not exported"
    return float(match.group(1))


def test_metrics_report_the_pool_capacity(client):
    body = client.get("/metrics").text

    assert _sample(body, "db_pool_capacity") == settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW


def test_checked_out_connections_follow_the_pool(client):
    before = _sample(client.get("/metrics").text, "db_pool_checked_out")

    with engine.connect(), engine.connect():
        during = _sample(client.get("/metrics").text, "db_pool_checked_out")
    after = _sample(client.get("/metrics").text, "db_pool_checked_out")

    assert during == before + 2
    assert after == before
