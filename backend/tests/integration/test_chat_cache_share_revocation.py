"""Share-revocation cache invalidation, against a real OpenSearch, Postgres, and Redis.

Mirrors ``test_digest_plane_opensearch.py::test_share_revocation_reaches_digest_documents``
(G5): a revoked collection share must not leave a user with continued, indirect access to
a file's content. That test proves the ACL rewrite lands in OpenSearch; this one proves the
*chat retrieval cache* — a second, ACL-blind layer sitting in front of the same index — does
not keep serving the revoked chunks out of Redis for the rest of the cache TTL.

Requires a real Redis in addition to OpenSearch/Postgres because the mandatory
must-fire control (no revocation → cache_hit is True) only means something against a
cache that actually stores and serves hits.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 MINIO_PORT=5278 REDIS_PORT=5277 \\
        pytest backend/tests/integration/test_chat_cache_share_revocation.py -m integration
"""

from __future__ import annotations

import os
import uuid as uuid_pkg

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.sharing import CollectionShare
from app.services.chat.retrieval import retrieve_context
from app.services.chat.settings import ChatSettings

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"


def _redis_reachable() -> bool:
    try:
        from app.core.redis import get_redis

        return bool(get_redis().ping())
    except Exception:
        return False


_REDIS_ABSENT = not _redis_reachable()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT — a stand-in index cannot validate update_by_query semantics."
        ),
    ),
    pytest.mark.skipif(
        _REDIS_ABSENT,
        reason=(
            "No Redis reachable. Start an isolated stack and export REDIS_PORT — the cache "
            "under test IS Redis, so a stand-in proves nothing."
        ),
    ),
]

_SCRIPT = [
    ("SPEAKER_00", "The Medicare fraud findings were reviewed by the compliance team."),
    ("SPEAKER_01", "Investigators traced the billing discrepancies to three clinics."),
    ("SPEAKER_00", "The final report on the Medicare fraud findings goes to the board next week."),
]


@pytest.fixture
def owner_and_guest(db_session, normal_user, other_user):
    """``normal_user`` owns the file; ``other_user`` is granted, then revoked, access."""
    return normal_user, other_user


@pytest.fixture
def shared_file(db_session, owner_and_guest):
    owner, _guest = owner_and_guest
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        filename="medicare-fraud.wav",
        storage_path="x/medicare-fraud.wav",
        file_size=1,
        content_type="audio/wav",
        duration=60.0,
        language="en",
        title="Medicare fraud findings review",
    )
    db_session.add(media_file)
    db_session.flush()

    speakers = {}
    for label in ("SPEAKER_00", "SPEAKER_01"):
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(), name=label, user_id=owner.id, media_file_id=media_file.id
        )
        db_session.add(speaker)
        speakers[label] = speaker
    db_session.flush()

    clock = 0.0
    for label, text in _SCRIPT:
        db_session.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                speaker_id=speakers[label].id,
                start_time=clock,
                end_time=clock + 8.0,
                text=text,
            )
        )
        clock += 8.0
    db_session.commit()
    return media_file


@pytest.fixture
def chunk_index(monkeypatch, db_session):
    """A throwaway index, real session wiring — same pattern as the digest-plane suite."""
    import contextlib

    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    @contextlib.contextmanager
    def _test_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)

    name = f"test_chat_cache_revoke_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])
        svc.reset_neural_pipeline_state()


def _segments_for(media_file, db_session):
    from app.services.ingest_artifacts.service import load_ordered_segments

    return [
        {
            "start": segment["start_time"],
            "end": segment["end_time"],
            "text": segment["text"],
            "speaker": segment["speaker"],
        }
        for segment in load_ordered_segments(db_session, media_file.id)
    ]


def _index_owner_only(media_file, owner, db_session, chunk_index):
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService

    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=media_file.user_id,
        segments=_segments_for(media_file, db_session),
        title=media_file.title or "",
        speakers=["SPEAKER_00", "SPEAKER_01"],
        tags=[],
        accessible_user_ids=[owner.id],
    )
    assert isinstance(result, dict), f"indexing returned a failure sentinel: {result!r}"
    # index_transcript_chunks does not itself refresh; update_file_access_index's
    # update_by_query (below) and retrieve_context's search both need the bulk
    # write visible first.
    chunk_index.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    return result


