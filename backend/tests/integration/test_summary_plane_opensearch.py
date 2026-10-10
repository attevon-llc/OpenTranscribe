"""The summary plane, against a real OpenSearch and a real database (issue #963).

Modelled on ``test_digest_plane_opensearch.py`` — the same reasons apply, one plane
over:

* **G1** — every rebuild trigger routes through ``index_transcript_chunks`` /
  ``reindex_transcript``, whose delete (``file_plane_query``) is unqualified. A
  rebuild that regenerated chunks and digests but not summaries would destroy the
  summary tier permanently, and no unit test can prove the round trip.
* **G5** — a share revocation rewrites ``accessible_user_ids`` keyed on ``file_id``.
  A summary document missing that field keeps whatever ACL it was last stamped
  with and stays retrievable by a user who has just lost access — a permission
  leak, invisible unless a real ``update_by_query`` runs against a real document.
* **#67, structurally** — Postgres ``media_file.summary_data`` is canonical;
  OpenSearch is derived. ``_index_summary_plane`` reads the column itself, on its
  own session, and has no parameter a caller could hand it a payload through.
* **The semantic capability** (T-R1) — the retired Postgres FTS leg could never
  find a summary leaf with no lexical overlap with the query. This is the one
  capability that can only be proven against a real kNN query.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 MINIO_PORT=5278 \\
        pytest backend/tests/integration/test_summary_plane_opensearch.py -m integration
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

_SUMMARY_DATA = {
    "bluf": "The team will cut business-trip costs by 20% before the next quarter.",
    "major_topics": [
        {
            "topic": "Travel budget",
            "key_points": [
                "Reduce flight spend by booking economy for domestic trips.",
                "Consolidate hotel bookings through the corporate rate program.",
            ],
        }
    ],
}

_SCRIPT = [
    ("SPEAKER_00", "Let us start with the quarterly budget review for the new product line."),
    ("SPEAKER_01", "I disagree with cutting marketing now because the launch is six weeks away."),
    ("SPEAKER_00", "If the launch moves to November the whole spending question changes."),
]


@pytest.fixture
def summarized_file(db_session):
    """A real ``media_file`` with real segments AND a real ``summary_data`` blob."""
    user = User(
        email=f"summary_{uuid_pkg.uuid4().hex[:10]}@example.com",
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
        filename="summary.wav",
        storage_path="x/summary.wav",
        file_size=1,
        content_type="audio/wav",
        duration=180.0,
        language="en",
        title="Quarterly planning — product line",
        summary_data=dict(_SUMMARY_DATA),
    )
    db_session.add(media_file)
    db_session.flush()

    speakers = {}
    for label in ("SPEAKER_00", "SPEAKER_01"):
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(), name=label, user_id=user.id, media_file_id=media_file.id
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
    db_session.flush()

    return media_file, user


@pytest.fixture
def chunk_index(monkeypatch, db_session):
    """A throwaway v6 index, with the service's own session pointed at the test one.

    Never touches the shared ``transcript_chunks`` index — a random-uuid-named
    index is created and deleted around the test. Neural search is OFF: the
    write-path tests below don't need it, and it removes a dependency on the
    cluster's ML Commons state. ``test_semantic_retrieval_finds_a_summary_leaf_
    with_no_lexical_overlap`` below enables it explicitly and fails loudly
    (never skips) if the neural pipeline cannot be verified.
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

    name = f"test_summary_plane_{uuid_pkg.uuid4().hex[:12]}"
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
        speakers=["SPEAKER_00", "SPEAKER_01"],
        tags=[],
        accessible_user_ids=accessible_user_ids,
    )
    assert isinstance(result, dict), f"indexing returned a failure sentinel: {result!r}"
    return result


def _plane(client, index_name, file_uuid: str, doc_type: str | None) -> list[dict[str, Any]]:
    client.indices.refresh(index=index_name)
    query: dict[str, Any] = {"bool": {"filter": [{"term": {"file_uuid": file_uuid}}]}}
    if doc_type is not None:
        query["bool"]["filter"].append({"term": {"doc_type": doc_type}})
    response = client.search(index=index_name, body={"size": 200, "query": query})
    return [{**hit["_source"], "_id": hit["_id"]} for hit in response["hits"]["hits"]]


def _summary_plane(client, index_name, file_uuid: str) -> list[dict[str, Any]]:
    return _plane(client, index_name, file_uuid, digest_mapping.DOC_TYPE_SUMMARY)


def test_indexing_a_transcript_writes_all_three_planes(chunk_index, summarized_file, db_session):
    """The write-path integration proof of Edit 5: one call, three planes."""
    from app.core.config import settings

    media_file, _ = summarized_file
    result = _index(media_file, db_session)
    file_uuid = str(media_file.uuid)

    chunks = _plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid, "chunk")
    digests = _plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid, "digest")
    summaries = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid)

    assert chunks, "no chunk-plane documents were written"
    assert digests, "no digest-plane documents were written"
    assert summaries, "no summary-plane documents were written — the tier does not exist"
    assert result["summary_leaves"] == len(summaries)
    assert not ({c["_id"] for c in chunks} & {s["_id"] for s in summaries})
    assert not ({d["_id"] for d in digests} & {s["_id"] for s in summaries})
    assert all(int(s["chunk_index"]) <= digest_mapping.SUMMARY_CHUNK_INDEX_BASE for s in summaries)


