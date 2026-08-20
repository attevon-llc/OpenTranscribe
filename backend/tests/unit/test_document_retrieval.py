"""The document-chunk retrieval leg (issue #463): chat's chunk plane, the
search-UI ``result_type=documents``/``all`` leg, the cache round-trip, and
document citations.

Everything here is mocked at the OpenSearch client boundary — no live stack
required, matching ``tests/unit/test_chat_retrieval.py``'s conventions for the
same module.
"""

from __future__ import annotations

from typing import TypedDict
from typing import Unpack
from unittest.mock import MagicMock
from unittest.mock import patch

from app.services.chat.citations import KIND_CHUNK
from app.services.chat.citations import KIND_DIGEST
from app.services.chat.citations import KIND_DOCUMENT
from app.services.chat.citations import build_citation
from app.services.chat.redactor import MaskedChunk
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.ingest_artifacts.index_mapping import document_chunk_plane_clause
from app.services.search.chunk_retrieval import ChunkHit
from app.services.search.chunk_retrieval import DocumentSearchResult
from app.services.search.chunk_retrieval import _widen_to_document_plane
from app.services.search.chunk_retrieval import retrieve_chunks
from app.services.search.chunk_retrieval import search_document_chunks
from app.services.search.hybrid_search_service import HybridSearchService

# --------------------------------------------------------------------------- #
# _widen_to_document_plane — the OR, not a fork
# --------------------------------------------------------------------------- #


def test_widen_ors_the_document_plane_into_the_chunk_plane():
    filters = [{"terms": {"accessible_user_ids": [1]}}, chunk_plane_clause()]
    widened = _widen_to_document_plane(filters, None)

    assert chunk_plane_clause() not in widened
    should_clause = next(f for f in widened if "bool" in f and "should" in f["bool"])
    assert chunk_plane_clause() in should_clause["bool"]["should"]
    assert document_chunk_plane_clause() in should_clause["bool"]["should"]
    assert should_clause["bool"]["minimum_should_match"] == 1
    # Nothing else in the filter list was touched.
    assert {"terms": {"accessible_user_ids": [1]}} in widened


def test_widen_is_a_noop_when_speaker_filtered():
    """Documents have no speaker field — a speaker-scoped turn must never widen."""
    filters = [{"terms": {"accessible_user_ids": [1]}}, chunk_plane_clause()]
    widened = _widen_to_document_plane(filters, ["Dana"])
    assert widened == filters
    assert chunk_plane_clause() in widened


def test_retrieve_chunks_widens_the_query_body_when_no_speaker_filter():
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("q", user_id=1, search_mode="keyword")

    filters = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert chunk_plane_clause() not in filters
    should_clause = next(f for f in filters if "bool" in f and "should" in f["bool"])
    assert document_chunk_plane_clause() in should_clause["bool"]["should"]


def test_retrieve_chunks_excludes_documents_when_speaker_filtered():
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("q", user_id=1, speakers=["Dana"], search_mode="keyword")

    filters = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert chunk_plane_clause() in filters
    assert not any(
        document_chunk_plane_clause() in f.get("bool", {}).get("should", []) for f in filters
    )


def test_retrieve_chunks_parses_a_document_hit_into_a_document_chunkhit():
    client = MagicMock()
    client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 2.0,
                    "_source": {
                        "file_uuid": "doc-1",
                        "file_id": 55,
                        "chunk_index": 3,
                        "content": "section 2 covers pricing",
                        "title": "report.pdf",
                        "doc_type": "document_chunk",
                        "page": 4,
                        "section_path": ["Chapter 2", "2.1 Pricing"],
                        "char_start": 100,
                        "char_end": 130,
                    },
                }
            ]
        }
    }
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        chunks = retrieve_chunks("pricing", user_id=1, search_mode="keyword")

    assert len(chunks) == 1
    hit = chunks[0]
    assert hit.is_document is True
    assert hit.source_kind == "document"
    assert hit.page == 4
    assert hit.section_path == ["Chapter 2", "2.1 Pricing"]
    assert hit.char_start == 100
    assert hit.char_end == 130


# --------------------------------------------------------------------------- #
# The cache round-trip — the documented intermittent-render trap
# --------------------------------------------------------------------------- #