def _grant_and_run_index_update(media_file, owner, guest, db_session):
    """Real collection-share grant, then the real ``update_file_access_index`` call."""
    from app.tasks.search_indexing_task import update_file_access_index

    collection = Collection(name=f"revoke-repro-{uuid_pkg.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(collection)
    db_session.flush()
    member = CollectionMember(collection_id=collection.id, media_file_id=media_file.id)
    db_session.add(member)
    db_session.flush()
    share = CollectionShare(
        collection_id=collection.id,
        shared_by_id=owner.id,
        target_type="user",
        target_user_id=guest.id,
    )
    db_session.add(share)
    db_session.commit()

    result = update_file_access_index([media_file.id])
    assert result["status"] == "success"
    return collection.id, share.id


def _revoke_and_run_index_update(share_id, media_file, db_session):
    from app.tasks.search_indexing_task import update_file_access_index

    db_session.query(CollectionShare).filter(CollectionShare.id == share_id).delete()
    db_session.commit()

    result = update_file_access_index([media_file.id])
    assert result["status"] == "success"


def _clear_chat_cache_keys_for(user_id):
    from app.core.redis import get_redis

    r = get_redis()
    keys = list(r.scan_iter(match=f"chat:retr:{user_id}:*"))
    if keys:
        r.delete(*keys)


def test_revoked_share_stops_reaching_chat_retrieval_cache(
    chunk_index, shared_file, owner_and_guest, db_session
):
    """The real bug: a cache entry written while shared must not survive a revoke.

    Includes the mandatory must-fire control (a second, non-revoking run) — without
    it, a broken cache that never hits at all would satisfy the main assertion for
    the wrong reason.
    """
    owner, guest = owner_and_guest
    settings = ChatSettings()

    def run_query():
        return retrieve_context(
            query="medicare fraud findings",
            user_id=guest.id,
            organization_id=None,
            file_uuids=None,
            settings=settings,
        )

    # --- main run: grant, cache, revoke, re-query ---
    _index_owner_only(shared_file, owner, db_session, chunk_index)
    _clear_chat_cache_keys_for(guest.id)
    collection_id, share_id = _grant_and_run_index_update(shared_file, owner, guest, db_session)

    first = run_query()
    assert first.cache_hit is False, "expected the first query to be a cache miss"
    assert str(shared_file.uuid) in {h.file_uuid for h in first.chunks}, (
        "control failed: the guest never had the file in their first result, so "
        "revoking access proves nothing"
    )

    _revoke_and_run_index_update(share_id, shared_file, db_session)

    second = run_query()
    assert not (
        second.cache_hit and str(shared_file.uuid) in {h.file_uuid for h in second.chunks}
    ), "a revoked share's chunks were served from a cache entry created before the revoke"

    _clear_chat_cache_keys_for(guest.id)
    db_session.query(CollectionMember).filter(
        CollectionMember.collection_id == collection_id
    ).delete()
    db_session.query(Collection).filter(Collection.id == collection_id).delete()
    db_session.commit()

    # --- must-fire control: identical flow, no revoke, must actually cache-hit ---
    _index_owner_only(shared_file, owner, db_session, chunk_index)
    collection_id2, _share_id2 = _grant_and_run_index_update(shared_file, owner, guest, db_session)

    control_first = run_query()
    assert control_first.cache_hit is False
    control_second = run_query()
    assert control_second.cache_hit is True, (
        "the cache never hit even without a revocation — this repro is vacuous"
    )
    assert str(shared_file.uuid) in {h.file_uuid for h in control_second.chunks}

    _clear_chat_cache_keys_for(guest.id)
    db_session.query(CollectionMember).filter(
        CollectionMember.collection_id == collection_id2
    ).delete()
    db_session.query(Collection).filter(Collection.id == collection_id2).delete()
    db_session.commit()