def test_a_summary_document_carries_both_identifiers(chunk_index, summarized_file, db_session):
    """G5: the ACL rewrite keys on file_id, the tenant backfill on file_uuid."""
    from app.core.config import settings

    media_file, _ = summarized_file
    _index(media_file, db_session)
    summaries = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, str(media_file.uuid))

    assert summaries
    for document in summaries:
        assert int(document["file_id"]) == media_file.id
        assert document["file_uuid"] == str(media_file.uuid)
        assert "speaker" not in document
        assert "start_time" not in document
        assert "end_time" not in document


def test_a_file_with_no_summary_indexes_zero_summary_documents(
    chunk_index, summarized_file, db_session
):
    """#462 rejection 3 — the no-LLM deployment / a file with no summary yet."""
    from app.core.config import settings

    media_file, _ = summarized_file
    media_file.summary_data = None
    db_session.flush()

    _index(media_file, db_session)
    summaries = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, str(media_file.uuid))
    assert summaries == []


def test_share_revocation_reaches_summary_documents(chunk_index, summarized_file, db_session):
    """A revoked share must not leave a readable summary behind."""
    from app.core.config import settings

    media_file, owner = summarized_file
    guest_id = owner.id + 10_000
    _index(media_file, db_session, accessible_user_ids=[owner.id, guest_id])
    file_uuid = str(media_file.uuid)

    granted = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid)
    assert granted, "no summary documents to revoke access to"
    assert all(guest_id in s["accessible_user_ids"] for s in granted), (
        "control failed: the guest never had access, so revoking it proves nothing"
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

    after = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid)
    assert after
    for document in after:
        assert guest_id not in document["accessible_user_ids"], (
            "a summary survived a share revocation with the revoked user still on its ACL"
        )


def test_a_full_reindex_preserves_and_regenerates_the_summary_plane(
    chunk_index, summarized_file, db_session
):
    """G1, end to end. The delete is unqualified; the rebuild must put the summary back.

    This is the test that catches Edit 5 being omitted — the single most likely
    way this feature regresses (issue #963 trap register, item 1).
    """
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService

    media_file, _ = summarized_file
    service = TranscriptIndexingService()
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)
    before = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid)
    assert before

    service.reindex_transcript(
        file_id=media_file.id,
        file_uuid=file_uuid,
        user_id=media_file.user_id,
        segments=_segments_for(media_file, db_session),
        title=media_file.title or "",
        speakers=["SPEAKER_00", "SPEAKER_01"],
        tags=[],
    )

    after = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid)
    assert after, "the rebuild destroyed the summary plane and did not put it back"
    assert sorted(d["content"] for d in after) == sorted(d["content"] for d in before)


def test_deleting_a_file_leaves_no_summary_behind(chunk_index, summarized_file, db_session):
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService

    media_file, _ = summarized_file
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)
    assert _plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid, None), (
        "nothing was indexed, so nothing is proven"
    )

    TranscriptIndexingService().delete_transcript_chunks(file_uuid)

    assert _plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid, None) == [], (
        "a document survived the per-file delete — a summary that outlives its "
        "recording is a readable digest of deleted content"
    )


def test_a_shorter_resummarization_leaves_no_orphan_leaf(chunk_index, summarized_file, db_session):
    """The #400/#435 hazard, one plane over: the id embeds the leaf number."""
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService

    media_file, _ = summarized_file
    service = TranscriptIndexingService()
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)
    leaves = len(_summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid))
    assert leaves >= 1

    # Plant a leaf beyond what the summary will ever produce again, exactly as a
    # longer previous summary would have left it — deliberately beyond a
    # contiguous assumption (mirrors test_a_shorter_resection_leaves_no_orphan_digest).
    orphan_index = leaves + 3
    chunk_index.index(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        id=digest_mapping.summary_document_id(file_uuid, orphan_index),
        body={
            "file_id": media_file.id,
            "file_uuid": file_uuid,
            "doc_type": digest_mapping.DOC_TYPE_SUMMARY,
            "chunk_index": digest_mapping.summary_chunk_index(orphan_index),
            "summary_leaf_index": orphan_index,
            "summary_key_path": "stale.leaf",
            "content": "stale leaf from a longer previous summary",
            "embedding_text": "stale leaf from a longer previous summary",
        },
        refresh=True,
    )

    # Re-summarizing with fewer leaves: shrink summary_data in place and reindex.
    media_file.summary_data = {"bluf": "A single, shorter summary line."}
    db_session.flush()
    service.reindex_transcript(
        file_id=media_file.id,
        file_uuid=file_uuid,
        user_id=media_file.user_id,
        segments=_segments_for(media_file, db_session),
        title=media_file.title or "",
        speakers=["SPEAKER_00", "SPEAKER_01"],
        tags=[],
    )

    remaining = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid)
    assert remaining
    assert max(int(s["summary_leaf_index"]) for s in remaining) < orphan_index, (
        "an orphan summary leaf survived the rebuild and still matches every query the file matches"
    )


