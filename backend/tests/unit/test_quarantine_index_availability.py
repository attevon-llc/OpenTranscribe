"""A taken-down (quarantined) file leaves the index read surfaces through the index itself.

Quarantine used to be Postgres-only: the transcript readers excluded a resolved ``must_not
terms file_uuid`` list (capped, failing closed with a 503 past the cap) and the speaker kNN
readers excluded nothing, so a taken-down file's voice could still be matched and suggested.
Now ``takedown_service.quarantine_file`` / ``release_file`` restamp the file's documents
(``availability_stamp``, the same ``update_by_query`` the trash uses) and every reader excludes
``availability: quarantined`` beside ``trashed``, except where the quarantine review exemption
applies (``file_availability.unavailable_list_exempt`` on the transcript plane).

The end-to-end tests run the real takedown actions against real rows (``db_session``) and an
in-memory cluster that HONOURS the query bodies it is sent (term / terms / exists / bool and
the ``filter`` of a ``knn`` query), so "the voice no longer matches" is a statement about what
a cluster obeying the body would return, not a restatement of the body. The live twin is
``tests/integration/test_quarantine_index_lifecycle_opensearch.py``.
"""

from __future__ import annotations

import contextlib
import json
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.core.config import settings
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.takedown_service import TakedownActor
from app.services.takedown_service import quarantine_file
from app.services.takedown_service import release_file
from tests.fixtures.opensearch_filter_eval import matches

SPEAKERS = "speakers_q_v4"


# --- an in-memory cluster that obeys the bodies ------------------------------------------------


