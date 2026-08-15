"""Functional tests for the four embedding-consistency admin routes (``admin.py``).

``GET /api/admin/embedding-consistency/status`` · ``.../counts`` ·
``POST .../repair`` · ``POST .../stop`` — all four had **no functional
coverage**; the only mention of any of them in ``tests/`` was
``unit/test_route_has_a_caller.py``, which asserts a path exists and never issues
a request. (``GET /admin/data-integrity/status`` is the sibling panel and lives in
``test_admin_data_integrity_routes.py``.)

These are the operator's whole view of speaker-voiceprint drift between Postgres
and OpenSearch, and the buttons that repair it. The invariants pinned here:

* the tier is ``admin``; a plain user gets 403 and an anonymous caller 401;
* ``running`` is derived from the Redis lock key — asserted **both ways**, since a
  hardcoded ``False`` would make the panel offer "Repair" during a live repair;
* ``repair`` honours the ``already_running`` guard and returns **no** task id when
  it declines, so a double click cannot stack two GPU sweeps;
* ``stop`` answers ``not_running`` rather than pretending to cancel, and clears
  the lock when there really is one;
* ``counts`` is a dry run, and its buckets are pinned exactly against a faked
  index seam (so they hold in CI too), with a live cross-check of
  ``total_pg_speakers`` against an independently-written SQL count on top.

**Redis is faked; nothing else is.** These handlers *write* lock keys — priming the
real Redis with an ``embedding_consistency_running`` lock would make the live admin
panel believe a repair was under way. OpenSearch and Postgres reads run for real
against the dev stack, read-only. No repair is ever actually executed: Celery
dispatch is no-oped by the autouse ``_skip_celery_dispatch`` fixture.
"""

from __future__ import annotations

import json
import os
import uuid as uuid_pkg
from unittest.mock import patch

import pytest
from fastapi import status
from sqlalchemy import text

#: Mirrors conftest's stack detection: False on the dev host, True on a bare CI
#: runner. Only the live ``counts`` cross-check needs it — the arithmetic is pinned
#: unconditionally by the faked-seam test below. Without this split that test
#: **failed** (not skipped) in the CPU-only CI job, since ``get_speaker_index()``
#: resolves the alias over HTTP and an unreachable cluster is an uncaught
#: ConnectionError → 500.
_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

EC_STATUS = "/api/admin/embedding-consistency/status"
EC_COUNTS = "/api/admin/embedding-consistency/counts"
EC_REPAIR = "/api/admin/embedding-consistency/repair"
EC_STOP = "/api/admin/embedding-consistency/stop"

#: The lock keys the handlers under test read (and, for ``stop``, delete).
EC_LOCK = "embedding_consistency_running"
EC_PROGRESS = "embedding_consistency_progress"
EC_LAST_RUN = "embedding_consistency_last_run"


