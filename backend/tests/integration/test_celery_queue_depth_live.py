"""Live-kombu proof for ``app.core.celery_metrics.queue_snapshot`` (issue #892).

The unit tests in ``tests/unit/test_celery_queue_depth.py`` assume kombu really
shards a queue into 10 priority sub-lists and really records a fetched-but-
unacknowledged message in its ``unacked`` hash. This test proves both, against
the REAL installed kombu and the REAL ``opentranscribe-redis`` container --
no celery worker involved, so nothing here ever executes a task.

Database **15**, never 0: ``INFO keyspace`` on the live container shows db0
only among 16 configured, so db 15 cannot collide with real broker traffic and
no running worker will ever consume from it.

Run:
    cd backend && PYTHONPATH=. pytest -m integration tests/integration/test_celery_queue_depth_live.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import redis as redis_lib
from dotenv import dotenv_values
from kombu import Connection
from kombu import Producer
from kombu import Queue

from app.core.celery_metrics import queue_snapshot
from app.utils.stats_helpers import get_queue_depths

# ``xdist_group`` is the ONLY isolation available here, and it is required.
#
# Both tests publish into one real queue on one Redis db. Under ``-n auto`` they
# landed on different workers and each counted the other's message -- measured
# 2026-09-12 on ``gw4``/``gw10``: ``pending == 1`` saw ``2``, and ``llen(...) == 0``
# saw ``1``.
#
# Two independent reasons a per-test queue name does NOT fix this, so don't try:
#   1. ``queue_snapshot`` reports only DECLARED queues (``CeleryQueues.ALL``), so
#      an invented name is absent from the result and every assertion KeyErrors.
#      See ``queue_name``'s docstring.
#   2. kombu keeps ONE unacked hash per Redis DB (``Channel.unacked_key`` +
#      ``unacked_index``), not one per queue, and ``raw_client``'s teardown
#      deletes it outright -- so concurrent tests would still wipe each other's
#      reservation no matter what their queues were called.
# Sharing a worker is what actually closes both.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group("celery_queue_depth_live"),
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT_ENV = _REPO_ROOT / ".env"

_TEST_DB = 15
_TEST_QUEUE = "utility"
_TEST_PRIORITY = 7


def _repo_env_value(key: str, default: str = "") -> str:
    """Read a value from the repo-root .env via python-dotenv, matching the
    existing convention in test_gpu_scale_smoke_live.py -- never a hand-rolled
    grep/cut, and never touching backend/tests/conftest.py."""
    if os.environ.get(key):
        return os.environ[key]
    if _REPO_ROOT_ENV.is_file():
        value = dotenv_values(_REPO_ROOT_ENV).get(key)
        if value:
            return str(value)
    return default


def _redis_url() -> str:
    password = _repo_env_value("REDIS_PASSWORD")
    port = _repo_env_value("REDIS_PORT", "5177")
    auth = f":{password}@" if password else ""
    return f"redis://{auth}localhost:{port}/{_TEST_DB}"


@pytest.fixture
def queue_name() -> str:
    """The queue these tests publish into.

    ⚠️ This CANNOT be made unique per test, and that is a property of the code
    under test, not an oversight. ``queue_snapshot`` builds its result as
    ``{name: ... for name in CeleryQueues.ALL}`` -- it reports only DECLARED
    queues -- so a uuid4-suffixed name is simply absent from the snapshot and
    every assertion here dies on ``KeyError``. Verified by trying it: a
    ``otqueuedepth-<uuid4>`` name failed both tests that way on 2026-09-12.

    Isolation therefore comes entirely from the module's ``xdist_group``. This
    fixture exists to give that constraint one documented home, so the next
    person reaching for the obvious uuid4 fix finds out here rather than from a
    red gate.
    """
    return _TEST_QUEUE


@pytest.fixture
def raw_client(queue_name: str):
    client = redis_lib.from_url(_redis_url())
    try:
        client.ping()
    except redis_lib.exceptions.RedisError as exc:
        pytest.skip(f"live Redis (db {_TEST_DB}) unreachable: {exc}")
    yield client
    # Teardown: delete exactly the keys this test touched. NEVER FLUSHDB --
    # db 15 is verified empty before this test runs, not owned outright.
    #
    # `unacked_key`/`unacked_index` are kombu's PER-DB globals, not per-queue, so
    # deleting them here is safe only because `xdist_group` (top of file) keeps
    # every test in this module on one worker. If that marker is ever removed,
    # these two deletes silently destroy a concurrent test's reservation.
    from kombu.transport.redis import Channel

    sep = Channel.sep
    keys_to_delete = [queue_name, f"{queue_name}{sep}{_TEST_PRIORITY}"]
    client.delete(*keys_to_delete, Channel.unacked_key, "unacked_index")
    client.close()


def test_a_task_at_a_non_default_priority_is_invisible_to_a_bare_llen_but_counted(
    raw_client, queue_name
):
    """AC1 end to end: publish at priority 7 with real kombu, no worker running."""
    with Connection(
        _redis_url(),
        transport_options={"priority_steps": list(range(10)), "queue_order_strategy": "priority"},
    ) as conn:
        channel = conn.channel()
        producer = Producer(channel)
        producer.publish(
            {"task": "noop", "args": [], "kwargs": {}},
            routing_key=queue_name,
            priority=_TEST_PRIORITY,
            declare=[Queue(queue_name)],
        )

        # Today's answer -- exactly the undercount #892 is about.
        assert raw_client.llen(queue_name) == 0

        snapshot = queue_snapshot(raw_client)
        assert snapshot[queue_name]["pending"] == 1


def test_a_real_reservation_with_no_worker_is_counted_as_reserved(
    raw_client, queue_name, monkeypatch
):
    """AC2 end to end: a Consumer that fetches-but-never-acks is exactly what a
    celery worker's prefetch buffer looks like on the wire -- no celery worker
    process is needed to produce that state.
    """
    from kombu.transport.redis import Channel

    with Connection(
        _redis_url(),
        transport_options={"priority_steps": list(range(10)), "queue_order_strategy": "priority"},
    ) as conn:
        channel = conn.channel()
        producer = Producer(channel)
        producer.publish(
            {"task": "noop", "args": [], "kwargs": {}},
            routing_key=queue_name,
            priority=_TEST_PRIORITY,
            declare=[Queue(queue_name)],
        )

        received: list[object] = []

        def _on_message(body, message):
            received.append(message)
            # Deliberately no ack() -- this IS the state under test.

        with conn.Consumer(queues=[Queue(queue_name)], callbacks=[_on_message], no_ack=False):
            conn.drain_events(timeout=5)

        assert received, "the consumer never received the published message"

        sep = Channel.sep
        assert raw_client.llen(queue_name) == 0
        assert raw_client.llen(f"{queue_name}{sep}{_TEST_PRIORITY}") == 0

        snapshot = queue_snapshot(raw_client)
        assert snapshot[queue_name]["reserved"] == 1

        # get_queue_depths() defaults to the real broker (db 0); point it at
        # THIS test's db 15 client instead of measuring live production traffic.
        monkeypatch.setattr("app.core.redis.get_redis", lambda: raw_client)
        depths = get_queue_depths()
        assert depths[queue_name] == 1
