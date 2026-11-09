# mypy: disable-error-code="arg-type"
# This suite passes a recording session stand-in to handlers that declare
# ``db: Session``. Declared once here rather than as a cast at every call site —
# a cast buries the thing being asserted, and widening a production signature to
# suit a test is worse. Same convention as the ``test_*_session_lifetime.py`` task modules.
"""API handlers must not hold the request transaction across a network call.

Sibling of the ``test_*_session_lifetime.py`` task modules, which covers the Celery half. The
defect is identical; only the session's provenance differs, and that difference
is why these needed their own harness:

* A **task** opens ``with session_scope() as db:``. The scope is visible, and the
  fix is to close it before the slow phase and reopen after.
* An **endpoint** receives ``db: Session = Depends(get_db)``. That session is
  opened by FastAPI before the handler body runs — by the auth dependency, in
  fact — and closed only when the response has been returned. There is no ``with``
  block to shorten. An OpenSearch round trip in the middle of such a handler holds
  a Postgres transaction for its whole duration, and a plain SELECT takes ACCESS
  SHARE for the life of its transaction: it queues every ``ALTER TABLE`` (any
  Alembic upgrade, which dev runs on backend startup), pins the vacuum horizon on
  ``transcript_segment``, and burns a pool connection. Shorter-lived than a Celery
  task, but the same lock on the same tables — and a slow cluster under
  concurrency is how a pool gets exhausted.

The fix, and what these tests pin: read what the response needs as **plain data**,
release the request transaction with ``db.close()``, then do the network work.
Where a write follows, the handler reuses ``db``, which begins a **second short**
transaction rather than resuming a held one.

========================================  =============================================
handler                                   network call formerly on the request session
========================================  =============================================
``POST /search/repair-indices``           8 OpenSearch probes across 4 indices
``GET /admin/backup/status``              OpenSearch snapshot reachability probe
========================================  =============================================

The two summarization handlers used to head that table. They no longer make a
network call at all — the ``transcript_summaries`` index is retired (#67) — so
their tests were deleted rather than kept as assertions about a call that cannot
happen; see the note where section 1 was.

The tests are behavioural, not structural: each wraps the real savepointed
``db_session`` in :class:`TrackedRequestSession` and has the stub for each
network call report whether a transaction was open **at the moment it ran**. Each
also asserts the handler really did read (or write) Postgres, so a handler that
never touches the DB — or one gutted into returning a constant — cannot pass.
"""

from types import SimpleNamespace

import pytest
from opensearchpy.exceptions import TransportError

from app.core.embedding_models import get_speaker_index
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings
from app.models.user import User
from tests.unit._session_lifetime_trackers import TrackedRequestSession


def _leak(tracker: TrackedRequestSession, label: str) -> str:
    return (
        f"the request transaction was held across the network call ({label}): "
        f"{tracker.observations}"
    )


@pytest.fixture
def tracked_db(db_session):
    """The savepointed test session, wrapped so transaction lifetime is observable.

    **Enters every handler with a transaction already open**, because that is what
    production does: FastAPI resolves ``Depends(get_current_admin_user)`` (and every
    other auth dependency) through this SAME request session *before* the handler
    body runs, so the SELECT that loaded the user has already begun one.

    This is not decoration. Without it the harness entered each handler with a
    closed transaction, so ``repair_indices`` — which touches Postgres only at the
    very end — reported "no transaction open" with its ``db.close()`` DELETED. The
    test could not fail, which is worse than no test.
    """
    tracker = TrackedRequestSession(db_session)
    tracker.query(User).filter(User.id == -1).first()
    assert tracker.open, "the harness must enter handlers with a request transaction open"
    return tracker


def _ctx(user):
    """A personal-scope ``RequestContext`` stand-in (no org tenant gate)."""
    return SimpleNamespace(user=user, org_id=None, org_role=None)


def _deployment_ctx(user):
    """A real context whose bypass holds the deployment-scope right (single-tenant admin)."""
    from app.services.platform_access import TenancyMode
    from app.services.platform_bypass import PlatformBypass
    from app.services.request_context import RequestContext

    return RequestContext(
        user=user,
        org_id=None,
        org_role=None,
        bypass=PlatformBypass(user=user, mode=TenancyMode.SINGLE),
    )