def test_cache_round_trip_preserves_every_document_field():
    original = ChunkHit(
        file_uuid="doc-1",
        file_id=7,
        chunk_index=2,
        content="the terms of the agreement",
        title="contract.pdf",
        source_kind="document",
        page=5,
        section_path=["Section 3", "Termination"],
        char_start=200,
        char_end=260,
    )

    restored = ChunkHit.from_cache_dict(original.to_cache_dict())

    assert restored.page == 5
    assert restored.section_path == ["Section 3", "Termination"]
    assert restored.char_start == 200
    assert restored.char_end == 260
    assert restored.is_document is True


def test_cache_round_trip_of_a_transcript_chunk_carries_no_document_fields():
    original = ChunkHit(
        file_uuid="media-1", file_id=1, chunk_index=0, content="hello", source_kind="media"
    )
    restored = ChunkHit.from_cache_dict(original.to_cache_dict())
    assert restored.page is None
    assert restored.section_path == []
    assert restored.char_start is None
    assert restored.char_end is None


def test_source_fields_allowlist_carries_the_four_document_fields():
    from app.services.search.chunk_retrieval import _build_body

    body = _build_body("q", [], 10, False, None, HybridSearchService(), "keyword")
    for field_name in ("page", "section_path", "char_start", "char_end"):
        assert field_name in body["_source"]


# --------------------------------------------------------------------------- #
# Result identity across TWO id spaces (Document.id vs MediaFile.id)
# --------------------------------------------------------------------------- #


def test_search_document_chunks_groups_by_file_uuid_never_by_bare_file_id():
    """Two colliding integer ids, two distinct file_uuids — a bare file_id key
    would silently merge an unrelated media file's hit into a document's."""
    client = MagicMock()
    client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 2.0,
                    "_source": {
                        "file_uuid": "document-uuid",
                        "file_id": 42,
                        "chunk_index": 0,
                        "content": "the document's own text",
                        "title": "doc.pdf",
                        "doc_type": "document_chunk",
                    },
                },
                {
                    "_score": 1.0,
                    "_source": {
                        "file_uuid": "media-uuid",
                        "file_id": 42,  # same integer id, unrelated row
                        "chunk_index": 0,
                        "content": "an unrelated media file's transcript",
                        "title": "recording.wav",
                        "doc_type": "chunk",
                    },
                },
            ]
        }
    }
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        result = search_document_chunks("q", user_id=1, search_mode="keyword")

    assert result.total_files == 2
    uuids = {hit.file_uuid for hit in result.results}
    assert uuids == {"document-uuid", "media-uuid"}
    # Each hit's own content, never cross-contaminated by the shared file_id.
    by_uuid = {hit.file_uuid: hit for hit in result.results}
    assert by_uuid["document-uuid"].matches[0].snippet == "the document's own text"
    assert by_uuid["media-uuid"].matches[0].snippet == "an unrelated media file's transcript"