def test_count_and_delete_cover_the_summary_plane_via_file_plane_query(
    chunk_index, summarized_file, db_session
):
    """T-I6: file_plane_query has no doc_type predicate — verify it, don't assume it."""
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService

    media_file, _ = summarized_file
    service = TranscriptIndexingService()
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)

    total = service.count_file_documents(file_uuid)
    summaries = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid)
    assert summaries
    assert total >= len(summaries)

    deleted = service.delete_transcript_chunks(file_uuid)
    assert deleted >= len(summaries)
    assert service.count_file_documents(file_uuid) == 0


def test_the_search_path_never_returns_a_summary_by_default(
    chunk_index, summarized_file, db_session
):
    """The default transcript-search plane clause must exclude summary docs.

    The summary is composed prose that shares no verbatim text with the
    transcript, so a query built from the SUMMARY finding zero chunk-plane
    matches — but hitting a summary document if it were visible — proves the
    exclusion rather than assuming it.
    """
    from app.core.config import settings
    from app.services.search.hybrid_search_service import HybridSearchService

    media_file, owner = summarized_file
    _index(media_file, db_session)
    file_uuid = str(media_file.uuid)
    summaries = _summary_plane(chunk_index, settings.OPENSEARCH_CHUNKS_INDEX, file_uuid)
    assert summaries

    service = HybridSearchService()
    filters = service._build_filters(
        owner.id, None, None, None, None, file_uuid=file_uuid, organization_id=None
    )
    response = chunk_index.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={
            "size": 200,
            "query": {
                "bool": {
                    "must": [{"match": {"content": "business trip costs quarter"}}],
                    "filter": filters,
                }
            },
        },
    )
    hits = response["hits"]["hits"]
    assert all(hit["_source"].get("doc_type") != digest_mapping.DOC_TYPE_SUMMARY for hit in hits), (
        "a summary document reached the default (chunk-plane) search path"
    )


# --------------------------------------------------------------------------- #
# T-R1 — the one test that can only go green through the new OpenSearch path   #
# --------------------------------------------------------------------------- #


def test_semantic_retrieval_finds_a_summary_leaf_with_no_lexical_overlap(monkeypatch, db_session):
    """The capability the retired Postgres FTS leg structurally could not have.

    A file's ``summary_data`` contains a leaf about cutting travel costs; the
    query shares NO lexeme with it. ``websearch_to_tsquery`` would return zero
    rows for this at HEAD. The new plane's kNN leg must find it via semantic
    similarity.

    Guard against the two ways this test lies (issue #963's own instruction):
    it asserts the neural pipeline is actually available and FAILS LOUDLY,
    never skips, if it is not — a silent skip is indistinguishable from a pass.
    """
    import contextlib

    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.indexing_service import is_neural_pipeline_available

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    @contextlib.contextmanager
    def _test_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)

    name = f"test_summary_plane_semantic_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", True)
    svc.reset_neural_pipeline_state()
    svc.ensure_chunks_index_exists()
    try:
        assert is_neural_pipeline_available(), (
            "the neural ingest pipeline could not be verified against this cluster — "
            "this test must FAIL, not skip, because a skip here is indistinguishable "
            "from a pass and this is the ONE test proving the new plane's semantic "
            "capability (issue #963, T-R1)"
        )

        user = User(
            email=f"summary_sem_{uuid_pkg.uuid4().hex[:10]}@example.com",
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
            filename="semantic.wav",
            storage_path="x/semantic.wav",
            file_size=1,
            content_type="audio/wav",
            duration=60.0,
            language="en",
            title="Cost review",
            summary_data={
                "bluf": "Cut business-trip costs by 20% before the next quarter.",
            },
        )
        db_session.add(media_file)
        db_session.flush()
        db_session.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                start_time=0.0,
                end_time=8.0,
                text="Let's discuss the upcoming product roadmap for next year.",
            )
        )
        db_session.flush()

        _index(media_file, db_session)
        file_uuid = str(media_file.uuid)
        summaries = _summary_plane(client, name, file_uuid)
        assert summaries, "no summary documents were indexed — nothing to retrieve semantically"

        service = HybridSearchService()
        hits, total = service.search_summary_plane(
            "reduce travel spend", user.id, organization_id=None, page=1, page_size=10
        )

        assert total >= 1, (
            "the semantically-related query found nothing — the kNN leg of the "
            "summary plane is not working"
        )
        matched_uuids = {h["file_uuid"] for h in hits}
        assert file_uuid in matched_uuids, (
            "the semantically-related file was not among the results at all"
        )
    finally:
        client.indices.delete(index=name, ignore=[404])
        svc.reset_neural_pipeline_state()
