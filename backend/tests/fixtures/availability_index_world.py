"""A throwaway OpenSearch world for the index-availability lifecycle suites (#1101, #1253).

Shared by ``tests/integration/test_trash_index_lifecycle_opensearch.py`` and
``tests/integration/test_quarantine_index_lifecycle_opensearch.py``: throwaway chunks,
transcripts and speaker indices (random names, deleted in teardown, the real indices never
touched), ``session_scope`` bridged to the savepoint session, a file indexed on every plane the
way the indexing task does it, and the list of index-backed read surfaces a file can appear on.
Opt-in (``availability_indices``); nothing autouse.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.user import User
from app.services.ingest_artifacts import index_mapping as digest_mapping
from tests.run_identity import RUN_TOKEN

MARK = RUN_TOKEN[:8]
SPEAKER = f"Zzyzx-{MARK}"
TAG = f"zzyzx-tag-{MARK}"
PHRASE = "quarterly budget review"
SUMMARY = {"bluf": f"The {PHRASE} cuts travel costs by twenty percent ({MARK})."}


@pytest.fixture
def availability_indices(monkeypatch, db_session):
    """Throwaway chunks, transcripts and speaker indices, and ``session_scope`` bridged.

    The services open their own sessions; under the savepoint harness a separate connection
    cannot see this test's rows, so without the bridge the stamp would read no rows and the
    tests would pass vacuously.
    """
    from app.core.config import settings
    from app.core.embedding_models import PYANNOTE_EMBEDDING_DIMENSION_V3
    from app.core.embedding_models import PYANNOTE_EMBEDDING_DIMENSION_V4
    from app.core.embedding_models import get_speaker_index
    from app.core.embedding_models import get_speaker_index_v3
    from app.core.embedding_models import get_speaker_index_v4
    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service import indices as os_indices
    from app.services.opensearch_service.aliases import invalidate_active_speaker_index_cache
    from app.services.search import index_admin
    from app.services.search import pipelines

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    @contextlib.contextmanager
    def _test_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)

    base = f"test_trash_lifecycle_{uuid_pkg.uuid4().hex[:10]}"
    chunks, transcripts = f"{base}_chunks", f"{base}_transcripts"
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", chunks)
    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", transcripts)
    monkeypatch.setattr(settings, "OPENSEARCH_SPEAKER_INDEX", f"{base}_speakers")
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    monkeypatch.setattr(os_indices, "_availability_mapping_checked", set())
    monkeypatch.setattr(os_indices, "_tenant_mapping_checked", set())
    v3, v4, alias = get_speaker_index_v3(), get_speaker_index_v4(), get_speaker_index()
    created = [chunks, transcripts, v3, v4]
    try:
        client.indices.create(index=chunks, body=index_admin._get_index_body_with_dimension(384))
        client.indices.create(index=transcripts, body=os_indices.transcript_index_body())
        os_indices._ensure_versioned_speaker_index(v3, PYANNOTE_EMBEDDING_DIMENSION_V3)
        os_indices._ensure_versioned_speaker_index(v4, PYANNOTE_EMBEDDING_DIMENSION_V4)
        client.indices.put_alias(index=v4, name=alias)
        invalidate_active_speaker_index_cache()
        pipelines.reset_neural_pipeline_state()
        yield client
    finally:
        for name in created:
            client.indices.delete(index=name, ignore=[404])
        invalidate_active_speaker_index_cache()
        pipelines.reset_neural_pipeline_state()


def make_user(db_session, prefix: str = "trash_lifecycle") -> User:
    user = User(
        email=f"{prefix}_{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db_session.add(user)
    db_session.flush()
    return user


def make_media_file(db_session, user: User) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"lifecycle-{MARK}.wav",
        storage_path=f"x/{uuid_pkg.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        duration=24.0,
        language="en",
        title=f"Budget planning {MARK}",
        summary_data=dict(SUMMARY),
    )
    db_session.add(media_file)
    db_session.flush()
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(), name=SPEAKER, user_id=user.id, media_file_id=media_file.id
    )
    db_session.add(speaker)
    db_session.flush()
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=8.0,
            text=f"Let us start the {PHRASE} for the new product line {MARK}.",
        )
    )
    db_session.commit()
    return media_file


def index_file(client, db_session, media_file: MediaFile) -> None:
    """Index every plane the way the indexing task does, availability from the row."""
    from app.core.config import settings
    from app.services.file_availability import index_availability
    from app.services.opensearch_service import index_transcript
    from app.services.search.indexing_service import TranscriptIndexingService

    text_ = f"Let us start the {PHRASE} {MARK}."
    segments: list[dict[str, Any]] = [
        {
            "start": 0.0,
            "end": 8.0,
            "text": f"Let us start the {PHRASE} {MARK}.",
            "speaker": SPEAKER,
        }
    ]
    availability = index_availability(media_file)
    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=media_file.user_id,
        segments=segments,
        title=media_file.title or "",
        speakers=[SPEAKER],
        tags=[TAG],
        accessible_user_ids=[media_file.user_id],
        availability=availability,
    )
    assert result.get("chunk_count", 0) >= 1, f"setup indexed nothing: {result!r}"
    index_transcript(
        media_file.id,
        str(media_file.uuid),
        media_file.user_id,
        text_,
        [SPEAKER],
        media_file.title or "",
        organization_id=None,
        availability=availability,
    )
    # A digest document, cloned from the chunk so the digest leg has a deterministic target
    # (the extractive digest needs far more text than this fixture holds).
    chunk = docs_of(
        client, settings.OPENSEARCH_CHUNKS_INDEX, media_file, digest_mapping.DOC_TYPE_CHUNK
    )[0]
    digest = {
        **chunk,
        digest_mapping.DOC_TYPE_FIELD: digest_mapping.DOC_TYPE_DIGEST,
        "digest_section": 0,
    }
    client.index(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        id=f"{media_file.uuid}_test_digest_0",
        body=digest,
        refresh=True,
    )


def docs_of(
    client, index: str, media_file: MediaFile, doc_type: str | None = None
) -> list[dict[str, Any]]:
    client.indices.refresh(index=index)
    query: dict[str, Any] = {"bool": {"filter": [{"term": {"file_uuid": str(media_file.uuid)}}]}}
    if doc_type is not None:
        query["bool"]["filter"].append({"term": {digest_mapping.DOC_TYPE_FIELD: doc_type}})
    response = client.search(index=index, body={"size": 100, "query": query})
    return [hit["_source"] for hit in response["hits"]["hits"]]


def visible_surfaces(user: User, media_file: MediaFile) -> dict[str, bool]:
    """Whether ``media_file`` shows up on each index-backed read surface, for ``user``."""
    from app.services.search.chunk_retrieval import retrieve_chunks
    from app.services.search.chunk_retrieval import retrieve_digests
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.summary_plane_search import search_summary_plane

    file_uuid = str(media_file.uuid)
    service = HybridSearchService()
    hits = service.search(query=PHRASE, user_id=user.id, search_mode="keyword")
    facets = service.get_available_filters(user_id=user.id, is_admin=False)
    suggestions = service.get_suggestions(prefix="Budget planning", user_id=user.id)
    summary_hits, _ = search_summary_plane(service, PHRASE, user.id)
    return {
        "search hits": any(h.file_uuid == file_uuid for h in hits.results),
        "search total": hits.total_results > 0,
        "find-bar count": service.count_matches(query=PHRASE, user_id=user.id) > 0,
        "speaker facet": SPEAKER in [s["name"] for s in facets["speakers"]],
        "tag facet": TAG in [t["name"] for t in facets["tags"]],
        "suggestions": any(MARK in s.get("text", "") for s in suggestions),
        "summary plane": any(h["file_uuid"] == file_uuid for h in summary_hits),
        "chat chunks": any(
            c.file_uuid == file_uuid
            for c in retrieve_chunks(PHRASE, user_id=user.id, search_mode="keyword")
        ),
        "chat digests": any(
            d.file_uuid == file_uuid
            for d in retrieve_digests(PHRASE, user_id=user.id, search_mode="keyword")
        ),
    }