# --------------------------------------------------------------------------- #
# 1. (removed) GET/DELETE /files/{uuid}/summary — the OpenSearch read on the
#    request session.
#
#    Those handlers no longer make one. The ``transcript_summaries`` index they
#    read and deleted from is retired (#67) and the summary is served straight
#    from ``media_file.summary_data``, so the read/release/write phase split went
#    with it. Tests asserting "the OpenSearch hop ran with no transaction open"
#    would now be asserting that a call which does not exist is not made inside a
#    transaction — green forever, and unable to fail. Endpoint behaviour is
#    covered by ``tests/api/test_summarization_endpoints.py``; the retirement
#    itself by ``tests/unit/test_transcript_summaries_index_retired.py``.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 2. POST /search/repair-indices — eight OpenSearch probes on the request session
# --------------------------------------------------------------------------- #
class _FakeIndices:
    def __init__(self, tracker, healthy: bool):
        self._tracker = tracker
        self._healthy = healthy

    def exists(self, index):
        self._tracker.observe("opensearch_exists")
        return True

    def create(self, **kwargs):  # pragma: no cover - never reached in these tests
        self._tracker.observe("opensearch_create")


class _FakeOpenSearchClient:
    def __init__(self, tracker, *, healthy: bool = True):
        self._tracker = tracker
        self._healthy = healthy
        self.indices = _FakeIndices(tracker, healthy)

    def search(self, index, body):
        self._tracker.observe("opensearch_search")
        if not self._healthy:
            raise TransportError(500, "index is corrupt")
        return {"hits": {"total": {"value": 0}}}


@pytest.fixture
def repair_env(monkeypatch):
    """Patch the OpenSearch client and the two repair strategies."""
    from app.services import opensearch_service as oss

    recorded: dict = {}

    def _install(tracker, *, healthy: bool = True):
        monkeypatch.setattr(
            oss, "opensearch_client", _FakeOpenSearchClient(tracker, healthy=healthy)
        )

        def _repair(idx):
            tracker.observe("opensearch_repair")
            recorded.setdefault("repaired", []).append(idx)
            return True

        def _rebuild(db):
            # The one repair that legitimately reads Postgres: it must run WITH a
            # transaction, which is what proves the release above was scoped and
            # not a blanket "never touch the DB".
            db.query(MediaFile).filter(MediaFile.id == -1).first()
            tracker.observe("postgres_rebuild_speakers")
            recorded["rebuilt"] = True
            return {"status": "rebuilt", "speakers_indexed": 7}

        monkeypatch.setattr(oss, "_repair_index", _repair)
        monkeypatch.setattr(oss, "rebuild_speaker_index", _rebuild)
        return recorded

    return _install


def test_repair_indices_probes_opensearch_outside_the_transaction(
    admin_user, tracked_db, repair_env
):
    """All eight probes run with the request transaction released."""
    from app.api.endpoints import search_index_health as search_ep

    repair_env(tracked_db, healthy=True)

    result = search_ep.repair_indices(
        db=tracked_db, ctx=_deployment_ctx(admin_user), current_user=admin_user
    )

    assert tracked_db.seen["opensearch_exists"] is False, _leak(tracked_db, "opensearch_exists")
    assert tracked_db.seen["opensearch_search"] is False, _leak(tracked_db, "opensearch_search")
    assert tracked_db.open is False

    # Four indices were really probed — a handler that skipped the work would also
    # report "no transaction open".
    assert result["status"] == "success"
    assert len(result["indices"]) == 4
    assert set(result["indices"].values()) == {"healthy"}
    probes = [label for label, _ in tracked_db.observations]
    assert probes.count("opensearch_exists") == 4
    assert probes.count("opensearch_search") == 4


def test_repair_indices_rebuilds_speakers_in_a_transaction_of_its_own(
    admin_user, tracked_db, repair_env
):
    """The Postgres rebuild is the LAST phase and does get a live transaction."""
    from app.api.endpoints import search_index_health as search_ep

    recorded = repair_env(tracked_db, healthy=False)

    result = search_ep.repair_indices(
        db=tracked_db, ctx=_deployment_ctx(admin_user), current_user=admin_user
    )

    assert recorded["rebuilt"] is True
    assert result["speakers_indexed"] == 7
    assert result["indices"][get_speaker_index()] == "rebuilt"
    # Every OpenSearch hop ran released...
    assert tracked_db.seen["opensearch_exists"] is False, _leak(tracked_db, "opensearch_exists")
    assert tracked_db.seen["opensearch_search"] is False, _leak(tracked_db, "opensearch_search")
    assert tracked_db.seen["opensearch_repair"] is False, _leak(tracked_db, "opensearch_repair")
    # ...and the DB-bound rebuild ran with one, in its own short transaction.
    assert tracked_db.seen["postgres_rebuild_speakers"] is True
    labels = [label for label, _ in tracked_db.observations]
    assert labels[-1] == "postgres_rebuild_speakers", labels


