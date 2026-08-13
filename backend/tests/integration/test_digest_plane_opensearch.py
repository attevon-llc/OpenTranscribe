"""The digest plane, against a real OpenSearch and a real database (#403 Stage 3).

Index v6 puts a second kind of document — the extractive digest, one per section —
into the same index as transcript chunks. Everything that made that safe is a
property of two systems talking to each other, so nothing here uses a stand-in:

* **G1** — every rebuild trigger routes through ``reindex_transcript``, whose delete
  is unqualified. A rebuild that regenerated chunks and not digests would destroy
  the digest tier permanently, and no unit test can prove the round trip.
* **G5** — a share revocation rewrites ``accessible_user_ids`` keyed on ``file_id``.
  A digest missing that field keeps whatever ACL it was last stamped with and stays
  retrievable by a user who has just lost access. That is a permission leak, and it
  is invisible unless a real ``update_by_query`` runs against a real document.
* **#400, one plane over** — the id embeds the section number, so a digest that
  re-sections shorter orphans the extras exactly the way a shorter re-chunk orphaned
  a chunk tail.
* **G6** — a digest reaching the search path would be displayed as if a speaker had
  said it, with neither of the two read-time masking treatments applied.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 MINIO_PORT=5278 \\
        pytest backend/tests/integration/test_digest_plane_opensearch.py -m integration
"""

from __future__ import annotations

import os
import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from app.services.ingest_artifacts import index_mapping as digest_mapping

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT — a stand-in index cannot validate update_by_query or "
            "delete_by_query semantics."
        ),
    ),
]

# Long enough that the digest builder's MIN_SENTENCE_WORDS keeps them, varied enough
# that TextRank has something to rank, and enough of them to produce >1 section.
_SCRIPT = [
    ("SPEAKER_00", "Let us start with the quarterly budget review for the new product line."),
    (
        "SPEAKER_01",
        "I disagree with cutting marketing now because the launch is six weeks away and we "
        "still need the awareness campaign running through the whole period.",
    ),
    (
        "SPEAKER_02",
        "The engineering timeline slipped again so the launch date is probably moving to "
        "November regardless of what we decide about the marketing budget today.",
    ),
    ("SPEAKER_00", "If the launch moves to November the whole spending question changes."),
    (
        "SPEAKER_01",
        "We should hold the marketing budget flat until engineering confirms the November "
        "date in writing, and then revisit the whole campaign plan together.",
    ),
]


@pytest.fixture
def transcribed_file(db_session):
    """A real ``media_file`` with real segments — the digest is built from these."""
    user = User(
        email=f"digest_{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db_session.add(user)
    db_session.flush()

    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="digest.wav",
        storage_path="x/digest.wav",
        file_size=1,
        content_type="audio/wav",
        duration=480.0,
        language="en",
        title="Quarterly planning — product line",
    )
    db_session.add(media_file)
    db_session.flush()

    speakers = {}
    for label in ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02"):
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(), name=label, user_id=user.id, media_file_id=media_file.id
        )
        db_session.add(speaker)
        speakers[label] = speaker
    db_session.flush()

    clock = 0.0
    for _ in range(6):
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
    db_session.flush()
    return media_file, user


@pytest.fixture
def chunk_index(monkeypatch, db_session):
    """A throwaway v6 index, with the service's own session pointed at the test one.

    ``_index_digest_plane`` deliberately opens its **own** session (its callers are a
    Celery task holding one over a batch and an API path holding none), which under
    the savepoint harness cannot see this test's uncommitted rows. Patching
    ``session_scope`` is the same fix ``test_chat_endpoints.py`` uses, and without it
    the digest is built from an empty transcript and every assertion here passes
    vacuously against zero sections.
    """
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

    name = f"test_digest_plane_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])
        svc.reset_neural_pipeline_state()


def _segments_for(media_file, db_session) -> list[dict[str, Any]]:
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


def _index(media_file, db_session, *, accessible_user_ids: list[int] | None = None):
    from app.services.search.indexing_service import TranscriptIndexingService

    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=media_file.user_id,
        segments=_segments_for(media_file, db_session),
        title=media_file.title or "",
        speakers=["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
        tags=[],
        accessible_user_ids=accessible_user_ids,
    )
    assert isinstance(result, dict), f"indexing returned a failure sentinel: {result!r}"
    return result