def test_search_document_chunks_paginates_over_files():
    client = MagicMock()
    hits = [
        {
            "_score": 10.0 - i,
            "_source": {
                "file_uuid": f"doc-{i}",
                "file_id": i,
                "chunk_index": 0,
                "content": f"document {i} body",
                "title": f"doc-{i}.pdf",
            },
        }
        for i in range(5)
    ]
    client.search.return_value = {"hits": {"hits": hits}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        page1 = search_document_chunks("q", user_id=1, page=1, page_size=2, search_mode="keyword")
        page2 = search_document_chunks("q", user_id=1, page=2, page_size=2, search_mode="keyword")

    assert [h.file_uuid for h in page1.results] == ["doc-0", "doc-1"]
    assert [h.file_uuid for h in page2.results] == ["doc-2", "doc-3"]
    assert page1.total_files == 5
    assert page2.total_files == 5


def test_search_document_chunks_caps_matches_per_file():
    client = MagicMock()
    hits = [
        {
            "_score": 1.0,
            "_source": {
                "file_uuid": "doc-1",
                "file_id": 1,
                "chunk_index": i,
                "content": f"chunk {i}",
                "title": "doc.pdf",
            },
        }
        for i in range(6)
    ]
    client.search.return_value = {"hits": {"hits": hits}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        result = search_document_chunks("q", user_id=1, search_mode="keyword")

    assert len(result.results) == 1
    assert len(result.results[0].matches) == 3  # _DOCUMENT_SEARCH_MATCHES_PER_FILE


def test_search_document_chunks_blank_query_short_circuits():
    with patch("app.services.search.chunk_retrieval.get_opensearch_client") as client:
        result = search_document_chunks("   ", user_id=1)
        assert result == DocumentSearchResult()
        client.assert_not_called()


def test_search_document_chunks_degrades_on_failure():
    client = MagicMock()
    client.search.side_effect = RuntimeError("opensearch down")
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        result = search_document_chunks("q", user_id=1)
    assert result == DocumentSearchResult()


def test_search_document_chunks_filters_by_the_document_plane_and_caller():
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        search_document_chunks("q", user_id=99, search_mode="keyword")

    filters = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert {"terms": {"accessible_user_ids": [99]}} in filters
    assert document_chunk_plane_clause() in filters
    assert chunk_plane_clause() not in filters


# --------------------------------------------------------------------------- #
# Document citations — never a start_time=0 sentinel
# --------------------------------------------------------------------------- #


class _ChunkHitKwargs(TypedDict, total=False):
    """The subset of ``ChunkHit`` constructor fields this fixture overrides.

    Typing the override keys against ``ChunkHit``'s real field types (rather
    than leaving them an untyped ``**overrides``) is what makes the
    ``ChunkHit(**defaults)``/``MaskedChunk(content=...)`` splats below
    checkable at all — a plain heterogeneous dict literal infers to
    ``dict[str, object]``, which no dataclass constructor can be checked
    against.
    """

    file_uuid: str
    file_id: int
    chunk_index: int
    content: str
    title: str
    source_kind: str
    page: int
    section_path: list[str]
    char_start: int
    char_end: int


def _masked_document_chunk(**overrides: Unpack[_ChunkHitKwargs]) -> MaskedChunk:
    defaults: _ChunkHitKwargs = {
        "file_uuid": "doc-1",
        "file_id": 1,
        "chunk_index": 0,
        "content": "the termination clause",
        "title": "contract.pdf",
        "source_kind": "document",
        "page": 3,
        "section_path": ["Section 5", "Termination"],
        "char_start": 10,
        "char_end": 40,
    }
    defaults.update(overrides)
    source = ChunkHit(**defaults)
    return MaskedChunk(source=source, content=defaults["content"])


def test_document_citation_kind_and_no_timestamp_sentinel():
    citation = build_citation(1, _masked_document_chunk())
    assert citation["kind"] == KIND_DOCUMENT
    # The trap this pins: a real 0.0 renders as a clickable 00:00 on a thing
    # that was never a recording. Must be None, never the ChunkHit default.
    assert citation["start_time"] is None
    assert citation["end_time"] is None
    assert citation["speaker"] is None


def test_document_citation_carries_page_and_joined_section_path():
    citation = build_citation(1, _masked_document_chunk())
    assert citation["page"] == 3
    # schemas/chat.py's Citation.section_path is `str | None` (a breadcrumb),
    # not a list — the list from ChunkHit must be joined, not passed raw.
    assert citation["section_path"] == "Section 5 > Termination"
    assert citation["char_start"] == 10
    assert citation["char_end"] == 40


def test_document_citation_with_no_section_path_is_none_not_empty_string():
    citation = build_citation(1, _masked_document_chunk(section_path=[]))
    assert citation["section_path"] is None


def test_chunk_citation_is_unaffected_by_the_document_kind_addition():
    """Regression: adding the document branch must not touch ordinary chunks."""
    chunk = ChunkHit(
        file_uuid="media-1",
        file_id=1,
        chunk_index=0,
        content="hello",
        speaker="Dana",
        start_time=12.0,
        end_time=20.0,
    )
    citation = build_citation(1, MaskedChunk(source=chunk, content="hello"))
    assert citation["kind"] == KIND_CHUNK
    assert citation["start_time"] == 12.0
    assert citation["speaker"] == "Dana"
    assert citation["page"] is None
    assert citation["section_path"] is None


def test_digest_citation_is_unaffected_by_the_document_kind_addition():
    chunk = ChunkHit(
        file_uuid="media-1",
        file_id=1,
        chunk_index=0,
        content="a summary sentence",
        digest_section=2,
        start_time=90.0,
    )
    citation = build_citation(1, MaskedChunk(source=chunk, content="a summary sentence"))
    assert citation["kind"] == KIND_DIGEST
    assert citation["start_time"] == 90.0
    assert citation["speaker"] is None
    assert citation["page"] is None
