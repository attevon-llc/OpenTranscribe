"""Scrape cost of ``/metrics`` and the autoscaler-sized ``/metrics/queues`` (issue #1001).

An autoscaler polling ``/metrics`` every few seconds kept an idle single-process
backend at about one CPU core: each scrape opened two DB sessions to re-read
backup/mirror job state that changes a few times a day, then rendered every
histogram series in the process.

- The backup/media-mirror projection is now refreshed at most once per TTL.
- ``/metrics/queues`` exposes ONLY the Celery queue gauges, from one pipelined
  Redis round trip and no database access, for pollers that need nothing else.
"""

from __future__ import annotations

import json
import time

import pytest
from kombu.transport.redis import Channel
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import event

from app.core import backup_metrics
from app.core.constants import CeleryQueues


@pytest.fixture
def settings_reads(monkeypatch):
    """Count reads of persisted job state (both projections go through this call)."""
    from app.services import system_settings_service as sss

    calls: list[list[str]] = []
    original = sss.get_settings_map

    def _counting(db, keys):
        calls.append(list(keys))
        return original(db, keys)

    monkeypatch.setattr(sss, "get_settings_map", _counting)
    return calls


def test_job_metrics_are_read_from_the_db_at_most_once_per_ttl(client, monkeypatch, settings_reads):
    # A controllable clock far ahead of any real monotonic reading, so whatever an
    # earlier test in this worker left behind is already stale.
    now = [1.0e12]
    monkeypatch.setattr(backup_metrics, "_clock", lambda: now[0], raising=False)
    ttl = getattr(backup_metrics, "JOB_METRICS_TTL_SECONDS", 60.0)

    assert client.get("/metrics").status_code == 200
    first = len(settings_reads)
    assert first == 2, "one read for backup state, one for media-mirror state"

    for _ in range(3):
        assert client.get("/metrics").status_code == 200
    assert len(settings_reads) == first, "scrapes inside the TTL must not touch the DB"

    now[0] += ttl + 1
    assert client.get("/metrics").status_code == 200
    assert len(settings_reads) == first + 2, "the first scrape after the TTL refreshes"


class _FakePipeline:
    def __init__(self, llen_map, unacked):
        self.commands: list[tuple[str, str]] = []
        self._llen_map = llen_map
        self._unacked = unacked

    def llen(self, key):
        self.commands.append(("llen", key))
        return self

    def hgetall(self, key):
        self.commands.append(("hgetall", key))
        return self

    def zrange(self, key, start, end, withscores=False):
        self.commands.append(("zrange", key))
        return self

    def execute(self):
        # LLEN -> int, HGETALL -> {tag: entry}, ZRANGE -> [(tag, delivery time)].
        unacked = {f"tag-{i}": value for i, value in enumerate(self._unacked)}
        results: list = []
        for cmd, key in self.commands:
            if cmd == "llen":
                results.append(self._llen_map.get(key, 0))
            elif cmd == "hgetall":
                results.append(unacked)
            else:
                results.append([(tag, time.time()) for tag in unacked])
        return results


class _FakeBroker:
    """Only ``pipeline()`` is offered: any direct command would be an extra round trip."""

    def __init__(self, llen_map, unacked):
        self._llen_map = llen_map
        self._unacked = unacked
        self.pipelines: list[_FakePipeline] = []

    def pipeline(self, transaction=False):
        pipe = _FakePipeline(self._llen_map, self._unacked)
        self.pipelines.append(pipe)
        return pipe


def test_queue_endpoint_is_one_redis_round_trip_and_no_db(client, monkeypatch, settings_reads):
    from app.db.base import engine

    fake = _FakeBroker(
        llen_map={"gpu": 4, f"gpu{Channel.sep}5": 3},
        unacked=[json.dumps([{}, "gpu", "gpu"]), json.dumps([{}, "cpu", "cpu"])],
    )
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)

    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        resp = client.get("/metrics/queues")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert len(fake.pipelines) == 1, "exactly one Redis round trip"
    assert statements == [], f"queue endpoint issued SQL: {statements}"
    assert settings_reads == []

    samples = {
        (s.name, s.labels.get("queue")): s.value
        for fam in text_string_to_metric_families(resp.text)
        for s in fam.samples
    }
    assert {name for name, _ in samples} == {
        "celery_queue_depth",
        "celery_queue_reserved",
        "celery_queue_orphaned",
        "celery_queue_oldest_unacked_age_seconds",
    }
    assert samples[("celery_queue_depth", "gpu")] == 7
    assert samples[("celery_queue_reserved", "gpu")] == 1
    assert samples[("celery_queue_reserved", "cpu")] == 1
    assert {q for _, q in samples} == set(CeleryQueues.ALL)


def test_full_metrics_still_carries_the_queue_gauges(client):
    """The small endpoint is an addition: ``/metrics`` keeps every family it had."""
    body = client.get("/metrics").text
    assert "celery_queue_depth" in body
    assert "celery_queue_reserved" in body
    assert "backup_last_status" in body