def _plane(client, file_uuid: str, doc_type: str | None) -> list[dict[str, Any]]:
    from app.core.config import settings

    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    query: dict[str, Any] = {"bool": {"filter": [{"term": {"file_uuid": file_uuid}}]}}
    if doc_type is not None:
        query["bool"]["filter"].append({"term": {"doc_type": doc_type}})
    response = client.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={"size": 200, "query": query},
    )
    return [{**hit["_source"], "_id": hit["_id"]} for hit in response["hits"]["hits"]]


def test_indexing_a_transcript_writes_both_planes(chunk_index, transcribed_file, db_session):
    media_file, _ = transcribed_file
    result = _index(media_file, db_session)
    file_uuid = str(media_file.uuid)

    chunks = _plane(chunk_index, file_uuid, digest_mapping.DOC_TYPE_CHUNK)
    digests = _plane(chunk_index, file_uuid, digest_mapping.DOC_TYPE_DIGEST)

    assert chunks, "no chunk-plane documents were written"
    assert digests, "no digest-plane documents were written — the tier does not exist"
    assert result["digest_sections"] == len(digests)
    assert {d["_id"] for d in digests} == {
        digest_mapping.digest_document_id(file_uuid, int(d["digest_section"])) for d in digests
    }
    assert all(int(d["chunk_index"]) < 0 for d in digests), (
        "digest documents need the negative chunk_index sentinel — index.sort.field "
        "includes chunk_index, and 0 is a real chunk"
    )
    assert not ({c["_id"] for c in chunks} & {d["_id"] for d in digests})


def test_a_digest_document_carries_both_identifiers(chunk_index, transcribed_file, db_session):
    """G5: the ACL rewrite keys on file_id, the tenant backfill on file_uuid."""
    media_file, _ = transcribed_file
    _index(media_file, db_session)
    digests = _plane(chunk_index, str(media_file.uuid), digest_mapping.DOC_TYPE_DIGEST)

    assert digests
    for document in digests:
        assert int(document["file_id"]) == media_file.id
        assert document["file_uuid"] == str(media_file.uuid)


def test_share_revocation_reaches_digest_documents(chunk_index, transcribed_file, db_session):
    """A revoked share must not leave a readable digest behind.

    Drives the real ``update_by_query`` `update_file_access_index` issues, keyed on
    ``file_id``. The control is the assertion that the digest carried the *granted*
    id first: a digest that was never reachable would pass a revocation test for
    entirely the wrong reason.
    """
    from app.core.config import settings

    media_file, owner = transcribed_file
    guest_id = owner.id + 10_000
    _index(media_file, db_session, accessible_user_ids=[owner.id, guest_id])

    granted = _plane(chunk_index, str(media_file.uuid), digest_mapping.DOC_TYPE_DIGEST)
    assert granted, "no digest documents to revoke access to"
    assert all(guest_id in d["accessible_user_ids"] for d in granted), (
        "control failed: the guest never had access to the digest, so revoking it proves nothing"
    )

    chunk_index.update_by_query(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={
            "query": {"term": {"file_id": media_file.id}},
            "script": {
                "source": "ctx._source.accessible_user_ids = params.ids",
                "lang": "painless",
                "params": {"ids": [owner.id]},
            },
        },
        refresh=True,
        conflicts="proceed",
    )

    after = _plane(chunk_index, str(media_file.uuid), digest_mapping.DOC_TYPE_DIGEST)
    assert after
    for document in after:
        assert guest_id not in document["accessible_user_ids"], (
            "a digest survived a share revocation with the revoked user still on its ACL"
        )


