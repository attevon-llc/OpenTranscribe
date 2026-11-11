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

import os
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import insert

from app.models.media import MediaFile
from app.services import trash_service
from app.services.ingest_artifacts import index_mapping as digest_mapping
from tests.fixtures.availability_index_world import MARK
from tests.fixtures.availability_index_world import PHRASE
from tests.fixtures.availability_index_world import SPEAKER
from tests.fixtures.availability_index_world import docs_of
from tests.fixtures.availability_index_world import index_file
from tests.fixtures.availability_index_world import make_media_file
from tests.fixtures.availability_index_world import make_user
from tests.fixtures.availability_index_world import visible_surfaces

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


def test_a_trashed_file_leaves_every_index_read_surface_and_restore_brings_it_back(
    availability_indices, db_session
):
    user = make_user(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)

    before = visible_surfaces(user, media_file)
    assert all(before.values()), f"control: the file must be visible before the trash: {before}"

    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)
    trashed = visible_surfaces(user, media_file)
    assert not any(trashed.values()), f"a trashed file is still visible on: {trashed}"

    trash_service.restore_files(db_session, [media_file], actor=user, request=None, bypass=None)
    after = visible_surfaces(user, media_file)
    assert all(after.values()), f"a restored file did not come back on: {after}"


def test_the_stamp_reaches_every_plane_and_index(availability_indices, db_session):
    from app.core.config import settings

    user = make_user(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)

    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)

    chunk_docs = docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    planes = {d[digest_mapping.DOC_TYPE_FIELD] for d in chunk_docs}
    assert {"chunk", "digest", "summary"} <= planes, planes
    assert {d["availability"] for d in chunk_docs} == {"trashed"}
    transcript = availability_indices.get(
        index=settings.OPENSEARCH_TRANSCRIPT_INDEX, id=str(media_file.uuid)
    )
    assert transcript["_source"]["availability"] == "trashed"


def test_a_document_written_before_the_field_existed_stays_visible(
    availability_indices, db_session
):
    """No ``availability`` value reads as available: the deploy needs no reindex."""
    from app.core.config import settings
    from app.services.search.hybrid_search_service import HybridSearchService

    user = make_user(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)
    availability_indices.update_by_query(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        refresh=True,
        body={
            "query": {"term": {"file_uuid": str(media_file.uuid)}},
            "script": {"source": "ctx._source.remove('availability')", "lang": "painless"},
        },
    )
    assert all(
        "availability" not in d
        for d in docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    )

    hits = HybridSearchService().search(query=PHRASE, user_id=user.id, search_mode="keyword")

    assert any(h.file_uuid == str(media_file.uuid) for h in hits.results)


def test_a_reindex_of_a_trashed_file_writes_trashed_documents(availability_indices, db_session):
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService
    from app.tasks.reindex_task import _extract_file_metadata

    user = make_user(db_session)
    media_file = make_media_file(db_session, user)
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

    docs = docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    assert docs and {d["availability"] for d in docs} == {"trashed"}


def test_a_trashed_files_voice_is_not_matched_and_comes_back_on_restore(
    availability_indices, db_session
):
    from app.core.embedding_models import PYANNOTE_EMBEDDING_DIMENSION_V4
    from app.core.embedding_models import get_speaker_index_v4
    from app.services.opensearch_service.matching import find_matching_speaker

    user = make_user(db_session)
    media_file = make_media_file(db_session, user)
    vector = [((i % 7) + 1) / 10.0 for i in range(PYANNOTE_EMBEDDING_DIMENSION_V4)]
    # Written without an availability value, the shape of a voiceprint from before the field.
    availability_indices.index(
        index=get_speaker_index_v4(),
        id=str(uuid_pkg.uuid4()),
        refresh=True,
        body={
            "speaker_id": 999_001,
            "speaker_uuid": str(uuid_pkg.uuid4()),
            "user_id": user.id,
            "name": SPEAKER,
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


def test_the_orphan_sweep_keeps_a_trashed_files_documents(availability_indices, db_session):
    from app.core.config import settings
    from app.tasks import opensearch_integrity_task as integrity

    user = make_user(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)
    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)

    # The sweep's own core, for real (dry_run=False), scoped to this test's throwaway index:
    # the full run_orphan_cleanup would also scan indices this test does not own.
    stats = integrity._cleanup_index_by_field(
        availability_indices,
        settings.OPENSEARCH_CHUNKS_INDEX,
        "file_uuid",
        integrity._get_all_file_uuids_from_db(),
        str,
        dry_run=False,
        still_present=integrity._file_uuids_present,
    )

    assert stats["orphaned_docs"] == 0, stats
    assert docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file), (
        "the integrity sweep deleted a trashed (restorable) file's documents"
    )


def test_a_failed_stamp_converges_through_the_reconcile(
    availability_indices, db_session, monkeypatch
):
    from app.core.config import settings
    from app.services.search import availability_stamp
    from app.tasks import search_availability_task

    user = make_user(db_session)
    media_file = make_media_file(db_session, user)
    index_file(availability_indices, db_session, media_file)
    dispatched: list[list[int]] = []
    real_stamp = availability_stamp.stamp_files

    def _down(*_a, **_k):
        raise ConnectionError("opensearch down for this one call")

    monkeypatch.setattr(availability_stamp, "stamp_files", _down)
    monkeypatch.setattr(availability_stamp, "dispatch_reconcile", dispatched.append)
    trash_service.trash_files(db_session, [media_file], actor=user, request=None, bypass=None)
    assert dispatched == [[media_file.id]]
    assert {
        d.get("availability")
        for d in docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    } == {"available"}

    monkeypatch.setattr(availability_stamp, "stamp_files", real_stamp)
    search_availability_task.availability_reconcile.run(file_ids=dispatched[0])

    docs = docs_of(availability_indices, settings.OPENSEARCH_CHUNKS_INDEX, media_file)
    assert {d["availability"] for d in docs} == {"trashed"}


def test_search_still_answers_with_10001_trashed_files(
    availability_indices, db_session, client, normal_user, user_token_headers
):
    """The regression the quarantine-list approach would cause: past 10,000 trashed files a
    Postgres-resolved ``must_not terms`` set fails closed with a 503. The trash is a field, so
    the search answers 200 and finds the live file."""
    live = make_media_file(db_session, normal_user)
    index_file(availability_indices, db_session, live)
    now = datetime.now(UTC)
    db_session.execute(
        insert(MediaFile),
        [
            {
                "uuid": uuid_pkg.uuid4(),
                "user_id": normal_user.id,
                "filename": f"bulk-trash-{i}.wav",
                "storage_path": f"x/bulk/{MARK}/{i}",
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
        params={"q": PHRASE, "search_mode": "keyword"},
        headers=user_token_headers,
    )

    assert response.status_code == 200, response.text
    assert str(live.uuid) in [r["file_uuid"] for r in response.json()["results"]]