class _InMemoryRedis:
    """The four Redis operations these handlers use, backed by a dict.

    Deliberately tiny: a broader double would start asserting its own behaviour.
    ``exists``/``get``/``set``/``delete`` are the entire surface of
    ``get_embedding_consistency_status`` and ``stop_consistency_repair``.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, **_kwargs) -> bool:
        self.store[key] = value
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += 1 if self.store.pop(key, None) is not None else 0
        return removed


@pytest.fixture
def fake_redis():
    """Patch the task module's ``get_redis`` with an in-memory double.

    The module binds ``get_redis`` at import (``from app.core.redis import
    get_redis``), so the patch target is the *module attribute*, not
    ``app.core.redis``.
    """
    fake = _InMemoryRedis()
    with patch("app.tasks.speaker_embedding_consistency.get_redis", return_value=fake):
        yield fake


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
_ROUTES = [
    ("GET", EC_STATUS),
    ("GET", EC_COUNTS),
    ("POST", EC_REPAIR),
    ("POST", EC_STOP),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path):
    """Repair rewrites the deployment-wide voiceprint index; reads expose every user's counts.

    Catches the dependency being relaxed to ``get_current_active_user`` — any
    account could then start a GPU sweep over every other account's speakers, or
    stop one an admin had started.
    """
    response = client.request(method, path, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_every_route_requires_authentication(client, method, path):
    response = client.request(method, path)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /embedding-consistency/status
# ---------------------------------------------------------------------------
def test_embedding_status_idle_shape(client, admin_token_headers, fake_redis):
    response = client.get(EC_STATUS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {"running", "progress", "last_run"}
    assert body["running"] is False
    assert body["progress"] is None
    assert body["last_run"] is None


def test_embedding_status_reports_live_progress(client, admin_token_headers, fake_redis):
    """A repair in flight reports its lock *and* its decoded progress payload.

    Catches ``progress`` being read from the wrong key (it and ``last_run`` are
    adjacent constants) — the panel's progress bar would sit at zero for the whole
    of a multi-minute GPU sweep.
    """
    fake_redis.store[EC_LOCK] = "1"
    fake_redis.store[EC_PROGRESS] = json.dumps({"processed": 12, "total": 40, "user_id": 3})
    fake_redis.store[EC_LAST_RUN] = json.dumps({"repaired": 7})

    response = client.get(EC_STATUS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["running"] is True
    assert body["progress"] == {"processed": 12, "total": 40, "user_id": 3}
    assert body["last_run"] == {"repaired": 7}


# ---------------------------------------------------------------------------
# GET /embedding-consistency/counts
# ---------------------------------------------------------------------------
@pytest.fixture
def faked_index():
    """Replace the Postgres and OpenSearch reads of ``counts`` with a known scenario.

    Two speakers with segments (indexed, missing), one more without, and an index
    holding the indexed one, the segmentless one, and a document for a speaker that
    no longer exists in Postgres. Every output bucket therefore has exactly one
    distinguishable member, which is what makes the arithmetic falsifiable — an
    implementation conflating "orphan" with "no segments" produces the same totals
    as the real dev data and passes a cross-check.

    ``get_opensearch_client`` returns None so the v3/v4 ``indices.exists`` probes
    short-circuit; nothing here touches a cluster, so it runs in CI too.
    """
    indexed = str(uuid_pkg.uuid4())
    missing = str(uuid_pkg.uuid4())
    segmentless = str(uuid_pkg.uuid4())
    orphan = str(uuid_pkg.uuid4())

    module = "app.tasks.speaker_embedding_consistency"
    with (
        patch(
            f"{module}._get_pg_speaker_uuids_with_segments",
            return_value={indexed: 1, missing: 1},
        ),
        patch(
            f"{module}._get_all_pg_speaker_uuids",
            return_value={indexed, missing, segmentless},
        ),
        patch(
            f"{module}._get_opensearch_speaker_uuids",
            return_value={indexed, segmentless, orphan},
        ),
        patch("app.services.opensearch_service.get_opensearch_client", return_value=None),
        patch(
            "app.services.embedding_mode_service.EmbeddingModeService.get_current_mode",
            return_value="v3",
        ),
    ):
        yield {"indexed": indexed, "missing": missing}


def test_counts_reports_indexed_missing_orphaned_and_segmentless_exactly(
    client, admin_token_headers, fake_redis, faked_index
):
    """Every bucket of the dry-run count is pinned to an exact number.

    ``no_segments`` and ``orphans`` are the pair that matters: a speaker in
    OpenSearch with no transcript segments is expected and harmless, while one
    absent from Postgres entirely is a document that will never be reclaimed.
    Merging the two buckets — or counting the segmentless row as "missing" — puts a
    permanent nonzero backlog in front of the operator that no repair can clear.
    """
    response = client.get(EC_COUNTS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["mode"] == "v3"
    assert body["total_pg_speakers"] == 2
    assert body["main_indexed"] == 1
    assert body["main_missing"] == 1
    assert body["no_segments"] == 1
    assert body["orphans"] == 1
    assert body["unrepairable"] == 0
    assert body["v4_exists"] is False
    assert body["v4_missing"] == 0


@pytest.mark.skipif(_OPENSEARCH_ABSENT, reason="counts queries the live speakers index")
def test_counts_agrees_with_an_independent_speaker_count(
    client, admin_token_headers, fake_redis, db_session
):
    """``total_pg_speakers`` counts speakers **that have segments** — cross-checked.

    The control is written as a JOIN/DISTINCT rather than the handler's own
    ``EXISTS`` sub-select, so it fails if that filter is dropped: without it the
    count would include speakers with no transcript segments, and the panel would
    report a permanent backlog of "missing" embeddings that can never be built
    because there is no audio to extract them from.
    """
    expected = db_session.execute(
        text(
            "SELECT COUNT(DISTINCT s.id) FROM speaker s "
            "JOIN transcript_segment ts ON ts.speaker_id = s.id"
        )
    ).scalar_one()
    assert expected > 0, "the cross-check is vacuous against an empty speaker table"

    response = client.get(EC_COUNTS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total_pg_speakers"] == expected
    assert body["main_indexed"] + body["main_missing"] <= expected


def test_counts_writes_nothing_to_the_lock_namespace(
    client, admin_token_headers, fake_redis, faked_index
):
    """A dry-run count must not claim the repair lock.

    Catches ``get_embedding_consistency_counts`` being swapped for the repair
    orchestrator (they share a module and both start by scanning the same sets) —
    opening the admin panel would then start a GPU sweep nobody asked for, and the
    ``already_running`` guard would block the operator's real repair afterwards.
    """
    response = client.get(EC_COUNTS, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert fake_redis.store == {}


# ---------------------------------------------------------------------------
# POST /embedding-consistency/repair
# ---------------------------------------------------------------------------
def test_repair_dispatches_and_returns_the_task_id(client, admin_token_headers, fake_redis):
    """The happy path answers with the id the panel polls.

    Celery dispatch is no-oped by the autouse ``_skip_celery_dispatch`` fixture,
    so this exercises the handler body rather than a broker. Catches the handler
    returning the ``AsyncResult`` object or a placeholder id — the panel has
    nothing else to correlate progress against.
    """
    response = client.post(EC_REPAIR, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "started"
    assert body["task_id"] == "test-task-id"


def test_repair_declines_while_one_is_already_running(client, admin_token_headers, fake_redis):
    """The guard must refuse **and** hand back no task id.

    Catches the guard being dropped, or being kept while still dispatching: a
    second sweep would run concurrently on the single GPU worker, and both would
    write the same speaker documents. Absence of ``task_id`` is the assertion that
    proves nothing was queued — the response is the only thing the caller sees.
    """
    fake_redis.store[EC_LOCK] = "1"

    response = client.post(EC_REPAIR, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body == {"status": "already_running"}


# ---------------------------------------------------------------------------
# POST /embedding-consistency/stop
# ---------------------------------------------------------------------------
def test_stop_says_not_running_when_nothing_is_running(client, admin_token_headers, fake_redis):
    """Cancelling nothing reports ``not_running`` rather than a fake success.

    Catches the ``exists`` guard being dropped: the panel would report "Stopped"
    for a repair that was never running, which is indistinguishable from a repair
    that failed to start.
    """
    response = client.post(EC_STOP, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "not_running"}


def test_stop_clears_the_lock_and_progress_when_a_repair_is_running(
    client, admin_token_headers, fake_redis
):
    """Stopping releases the lock, or the next repair is blocked forever.

    The lock has a TTL, but until it expires ``already_running`` refuses every new
    repair — so a stop that reported success without deleting the key would leave
    the feature dead with no error anywhere. No batch ids are stored, so nothing is
    revoked and no broker is contacted.
    """
    fake_redis.store[EC_LOCK] = "1"
    fake_redis.store[EC_PROGRESS] = json.dumps({"processed": 3, "user_id": 1})

    response = client.post(EC_STOP, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "stopped"
    assert body["revoked_tasks"] == 0
    assert EC_LOCK not in fake_redis.store
    assert EC_PROGRESS not in fake_redis.store