# --------------------------------------------------------------------------- #
# 3. GET /admin/backup/status — OpenSearch snapshot probe on the request session
# --------------------------------------------------------------------------- #
def test_backup_status_probes_opensearch_outside_the_transaction(
    db_session, admin_user, tracked_db, monkeypatch
):
    """The snapshot reachability probe must run with the request transaction released."""
    from app.api.endpoints import backup_settings as bset
    from app.services import opensearch_snapshot

    db_session.add(SystemSettings(key="backup.include_opensearch", value="true"))
    db_session.flush()

    def _status():
        tracked_db.observe("opensearch_snapshot_status")
        return {"reachable": True, "repository_registered": True, "last_snapshot": "snap-1"}

    monkeypatch.setattr(opensearch_snapshot, "snapshot_status", _status)

    result = bset.get_backup_status(db=tracked_db, current_user=admin_user)

    assert tracked_db.seen["opensearch_snapshot_status"] is False, _leak(
        tracked_db, "opensearch_snapshot_status"
    )
    assert tracked_db.open is False

    # The settings really were read from Postgres first, so the probe cannot pass
    # by being skipped or by the handler never touching the DB.
    assert tracked_db.opened == 1, f"expected exactly one read transaction, got {tracked_db.opened}"
    assert result.include_opensearch is True
    assert result.opensearch_snapshot_status is not None
    assert result.opensearch_snapshot_status.last_snapshot == "snap-1"


def test_backup_status_skips_the_probe_when_opensearch_is_excluded(
    db_session, admin_user, tracked_db, monkeypatch
):
    """No probe at all when the setting is off — the response contract is unchanged."""
    from app.api.endpoints import backup_settings as bset
    from app.services import opensearch_snapshot

    db_session.add(SystemSettings(key="backup.include_opensearch", value="false"))
    db_session.flush()

    def _status():  # pragma: no cover - must not run
        tracked_db.observe("opensearch_snapshot_status")
        return {"reachable": True, "repository_registered": True, "last_snapshot": None}

    monkeypatch.setattr(opensearch_snapshot, "snapshot_status", _status)

    result = bset.get_backup_status(db=tracked_db, current_user=admin_user)

    assert result.include_opensearch is False
    assert result.opensearch_snapshot_status is None
    assert "opensearch_snapshot_status" not in tracked_db.seen


# --------------------------------------------------------------------------- #
# GET /llm/status and GET /files/{uuid}/summary-status: the provider health check
#
# Both are ``async def`` and await ``is_llm_available``, which dials the configured LLM
# provider's models endpoint (3 s timeout) and opens its OWN session to read the
# settings. The request session (opened by the auth dependency) used to stay open across
# that dial, so each call held two pool connections, one of them idle in a transaction
# for the whole outbound round trip. /llm/status is polled by every open SPA tab.
# --------------------------------------------------------------------------- #
def _observing_llm_check(tracker: TrackedRequestSession, available: bool):
    async def _is_llm_available(user_id=None):
        tracker.observe("llm_health_check")
        return available

    return _is_llm_available


class _FakeLLMForStatus:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            provider=SimpleNamespace(value="openai"), model="m", max_tokens=8192
        )

    def close(self) -> None:
        return None


def test_llm_status_dials_the_provider_outside_the_transaction(
    normal_user, tracked_db, monkeypatch
):
    import asyncio

    from app.api.endpoints import llm_status as status_ep

    monkeypatch.setattr(status_ep, "is_llm_available", _observing_llm_check(tracked_db, True))
    monkeypatch.setattr(
        status_ep.LLMService, "create_from_settings", lambda user_id=None: _FakeLLMForStatus()
    )

    result = asyncio.run(status_ep.get_llm_status(current_user=normal_user, db=tracked_db))

    assert tracked_db.seen["llm_health_check"] is False, _leak(tracked_db, "llm_health_check")
    assert result["available"] is True
    assert result["user_id"] == normal_user.id
    assert result["provider"] == "openai"
    assert result["context_window"] == 8192


def test_summary_status_dials_the_provider_outside_the_transaction(
    normal_user, db_session, tracked_db, monkeypatch
):
    import asyncio
    import uuid

    from app.api.endpoints.files import summary_status as summary_ep

    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="lifetime.wav",
        title="lifetime",
        storage_path="media/test/lifetime.wav",
        content_type="audio/wav",
        file_size=1,
        status="completed",
        summary_status="failed",
        is_public=False,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.flush()
    monkeypatch.setattr(summary_ep, "is_llm_available", _observing_llm_check(tracked_db, True))

    result = asyncio.run(
        summary_ep.get_summary_status(
            file_uuid=str(media_file.uuid),
            db=tracked_db,
            current_user=normal_user,
            ctx=_deployment_ctx(normal_user),
        )
    )

    assert tracked_db.seen["llm_health_check"] is False, _leak(tracked_db, "llm_health_check")
    # Read from the row, through the request session, before it was released.
    assert result["file_id"] == str(media_file.uuid)
    assert result["filename"] == "lifetime.wav"
    assert result["summary_status"] == "failed"
    assert result["llm_available"] is True
    assert result["can_retry"] is True
