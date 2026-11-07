"""A trashed file leaves search, chat retrieval and speaker matching, and comes back (#1101).

Against a real OpenSearch and a real database, because the thing under test is two systems
agreeing: ``trash_service`` commits the row, then rewrites the file's documents in place
(``availability_stamp``: ``update_by_query`` with ``refresh=True``), and every reader excludes
``availability: trashed`` inside the query itself. A stand-in index cannot prove that the
``must_not`` excludes the documents from hits, ``total``, facets and kNN neighbours, or that a
document with NO ``availability`` value (written before the field existed) stays visible.

The readers are called at the SERVICE level, below the Postgres post-filter
(``file_availability.unavailable_among`` in ``search.py`` / ``chat/evidence.py``): with the
post-filter in front, a broken index filter would still pass. The one endpoint test is the
10,001-trashed-files property, which is about the endpoint not failing.

Every test creates its own users, files and throwaway indices (random names, deleted in
teardown) and never touches dev data or the real ``transcript_chunks`` / ``transcripts`` /
``speakers*`` indices. Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_trash_index_lifecycle_opensearch.py -m integration
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import insert

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.user import User
from app.services import trash_service
from app.services.ingest_artifacts import index_mapping as digest_mapping

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group("opensearch_speaker_indices"),
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT: a stand-in index cannot prove that update_by_query stamps the "
            "documents or that the must_not excludes them from hits, counts and kNN."
        ),
    ),
]

_MARK = uuid_pkg.uuid4().hex[:8]
_SPEAKER = f"Zzyzx-{_MARK}"
_TAG = f"zzyzx-tag-{_MARK}"
_PHRASE = "quarterly budget review"
_SUMMARY = {"bluf": f"The {_PHRASE} cuts travel costs by twenty percent ({_MARK})."}


@pytest.fixture
def indices(monkeypatch, db_session):
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


def _user(db_session, prefix: str = "trash_lifecycle") -> User:
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


def _media_file(db_session, user: User) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"lifecycle-{_MARK}.wav",
        storage_path=f"x/{uuid_pkg.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        duration=24.0,
        language="en",
        title=f"Budget planning {_MARK}",
        summary_data=dict(_SUMMARY),
    )
    db_session.add(media_file)
    db_session.flush()
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(), name=_SPEAKER, user_id=user.id, media_file_id=media_file.id
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
            text=f"Let us start the {_PHRASE} for the new product line {_MARK}.",
        )
    )
    db_session.commit()
    return media_file


def _index(client, db_session, media_file: MediaFile) -> None:
    """Index every plane the way the indexing task does, availability from the row."""
    from app.core.config import settings
    from app.services.file_availability import index_availability
    from app.services.opensearch_service import index_transcript
    from app.services.search.indexing_service import TranscriptIndexingService

    text_ = f"Let us start the {_PHRASE} {_MARK}."
    segments: list[dict[str, Any]] = [
        {
            "start": 0.0,
            "end": 8.0,
            "text": f"Let us start the {_PHRASE} {_MARK}.",
            "speaker": _SPEAKER,
        }
    ]
    availability = index_availability(media_file)
    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=media_file.user_id,
        segments=segments,
        title=media_file.title or "",
        speakers=[_SPEAKER],
        tags=[_TAG],
        accessible_user_ids=[media_file.user_id],
        availability=availability,
    )
    assert result.get("chunk_count", 0) >= 1, f"setup indexed nothing: {result!r}"
    index_transcript(
        media_file.id,
        str(media_file.uuid),
        media_file.user_id,
        text_,
        [_SPEAKER],
        media_file.title or "",
        organization_id=None,
        availability=availability,
    )
    # A digest document, cloned from the chunk so the digest leg has a deterministic target
    # (the extractive digest needs far more text than this fixture holds).
    chunk = _docs(
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


def _docs(
    client, index: str, media_file: MediaFile, doc_type: str | None = None
) -> list[dict[str, Any]]:
    client.indices.refresh(index=index)
    query: dict[str, Any] = {"bool": {"filter": [{"term": {"file_uuid": str(media_file.uuid)}}]}}
    if doc_type is not None:
        query["bool"]["filter"].append({"term": {digest_mapping.DOC_TYPE_FIELD: doc_type}})
    response = client.search(index=index, body={"size": 100, "query": query})
    return [hit["_source"] for hit in response["hits"]["hits"]]


def _visible(user: User, media_file: MediaFile) -> dict[str, bool]:
    """Whether ``media_file`` shows up on each index-backed read surface, for ``user``."""
    from app.services.search.chunk_retrieval import retrieve_chunks
    from app.services.search.chunk_retrieval import retrieve_digests
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.summary_plane_search import search_summary_plane

    file_uuid = str(media_file.uuid)
    service = HybridSearchService()
    hits = service.search(query=_PHRASE, user_id=user.id, search_mode="keyword")
    facets = service.get_available_filters(user_id=user.id, is_admin=False)
    suggestions = service.get_suggestions(prefix="Budget planning", user_id=user.id)
    summary_hits, _ = search_summary_plane(service, _PHRASE, user.id)
    return {
        "search hits": any(h.file_uuid == file_uuid for h in hits.results),
        "search total": hits.total_results > 0,
        "find-bar count": service.count_matches(query=_PHRASE, user_id=user.id) > 0,
        "speaker facet": _SPEAKER in [s["name"] for s in facets["speakers"]],
        "tag facet": _TAG in [t["name"] for t in facets["tags"]],
        "suggestions": any(_MARK in s.get("text", "") for s in suggestions),
        "summary plane": any(h["file_uuid"] == file_uuid for h in summary_hits),
        "chat chunks": any(
            c.file_uuid == file_uuid
            for c in retrieve_chunks(_PHRASE, user_id=user.id, search_mode="keyword")
        ),
        "chat digests": any(
            d.file_uuid == file_uuid
            for d in retrieve_digests(_PHRASE, user_id=user.id, search_mode="keyword")
        ),
    }


def test_a_trashed_file_leaves_every_index_read_surface_and_restore_brings_it_back(
    indices, db_session
):
    user = _user(db_session)
    media_file = _media_file(db_session, user)
    _index(indices, db_session, media_file)

    before = _visible(user, media_file)
    assert all(before.values()), f"control: the file must be visible before the trash: {before}"

    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)
    trashed = _visible(user, media_file)
    assert not any(trashed.values()), f"a trashed file is still visible on: {trashed}"

    trash_service.restore_files(db_session, [media_file], actor=user, request=None, bypass=None)
    after = _visible(user, media_file)
    assert all(after.values()), f"a restored file did not come back on: {after}"


def test_the_stamp_reaches_every_plane_and_index(indices, db_session):
    from app.core.config import settings

    user = _user(db_session)
    media_file = _media_file(db_session, user)
    _index(indices, db_session, media_file)

    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)

    chunk_docs = _docs(indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    planes = {d[digest_mapping.DOC_TYPE_FIELD] for d in chunk_docs}
    assert {"chunk", "digest", "summary"} <= planes, planes
    assert {d["availability"] for d in chunk_docs} == {"trashed"}
    transcript = indices.get(index=settings.OPENSEARCH_TRANSCRIPT_INDEX, id=str(media_file.uuid))
    assert transcript["_source"]["availability"] == "trashed"


def test_a_document_written_before_the_field_existed_stays_visible(indices, db_session):
    """No ``availability`` value reads as available: the deploy needs no reindex."""
    from app.core.config import settings
    from app.services.search.hybrid_search_service import HybridSearchService

    user = _user(db_session)
    media_file = _media_file(db_session, user)
    _index(indices, db_session, media_file)
    indices.update_by_query(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        refresh=True,
        body={
            "query": {"term": {"file_uuid": str(media_file.uuid)}},
            "script": {"source": "ctx._source.remove('availability')", "lang": "painless"},
        },
    )
    assert all(
        "availability" not in d
        for d in _docs(indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    )

    hits = HybridSearchService().search(query=_PHRASE, user_id=user.id, search_mode="keyword")

    assert any(h.file_uuid == str(media_file.uuid) for h in hits.results)


def test_a_reindex_of_a_trashed_file_writes_trashed_documents(indices, db_session):
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService
    from app.tasks.reindex_task import _extract_file_metadata

    user = _user(db_session)
    media_file = _media_file(db_session, user)
    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)
    db_session.refresh(media_file)

    metadata = _extract_file_metadata(db_session, media_file)
    assert metadata is not None
    TranscriptIndexingService().reindex_transcript(
        file_id=metadata["file_id"],
        file_uuid=metadata["file_uuid"],
        user_id=user.id,
        segments=metadata["segments"],
        title=metadata["title"],
        speakers=metadata["speakers"],
        tags=metadata["tags"],
        accessible_user_ids=metadata["accessible_user_ids"],
        availability=metadata["availability"],
    )

    docs = _docs(indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    assert docs and {d["availability"] for d in docs} == {"trashed"}


def test_a_trashed_files_voice_is_not_matched_and_comes_back_on_restore(indices, db_session):
    from app.core.embedding_models import PYANNOTE_EMBEDDING_DIMENSION_V4
    from app.core.embedding_models import get_speaker_index_v4
    from app.services.opensearch_service.matching import find_matching_speaker

    user = _user(db_session)
    media_file = _media_file(db_session, user)
    vector = [((i % 7) + 1) / 10.0 for i in range(PYANNOTE_EMBEDDING_DIMENSION_V4)]
    # Written without an availability value, the shape of a voiceprint from before the field.
    indices.index(
        index=get_speaker_index_v4(),
        id=str(uuid_pkg.uuid4()),
        refresh=True,
        body={
            "speaker_id": 999_001,
            "speaker_uuid": str(uuid_pkg.uuid4()),
            "user_id": user.id,
            "name": _SPEAKER,
            "collection_ids": [],
            "media_file_id": media_file.id,
            "segment_count": 1,
            "embedding": vector,
        },
    )

    def _match():
        return find_matching_speaker(vector, user.id, threshold=0.5, organization_id=None)

    assert _match() is not None, "control: the voice must match before the trash"
    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)
    assert _match() is None, "a trashed file's voice still matched"
    trash_service.restore_files(db_session, [media_file], actor=user, request=None, bypass=None)
    assert _match() is not None, "a restored file's voice did not come back"


def test_the_orphan_sweep_keeps_a_trashed_files_documents(indices, db_session):
    from app.core.config import settings
    from app.tasks import opensearch_integrity_task as integrity

    user = _user(db_session)
    media_file = _media_file(db_session, user)
    _index(indices, db_session, media_file)
    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)

    # The sweep's own core, for real (dry_run=False), scoped to this test's throwaway index:
    # the full run_orphan_cleanup would also scan indices this test does not own.
    stats = integrity._cleanup_index_by_field(
        indices,
        settings.OPENSEARCH_CHUNKS_INDEX,
        "file_uuid",
        integrity._get_all_file_uuids_from_db(),
        str,
        dry_run=False,
        still_present=integrity._file_uuids_present,
    )

    assert stats["orphaned_docs"] == 0, stats
    assert _docs(indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file), (
        "the integrity sweep deleted a trashed (restorable) file's documents"
    )


def test_a_failed_stamp_converges_through_the_reconcile(indices, db_session, monkeypatch):
    from app.core.config import settings
    from app.services.search import availability_stamp
    from app.tasks import search_availability_task

    user = _user(db_session)
    media_file = _media_file(db_session, user)
    _index(indices, db_session, media_file)
    dispatched: list[list[int]] = []
    real_stamp = availability_stamp.stamp_files

    def _down(*_a, **_k):
        raise ConnectionError("opensearch down for this one call")

    monkeypatch.setattr(availability_stamp, "stamp_files", _down)
    monkeypatch.setattr(availability_stamp, "dispatch_reconcile", dispatched.append)
    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)
    assert dispatched == [[media_file.id]]
    assert {
        d.get("availability") for d in _docs(indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    } == {"available"}

    monkeypatch.setattr(availability_stamp, "stamp_files", real_stamp)
    search_availability_task.availability_reconcile.run(file_ids=dispatched[0])

    docs = _docs(indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    assert {d["availability"] for d in docs} == {"trashed"}


def test_search_still_answers_with_10001_trashed_files(
    indices, db_session, client, normal_user, user_token_headers
):
    """The regression the quarantine-list approach would cause: past 10,000 trashed files a
    Postgres-resolved ``must_not terms`` set fails closed with a 503. The trash is a field, so
    the search answers 200 and finds the live file."""
    live = _media_file(db_session, normal_user)
    _index(indices, db_session, live)
    now = datetime.now(UTC)
    db_session.execute(
        insert(MediaFile),
        [
            {
                "uuid": uuid_pkg.uuid4(),
                "user_id": normal_user.id,
                "filename": f"bulk-trash-{i}.wav",
                "storage_path": f"x/bulk/{_MARK}/{i}",
                "file_size": 1,
                "content_type": "audio/wav",
                "deleted_at": now,
                "purge_after": now + timedelta(days=30),
                "trash_origin": "user",
                "deleted_by_user_id": normal_user.id,
            }
            for i in range(10_001)
        ],
    )
    db_session.flush()

    response = client.get(
        "/api/search",
        params={"q": _PHRASE, "search_mode": "keyword"},
        headers=user_token_headers,
    )

    assert response.status_code == 200, response.text
    assert str(live.uuid) in [r["file_uuid"] for r in response.json()["results"]]