class InMemoryCluster:
    """Holds documents per index; answers ``search`` (plain or kNN) and ``update_by_query``."""

    def __init__(self, indices: dict[str, list[dict[str, Any]]]) -> None:
        self.docs = indices
        self.bodies: list[dict[str, Any]] = []
        outer = self

        class _Indices:
            def exists(self, index: str) -> bool:
                return index in outer.docs

        self.indices = _Indices()

    def search(self, index: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.bodies.append(body)
        query = body.get("query", {"match_all": {}})
        if "knn" in query:
            ((_field, knn),) = query["knn"].items()
            query = knn.get("filter", {"match_all": {}})
        hits = [
            {"_score": 1.0, "_source": dict(doc)}
            for doc in self.docs.get(index, [])
            if matches(doc, query)
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits[: body.get("size", 10)]}}

    def update_by_query(self, index: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        value = body["script"]["params"]["value"]
        updated = 0
        for doc in self.docs.get(index, []):
            if matches(doc, body["query"]):
                doc["availability"] = value
                updated += 1
        return {"updated": updated, "version_conflicts": 0, "failures": []}


# --- fixtures --------------------------------------------------------------------------------


@pytest.fixture
def bridged(monkeypatch, db_session):
    """Services open their own sessions; under the savepoint harness they must see this one."""

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    return db_session


def _user(db, *, admin: bool = False) -> User:
    user = User(
        email=f"qidx_{uuid_pkg.uuid4().hex[:10]}@example.com",
        full_name="Quarantine index test user",
        hashed_password="x",
        is_active=True,
        is_superuser=admin,
        role="super_admin" if admin else "user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _file(db, owner: User) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        filename=f"qidx_{file_uuid.hex[:8]}.wav",
        storage_path="",  # no object: the best-effort storage legal hold is a no-op
        content_type="audio/wav",
        file_size=1,
        user_id=owner.id,
        status=FileStatus.COMPLETED,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _voiceprint(owner: User, media_file: MediaFile) -> dict[str, Any]:
    """A per-file voiceprint as written before the field existed: no ``availability``."""
    return {
        "speaker_id": 900_000 + int(media_file.id) % 1000,
        "speaker_uuid": str(uuid_pkg.uuid4()),
        "user_id": owner.id,
        "name": "Taken Down Voice",
        "collection_ids": [],
        "media_file_id": media_file.id,
        "segment_count": 1,
    }


@pytest.fixture
def cluster(monkeypatch) -> InMemoryCluster:
    """The speaker index only; the stamp skips the transcript indices this cluster lacks."""
    from app.services.opensearch_service import client as os_client
    from app.services.opensearch_service import matching

    fake = InMemoryCluster({SPEAKERS: []})
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", "chunks_q_absent")
    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", "transcripts_q_absent")
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: fake)
    monkeypatch.setattr(
        "app.services.opensearch_service.speaker_maintenance.speaker_embedding_indices",
        lambda: [SPEAKERS],
    )
    monkeypatch.setattr(matching, "ensure_indices_exist", lambda: None)
    monkeypatch.setattr(matching, "get_active_speaker_index", lambda: SPEAKERS)
    monkeypatch.setattr(matching, "query_embedding_dimension_mismatch", lambda *_a: False)
    return fake


def _match(owner: User) -> dict[str, Any] | None:
    from app.services.opensearch_service.matching import find_matching_speaker

    return find_matching_speaker([0.1] * 8, owner.id, threshold=0.5, organization_id=None)


def _quarantine(db, media_file: MediaFile, admin: User) -> None:
    quarantine_file(
        db,
        media_file,
        actor=TakedownActor.platform(admin),
        reason="takedown (test)",
        legal_hold=False,
        operator_action_reason="operator action (test)",
    )


# --- the speaker-matching gap ----------------------------------------------------------------


def test_a_quarantined_files_voice_is_not_matched_and_release_brings_it_back(bridged, cluster):
    """The gap: before this change a taken-down file's voiceprint was still a kNN neighbour,
    so speaker matching kept linking and suggesting it."""
    owner, admin = _user(bridged), _user(bridged, admin=True)
    media_file = _file(bridged, owner)
    cluster.docs[SPEAKERS].append(_voiceprint(owner, media_file))

    assert _match(owner) is not None, "control: the voice matches before the takedown"

    _quarantine(bridged, media_file, admin)
    assert _match(owner) is None, "a quarantined file's voice was still matched"
    assert cluster.docs[SPEAKERS][0]["availability"] == "quarantined"

    release_file(bridged, media_file, actor=TakedownActor.platform(admin), reason="released")
    assert cluster.docs[SPEAKERS][0]["availability"] == "available"
    assert _match(owner) is not None, "a released file's voice did not come back"


def test_the_speaker_to_speaker_match_body_drops_a_quarantined_voice(db_session):
    """``build_speaker_match_query`` feeds the msearch matcher and the cluster suggestions."""
    from app.services.speaker_matching_service import SpeakerMatchingService

    body = SpeakerMatchingService(db_session, None)._build_speaker_match_query(
        np.zeros(4), 7, 1, None
    )
    knn_filter = body["query"]["knn"]["embedding"]["filter"]
    voice = {"user_id": 7, "speaker_id": 2}

    assert matches({**voice, "availability": "available"}, knn_filter)
    assert matches(voice, knn_filter), "a voiceprint with no value (pre-upgrade) stays matchable"
    assert not matches({**voice, "availability": "quarantined"}, knn_filter)
    assert not matches({**voice, "availability": "trashed"}, knn_filter)


# --- the value the index carries --------------------------------------------------------------


def test_a_trashed_and_quarantined_file_reads_trashed(bridged):
    """The trash hides from everyone, the quarantine only from those the review exemption does
    not reach, so a file in both states is stamped with the stronger one; restoring it from
    the trash leaves it quarantined, and releasing it while trashed leaves it trashed."""
    from app.services.file_availability import index_availability
    from app.services.file_availability import index_availability_by_id

    owner = _user(bridged)
    media_file = _file(bridged, owner)
    assert index_availability(media_file) == "available"

    media_file.is_quarantined = True
    bridged.flush()
    assert index_availability(media_file) == "quarantined"
    assert index_availability_by_id(bridged, [media_file.id]) == {media_file.id: "quarantined"}

    media_file.deleted_at = datetime.now(UTC)
    media_file.purge_after = datetime.now(UTC) + timedelta(days=30)
    media_file.trash_origin = "user"
    bridged.flush()
    assert index_availability(media_file) == "trashed"
    assert index_availability_by_id(bridged, [media_file.id]) == {media_file.id: "trashed"}


def test_the_stamp_rewrites_only_documents_not_already_quarantined(monkeypatch):
    from app.services.search import availability_stamp

    calls: list[dict[str, Any]] = []

    def _update_by_query(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"updated": 2, "version_conflicts": 0, "failures": []}

    recording = SimpleNamespace(
        indices=SimpleNamespace(exists=lambda index: index == "chunks_r"),
        update_by_query=_update_by_query,
    )
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", "chunks_r")
    monkeypatch.setattr(
        "app.services.opensearch_service.speaker_maintenance.speaker_embedding_indices",
        list,
    )

    outcome = availability_stamp.stamp_files([4], "quarantined", client=recording)

    assert outcome.complete
    (call,) = calls
    assert call["body"] == {
        "query": {
            "bool": {
                "filter": [{"terms": {"file_id": [4]}}],
                "must_not": [{"term": {"availability": "quarantined"}}],
            }
        },
        "script": {
            "source": "ctx._source.availability = params.value",
            "lang": "painless",
            "params": {"value": "quarantined"},
        },
    }


# --- the transcript-plane readers -------------------------------------------------------------


def test_the_acl_clause_excludes_quarantined_unless_the_review_exemption_applies():
    from app.services.search.access_filter import access_filter_clause

    doc = {"accessible_user_ids": [7]}
    plain = access_filter_clause(7, None)
    reviewer = access_filter_clause(7, None, include_quarantined=True)

    assert matches(doc, plain) and matches(doc, reviewer)
    assert not matches({**doc, "availability": "quarantined"}, plain)
    assert matches({**doc, "availability": "quarantined"}, reviewer)
    assert not matches({**doc, "availability": "trashed"}, plain)
    assert not matches({**doc, "availability": "trashed"}, reviewer), "the trash has no exemption"


class _AggregationCluster(InMemoryCluster):
    """Records the facet body and answers with no buckets."""

    def search(self, index: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.bodies.append(body)
        return {"aggregations": {}}


def _facet_body(monkeypatch, *, quarantined: int, include_quarantined: bool) -> tuple[str, int]:
    """The facet aggregation body with Postgres holding ``quarantined`` taken-down files, and
    how many times building it opened a database session."""
    from app.services.search import search_infra
    from app.services.search import suggestions

    opened = 0

    class _Rows:
        def __getattr__(self, _name: str) -> Any:
            return lambda *_a, **_k: self

        def all(self) -> list[tuple[str]]:
            return [(f"{i:08d}-0000-0000-0000-000000000000",) for i in range(quarantined)]

    @contextlib.contextmanager
    def _scope():
        nonlocal opened
        opened += 1
        yield _Rows()

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    fake = _AggregationCluster({settings.OPENSEARCH_CHUNKS_INDEX: []})
    monkeypatch.setattr(suggestions._client, "opensearch_client", fake)
    monkeypatch.setattr(search_infra, "_index_verified", True)

    suggestions.get_available_filters(user_id=7, is_admin=include_quarantined)

    (body,) = fake.bodies
    return json.dumps(body, sort_keys=True), opened


@pytest.mark.parametrize("include_quarantined", [False, True])
def test_the_read_body_is_the_same_for_0_and_10001_quarantined_files(
    monkeypatch, include_quarantined
):
    """No per-file id list: the quarantine is a field, so past 10,000 taken-down files the
    facets still answer, with a body identical to the empty one and no database read."""
    empty, empty_opened = _facet_body(
        monkeypatch, quarantined=0, include_quarantined=include_quarantined
    )
    large, large_opened = _facet_body(
        monkeypatch, quarantined=10_001, include_quarantined=include_quarantined
    )

    assert empty_opened == large_opened == 0
    assert large == empty
    assert "file_uuid" not in large
    assert ('"quarantined"' in large) is not include_quarantined


def test_the_boot_dispatch_is_an_unscoped_reconcile_and_an_empty_scope_sends_nothing(monkeypatch):
    """The once-per-boot run is what stamps takedowns older than the field: it must reach the
    task unscoped (``file_ids=None``), while a scoped dispatch with no ids is a no-op."""
    from app.core.celery import celery_app
    from app.services.search import availability_stamp

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda name, **kw: sent.append({"name": name, **kw})
    )

    availability_stamp.dispatch_reconcile([])
    availability_stamp.dispatch_reconcile(None)

    assert [(s["name"], s["kwargs"]) for s in sent] == [
        ("search.availability_reconcile", {"file_ids": None})
    ]
