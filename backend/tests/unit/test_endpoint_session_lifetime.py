# mypy: disable-error-code="arg-type"
# This suite passes a recording session stand-in to handlers that declare
# ``db: Session``. Declared once here rather than as a cast at every call site —
# a cast buries the thing being asserted, and widening a production signature to
# suit a test is worse. Same convention as ``test_task_session_lifetime.py``.
"""API handlers must not hold the request transaction across a network call.

Sibling of ``test_task_session_lifetime.py``, which covers the Celery half. The
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
Where a write follows (``delete_summary``) the handler reuses ``db``, which begins
a **second short** transaction rather than resuming a held one.

========================================  =============================================
handler                                   network call formerly on the request session
========================================  =============================================
``GET /files/{uuid}/summary``             OpenSearch summary read
``DELETE /files/{uuid}/summary``          OpenSearch index probe + document delete
``POST /search/repair-indices``           8 OpenSearch probes across 4 indices
``GET /admin/backup/status``              OpenSearch snapshot reachability probe
========================================  =============================================

The tests are behavioural, not structural: each wraps the real savepointed
``db_session`` in :class:`_TrackedRequestSession` and has the stub for each
network call report whether a transaction was open **at the moment it ran**. Each
also asserts the handler really did read (or write) Postgres, so a handler that
never touches the DB — or one gutted into returning a constant — cannot pass.
"""

import uuid as uuid_mod
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.constants import get_speaker_index
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings
from app.models.user import User

#: Session methods that (re)open a transaction on first use.
_DB_WORK = frozenset(
    {
        "query",
        "execute",
        "add",
        "add_all",
        "flush",
        "refresh",
        "get",
        "scalar",
        "scalars",
        "merge",
        "delete",
        "bulk_save_objects",
    }
)

#: Session methods that END the current transaction and release the connection.
_DB_RELEASE = frozenset({"commit", "rollback", "close"})


class _TrackedRequestSession:
    """Stands in for the request-scoped ``Depends(get_db)`` session.

    Records whether a Postgres transaction is open at the moment each network stub
    runs, and how many separate transactions the handler opened.

    ``close()`` is deliberately **not** forwarded: the ``db_session`` fixture is
    savepoint-isolated inside the test's own transaction, and a real close would
    unwind it. It is translated to ``expunge_all()``, which reproduces the part
    that matters — every ORM instance the handler loaded becomes **detached**,
    exactly as after ``get_db``'s session is released — so a handler that kept
    reading an ORM row past the release fails here rather than silently working.
    """

    def __init__(self, session):
        self._session = session
        #: Is a transaction currently open?
        self.open = False
        #: How many distinct transactions were opened.
        self.opened = 0
        #: (label, transaction-open?) reported from inside each network call.
        self.observations: list[tuple[str, bool]] = []

    def observe(self, label: str) -> None:
        self.observations.append((label, self.open))

    @property
    def seen(self) -> dict[str, bool]:
        return dict(self.observations)

    def __getattr__(self, name: str):
        attr = getattr(self._session, name)
        if name in _DB_WORK:

            def _work(*args, **kwargs):
                if not self.open:
                    self.open = True
                    self.opened += 1
                return attr(*args, **kwargs)

            return _work
        if name in _DB_RELEASE:

            def _release(*args, **kwargs):
                self.open = False
                if name == "close":
                    # See the class docstring — never really close the harness session.
                    return self._session.expunge_all()
                return attr(*args, **kwargs)

            return _release
        return attr


def _leak(tracker: _TrackedRequestSession, label: str) -> str:
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
    tracker = _TrackedRequestSession(db_session)
    tracker.query(User).filter(User.id == -1).first()
    assert tracker.open, "the harness must enter handlers with a request transaction open"
    return tracker


def _ctx(user):
    """A personal-scope ``RequestContext`` stand-in (no org tenant gate)."""
    return SimpleNamespace(user=user, org_id=None, org_role=None)


# --------------------------------------------------------------------------- #
# 1. GET /files/{uuid}/summary — OpenSearch read on the request session
# --------------------------------------------------------------------------- #
class _FakeSummaryIndex:
    """Stands in for ``OpenSearchSummaryService``.

    Its **constructor** is a round trip too — the real one calls
    ``indices.exists`` (and may ``indices.create``) from ``__init__``, which is
    why the auditor flagged the bare construction as well as the query.
    """

    def __init__(self, tracker, recorded, *, summary: dict[str, Any] | None = None):
        self._tracker = tracker
        self._recorded = recorded
        self._summary = summary
        tracker.observe("opensearch_connect")

    def get_summary_by_file_id(self, file_id, user_id):
        self._tracker.observe("opensearch_get_summary")
        self._recorded["get_args"] = (file_id, user_id)
        return self._summary

    def delete_summary(self, document_id):
        self._tracker.observe("opensearch_delete_summary")
        self._recorded["deleted"] = document_id
        return True


def _make_summarised_file(db_session, user, **overrides):
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="meeting.mp4",
        storage_path="test/meeting.mp4",
        content_type="video/mp4",
        file_size=1000,
        **overrides,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


@pytest.fixture
def summary_endpoint_env(monkeypatch):
    """Patch the summarization endpoints' OpenSearch seam; keep the DB real."""
    from app.api.endpoints import summarization as summ

    recorded: dict = {}

    def _install(tracker, *, summary=None):
        monkeypatch.setattr(
            summ,
            "OpenSearchSummaryService",
            lambda: _FakeSummaryIndex(tracker, recorded, summary=summary),
        )
        return recorded

    return _install