def test_a_full_reindex_preserves_and_regenerates_the_digest_plane(
    chunk_index, transcribed_file, db_session
):
    """G1, end to end. The delete is unqualified; the rebuild must put both back."""
    from app.services.search.indexing_service import TranscriptIndexingService

    media_file, _ = transcribed_file
    service = TranscriptIndexingService()
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)
    before = _plane(chunk_index, file_uuid, digest_mapping.DOC_TYPE_DIGEST)
    assert before

    service.reindex_transcript(
        file_id=media_file.id,
        file_uuid=file_uuid,
        user_id=media_file.user_id,
        segments=_segments_for(media_file, db_session),
        title=media_file.title or "",
        speakers=["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
        tags=[],
    )

    after = _plane(chunk_index, file_uuid, digest_mapping.DOC_TYPE_DIGEST)
    assert after, "the rebuild destroyed the digest plane and did not put it back"
    assert [d["content"] for d in sorted(after, key=lambda d: d["digest_section"])] == [
        d["content"] for d in sorted(before, key=lambda d: d["digest_section"])
    ], "the same transcript must regenerate the same digest — determinism is a Stage 3 gate"


def test_deleting_a_file_leaves_no_digest_behind(chunk_index, transcribed_file, db_session):
    from app.services.search.indexing_service import TranscriptIndexingService

    media_file, _ = transcribed_file
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)
    assert _plane(chunk_index, file_uuid, None), "nothing was indexed, so nothing is proven"

    TranscriptIndexingService().delete_transcript_chunks(file_uuid)

    assert _plane(chunk_index, file_uuid, None) == [], (
        "a document survived the per-file delete — a digest that outlives its recording "
        "is a readable summary of deleted content"
    )


def test_a_shorter_resection_leaves_no_orphan_digest(chunk_index, transcribed_file, db_session):
    """The #400 hazard, one plane over: the id embeds the section number."""
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService

    media_file, _ = transcribed_file
    service = TranscriptIndexingService()
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)
    sections = len(_plane(chunk_index, file_uuid, digest_mapping.DOC_TYPE_DIGEST))
    assert sections >= 2, "need a multi-section digest for a shrink to be observable"

    # Plant a section beyond what the digest will ever produce again, exactly as a
    # longer previous sectioning would have left it.
    orphan_index = sections + 3
    chunk_index.index(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        id=digest_mapping.digest_document_id(file_uuid, orphan_index),
        body={
            "file_id": media_file.id,
            "file_uuid": file_uuid,
            "doc_type": digest_mapping.DOC_TYPE_DIGEST,
            "chunk_index": digest_mapping.digest_chunk_index(orphan_index),
            "digest_section": orphan_index,
            "content": "stale section from a longer previous digest",
            "embedding_text": "stale section from a longer previous digest",
        },
        refresh=True,
    )

    service.reindex_transcript(
        file_id=media_file.id,
        file_uuid=file_uuid,
        user_id=media_file.user_id,
        segments=_segments_for(media_file, db_session),
        title=media_file.title or "",
        speakers=["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
        tags=[],
    )

    remaining = _plane(chunk_index, file_uuid, digest_mapping.DOC_TYPE_DIGEST)
    assert remaining
    assert max(int(d["digest_section"]) for d in remaining) < orphan_index, (
        "an orphan digest section survived the rebuild and still matches every query "
        "the file matches"
    )


def test_the_search_path_never_returns_a_digest(chunk_index, transcribed_file, db_session):
    """G6: derived text must not surface as if somebody had said it.

    The digest quotes the transcript verbatim, so a query drawn from it matches both
    planes — which is what makes the exclusion observable rather than tautological.
    """
    from app.services.search.hybrid_search_service import HybridSearchService

    media_file, owner = transcribed_file
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)
    digests = _plane(chunk_index, file_uuid, digest_mapping.DOC_TYPE_DIGEST)
    assert digests

    service = HybridSearchService()
    filters = service._build_filters(
        owner.id, None, None, None, None, file_uuid=file_uuid, organization_id=None
    )
    from app.core.config import settings

    response = chunk_index.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={
            "size": 200,
            "query": {
                "bool": {
                    "must": [{"match": {"content": "marketing budget engineering timeline"}}],
                    "filter": filters,
                }
            },
        },
    )
    hits = response["hits"]["hits"]
    assert hits, "the query matched nothing at all, so the exclusion is unproven"
    assert all(hit["_source"].get("doc_type") != digest_mapping.DOC_TYPE_DIGEST for hit in hits), (
        "a digest reached the search path, where it would render as a quote and receive "
        "neither of the two read-time masking treatments"
    )