def test_get_file_summary_reads_opensearch_with_no_transaction_open(
    db_session, normal_user, tracked_db, summary_endpoint_env
):
    """The regression: the OpenSearch read must run with the request transaction closed."""
    from app.api.endpoints import summarization as summ

    media_file = _make_summarised_file(db_session, normal_user, title="Weekly sync")
    recorded = summary_endpoint_env(
        tracked_db,
        summary={
            "summary_data": {"bluf": "They agreed."},
            "document_id": "summary-doc-1",
            "created_at": None,
            "updated_at": None,
        },
    )

    response = summ.get_file_summary(
        file_uuid=str(media_file.uuid),
        current_user=normal_user,
        ctx=_ctx(normal_user),
        db=tracked_db,
    )

    assert tracked_db.seen["opensearch_connect"] is False, _leak(tracked_db, "opensearch_connect")
    assert tracked_db.seen["opensearch_get_summary"] is False, _leak(
        tracked_db, "opensearch_get_summary"
    )
    assert tracked_db.open is False

    # The read phase really happened, so "no transaction open" cannot be satisfied
    # by a handler that never touched Postgres.
    assert tracked_db.opened == 1, f"expected exactly one read transaction, got {tracked_db.opened}"
    assert recorded["get_args"] == (media_file.id, normal_user.id)
    assert response.source == "opensearch"
    assert response.filename == "Weekly sync"
    assert str(response.file_id) == str(media_file.uuid)
    assert response.summary_data == {"bluf": "They agreed."}


def test_get_file_summary_falls_back_to_postgres_from_the_snapshot(
    db_session, normal_user, tracked_db, summary_endpoint_env
):
    """The PostgreSQL fallback must be served from plain data read BEFORE the release."""
    from app.api.endpoints import summarization as summ

    media_file = _make_summarised_file(
        db_session, normal_user, summary_data={"bluf": "From Postgres."}
    )
    summary_endpoint_env(tracked_db, summary=None)

    response = summ.get_file_summary(
        file_uuid=str(media_file.uuid),
        current_user=normal_user,
        ctx=_ctx(normal_user),
        db=tracked_db,
    )

    assert response.source == "postgresql"
    assert response.summary_data == {"bluf": "From Postgres."}
    assert tracked_db.seen["opensearch_get_summary"] is False, _leak(
        tracked_db, "opensearch_get_summary"
    )
    # Served without reopening a transaction after the release.
    assert tracked_db.opened == 1


def test_summary_snapshot_returns_plain_data(db_session, normal_user, tracked_db):
    """``_load_summary_snapshot`` must not hand back live ORM instances."""
    from app.api.endpoints import summarization as summ

    media_file = _make_summarised_file(
        db_session,
        normal_user,
        summary_data={"bluf": "x"},
        summary_opensearch_id="summary-doc-1",
    )

    snapshot = summ._load_summary_snapshot(
        tracked_db, str(media_file.uuid), normal_user, _ctx(normal_user)
    )

    assert snapshot["file_id"] == media_file.id
    assert snapshot["summary_opensearch_id"] == "summary-doc-1"
    assert snapshot["summary_data"] == {"bluf": "x"}
    for value in snapshot.values():
        assert not isinstance(value, MediaFile)


def test_delete_summary_calls_opensearch_outside_the_transaction(
    db_session, normal_user, tracked_db, summary_endpoint_env
):
    """Read, then delete the document with nothing held, then write in a NEW transaction."""
    from app.api.endpoints import summarization as summ

    media_file = _make_summarised_file(
        db_session,
        normal_user,
        summary_data={"bluf": "gone soon"},
        summary_opensearch_id="summary-doc-1",
    )
    recorded = summary_endpoint_env(tracked_db)

    result = summ.delete_summary(
        file_uuid=str(media_file.uuid),
        current_user=normal_user,
        ctx=_ctx(normal_user),
        db=tracked_db,
    )

    assert tracked_db.seen["opensearch_connect"] is False, _leak(tracked_db, "opensearch_connect")
    assert tracked_db.seen["opensearch_delete_summary"] is False, _leak(
        tracked_db, "opensearch_delete_summary"
    )
    assert recorded["deleted"] == "summary-doc-1"

    # A read transaction and a separate write transaction — not one long one.
    assert tracked_db.opened >= 2, (
        f"expected a read transaction and a write transaction, got {tracked_db.opened}"
    )
    assert result["file_id"] == str(media_file.uuid)

    db_session.expire_all()
    refreshed = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).first()
    assert refreshed.summary_data is None
    assert refreshed.summary_opensearch_id is None


def test_delete_summary_without_a_summary_is_404(
    db_session, normal_user, tracked_db, summary_endpoint_env
):
    """The 404 path must survive the restructure — and still not touch OpenSearch."""
    from fastapi import HTTPException

    from app.api.endpoints import summarization as summ

    media_file = _make_summarised_file(db_session, normal_user)
    summary_endpoint_env(tracked_db)

    with pytest.raises(HTTPException) as exc:
        summ.delete_summary(
            file_uuid=str(media_file.uuid),
            current_user=normal_user,
            ctx=_ctx(normal_user),
            db=tracked_db,
        )

    assert exc.value.status_code == 404
    assert "opensearch_delete_summary" not in tracked_db.seen


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
            raise RuntimeError("index is corrupt")
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
    from app.api.endpoints import search as search_ep

    repair_env(tracked_db, healthy=True)

    result = search_ep.repair_indices(db=tracked_db, current_user=admin_user)

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
    from app.api.endpoints import search as search_ep

    recorded = repair_env(tracked_db, healthy=False)

    result = search_ep.repair_indices(db=tracked_db, current_user=admin_user)

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
