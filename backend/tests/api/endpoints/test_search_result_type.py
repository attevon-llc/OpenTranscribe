"""GET /api/search's ``result_type`` parameter (issue #462, #463).

One parameter, ``result_type: transcripts | summaries | documents | all``,
defaulting to ``transcripts`` so every existing caller is byte-identical.
``documents`` (issue #463) now searches the document-chunk plane — see
``api/endpoints/search.py::_document_search_payload`` and
``services/search/chunk_retrieval.search_document_chunks``.

Most of this needs no OpenSearch: the transcript leg is stubbed via
``HybridSearchService.search`` (same technique as ``test_search.py``), the
document leg via ``chunk_retrieval.search_document_chunks`` (same technique,
applied the same way), so these tests are deterministic and fast, and the
summary leg is real Postgres.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.prompt import UserSetting
from app.models.sharing import CollectionShare
from app.services.search.chunk_retrieval import DocumentChunkMatch
from app.services.search.chunk_retrieval import DocumentSearchHit
from app.services.search.chunk_retrieval import DocumentSearchResult
from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import SearchResponse

SEARCH_PATH = "/api/search"

_DEFAULT_KEYS = {
    "query",
    "results",
    "total_results",
    "total_files",
    "page",
    "page_size",
    "total_pages",
    "search_time_ms",
    "filters_applied",
    "search_mode",
}


def _empty_transcript_response(query: str) -> SearchResponse:
    return SearchResponse(
        query=query,
        results=[],
        total_results=0,
        total_files=0,
        page=1,
        page_size=20,
        total_pages=0,
        search_time_ms=1.0,
    )


@pytest.fixture
def stub_transcript_search(monkeypatch):
    """Deterministic, empty transcript leg — installed explicitly per test so a
    test that means to prove the transcript leg is NEVER called can instead
    install a raising stub."""

    def _fake_search(_self, **kwargs):
        return _empty_transcript_response(kwargs.get("query", ""))

    monkeypatch.setattr(HybridSearchService, "search", _fake_search)


@pytest.fixture
def transcript_search_must_not_be_called(monkeypatch):
    def _explode(_self, **_kwargs):
        raise AssertionError("HybridSearchService.search was called for a summaries-only request")

    monkeypatch.setattr(HybridSearchService, "search", _explode)


@pytest.fixture
def stub_document_search(monkeypatch):
    """Deterministic, empty document leg — the document-search analog of
    ``stub_transcript_search``. ``_document_search_payload`` imports
    ``search_document_chunks`` locally, so patching the module attribute is
    what that late-binding import re-reads."""

    def _fake(_query, **_kwargs):
        return DocumentSearchResult()

    monkeypatch.setattr("app.services.search.chunk_retrieval.search_document_chunks", _fake)


def _make_file(db_session, user, *, summary, title=None) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
        title=title,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        user_id=user.id,
        status="completed",
        summary_data=summary,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _share_with(db_session, owner, recipient, media_file, *, permission="viewer") -> None:
    collection = Collection(
        user_id=owner.id,
        name=f"share-{uuid_pkg.uuid4().hex[:8]}",
        description="result_type endpoint test",
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=recipient.id,
            permission=permission,
        )
    )
    db_session.commit()


# --------------------------------------------------------------------------- #
# Parameter contract                                                           #
# --------------------------------------------------------------------------- #


class TestResultTypeParameterContract:
    def test_an_unknown_result_type_is_400(
        self, client, user_token_headers, stub_transcript_search
    ):
        response = client.get(
            SEARCH_PATH, params={"q": "test", "result_type": "bogus"}, headers=user_token_headers
        )
        assert response.status_code == 400, response.text

    def test_documents_alone_is_200(
        self, client, user_token_headers, stub_transcript_search, stub_document_search
    ):
        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "result_type": "documents"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text

    def test_all_includes_the_documents_leg(
        self, client, user_token_headers, stub_transcript_search, stub_document_search
    ):
        response = client.get(
            SEARCH_PATH, params={"q": "test", "result_type": "all"}, headers=user_token_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "document_results" in body
        assert "document_total" in body


# --------------------------------------------------------------------------- #
# Default is byte-identical to explicit transcripts-only                       #
# --------------------------------------------------------------------------- #


class TestDefaultIsByteIdenticalToTranscriptsOnly:
    def test_omitting_result_type_matches_explicit_transcripts(
        self, client, user_token_headers, stub_transcript_search
    ):
        omitted = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers)
        explicit = client.get(
            SEARCH_PATH,
            params={"q": "test", "result_type": "transcripts"},
            headers=user_token_headers,
        )
        assert omitted.status_code == 200, omitted.text
        assert explicit.status_code == 200, explicit.text
        assert omitted.json() == explicit.json()

    def test_the_default_response_carries_no_summary_keys(
        self, client, user_token_headers, stub_transcript_search
    ):
        body = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers).json()
        # `embedding_warning` is pre-existing, conditional (#437 mixed-index
        # advisory) and unrelated to this change — everything else must match
        # the documented transcript-only shape exactly.
        assert set(body.keys()) - {"embedding_warning"} == _DEFAULT_KEYS
        assert "summary_results" not in body
        assert "summary_total" not in body
        assert "document_results" not in body
        assert "document_total" not in body

    def test_a_summary_that_would_match_is_invisible_by_default(
        self, client, user_token_headers, normal_user, db_session, stub_transcript_search
    ):
        """The strongest form of the pin: a summary genuinely matching the
        query must not leak into the default (transcripts-only) response."""
        _make_file(db_session, normal_user, summary={"bluf": "a very particular roadmap phrase"})
        body = client.get(
            SEARCH_PATH, params={"q": "particular"}, headers=user_token_headers
        ).json()
        assert body["results"] == []
        assert "summary_results" not in body


# --------------------------------------------------------------------------- #
# summaries                                                                     #
# --------------------------------------------------------------------------- #


class TestSummariesResultType:
    def test_summaries_only_never_calls_the_transcript_service(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text

    def test_summaries_only_response_shape(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        _make_file(db_session, normal_user, summary={"bluf": "a distinctive roadmap phrase"})
        body = client.get(
            SEARCH_PATH,
            params={"q": "distinctive", "result_type": "summaries"},
            headers=user_token_headers,
        ).json()
        assert body["results"] == []
        assert body["total_results"] == 0
        assert body["summary_total"] == 1
        assert len(body["summary_results"]) == 1
        hit = body["summary_results"][0]
        assert hit["matches"] == [{"key_path": "bluf", "snippet": "a distinctive roadmap phrase"}]

    def test_all_returns_both_legs(
        self, client, user_token_headers, normal_user, db_session, stub_transcript_search
    ):
        _make_file(db_session, normal_user, summary={"bluf": "a distinctive roadmap phrase"})
        body = client.get(
            SEARCH_PATH,
            params={"q": "distinctive", "result_type": "all"},
            headers=user_token_headers,
        ).json()
        assert "results" in body  # transcript leg (stubbed empty)
        assert body["summary_total"] == 1


# --------------------------------------------------------------------------- #
# documents (issue #463)                                                      #
# --------------------------------------------------------------------------- #


class TestDocumentsResultType:
    def test_documents_only_never_calls_the_transcript_service(
        self, client, user_token_headers, transcript_search_must_not_be_called, stub_document_search
    ):
        response = client.get(
            SEARCH_PATH,
            params={"q": "revenue", "result_type": "documents"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text

    def test_documents_only_response_shape(
        self, client, user_token_headers, transcript_search_must_not_be_called, monkeypatch
    ):
        def _fake(_query, **_kwargs):
            return DocumentSearchResult(
                results=[
                    DocumentSearchHit(
                        file_uuid="doc-uuid-1",
                        file_id=999,
                        title="report.pdf",
                        matches=[
                            DocumentChunkMatch(
                                chunk_index=0,
                                page=2,
                                section_path=["Intro"],
                                snippet="quarterly revenue rose",
                                score=1.5,
                            )
                        ],
                    )
                ],
                total_results=1,
                total_files=1,
            )

        monkeypatch.setattr("app.services.search.chunk_retrieval.search_document_chunks", _fake)
        body = client.get(
            SEARCH_PATH,
            params={"q": "revenue", "result_type": "documents"},
            headers=user_token_headers,
        ).json()
        assert body["results"] == []
        assert body["document_total"] == 1
        assert len(body["document_results"]) == 1
        hit = body["document_results"][0]
        assert hit["file_uuid"] == "doc-uuid-1"
        assert hit["file_id"] == 999
        assert hit["title"] == "report.pdf"
        match = hit["matches"][0]
        assert match["chunk_index"] == 0
        assert match["page"] == 2
        assert match["section_path"] == ["Intro"]
        assert match["snippet"] == "quarterly revenue rose"

    def test_all_includes_document_results_alongside_the_other_legs(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        stub_transcript_search,
        monkeypatch,
    ):
        _make_file(db_session, normal_user, summary={"bluf": "unrelated"})

        def _fake(_query, **_kwargs):
            return DocumentSearchResult(
                results=[DocumentSearchHit(file_uuid="doc-uuid-2", file_id=1, title="notes.docx")],
                total_results=1,
                total_files=1,
            )

        monkeypatch.setattr("app.services.search.chunk_retrieval.search_document_chunks", _fake)
        body = client.get(
            SEARCH_PATH, params={"q": "notes", "result_type": "all"}, headers=user_token_headers
        ).json()
        assert "results" in body  # transcript leg present, stubbed empty
        assert "summary_total" in body  # summary leg present
        assert body["document_total"] == 1
        assert [h["file_uuid"] for h in body["document_results"]] == ["doc-uuid-2"]

    def test_a_broken_document_leg_never_500s(
        self, client, user_token_headers, stub_transcript_search, monkeypatch
    ):
        """search_document_chunks degrades to an empty result on any OpenSearch
        failure (see its own docstring) — the endpoint must never surface a 500
        for a document-leg outage."""

        def _fake(_query, **_kwargs):
            return DocumentSearchResult()

        monkeypatch.setattr("app.services.search.chunk_retrieval.search_document_chunks", _fake)
        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "result_type": "documents"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["document_results"] == []


# --------------------------------------------------------------------------- #
# Masking fails closed                                                         #
# --------------------------------------------------------------------------- #


class TestSummaryMaskingFailsClosed:
    def test_a_detector_outage_is_503(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        monkeypatch,
        stub_transcript_search,
    ):
        from app.services.redaction.summary_redaction import SummaryMaskingUnavailableError
        from app.services.search import summary_search

        _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        for key, value in (("redaction_enabled", "true"), ("redaction_categories", '["pii"]')):
            db_session.add(
                UserSetting(user_id=normal_user.id, setting_key=key, setting_value=value)
            )
        db_session.commit()

        def _raise(*_args, **_kwargs):
            raise SummaryMaskingUnavailableError("pii detector unavailable")

        monkeypatch.setattr(summary_search, "mask_summary", _raise)

        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 503, response.text

    def test_a_document_leg_detector_outage_is_503(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        monkeypatch,
        stub_transcript_search,
    ):
        from app.services.search import snippet_redaction

        for key, value in (("redaction_enabled", "true"), ("redaction_categories", '["pii"]')):
            db_session.add(
                UserSetting(user_id=normal_user.id, setting_key=key, setting_value=value)
            )
        db_session.commit()

        def _fake_search(_query, **_kwargs):
            return DocumentSearchResult(
                results=[
                    DocumentSearchHit(
                        file_uuid="doc-outage",
                        file_id=1,
                        title="report.pdf",
                        matches=[
                            DocumentChunkMatch(
                                chunk_index=0,
                                page=None,
                                section_path=[],
                                snippet="call me",
                                score=1.0,
                            )
                        ],
                    )
                ],
                total_results=1,
                total_files=1,
            )

        def _raise(*_args, **_kwargs):
            raise snippet_redaction.SnippetMaskingUnavailableError("pii detector unavailable")

        monkeypatch.setattr(
            "app.services.search.chunk_retrieval.search_document_chunks", _fake_search
        )
        monkeypatch.setattr(snippet_redaction, "mask_snippets", _raise)

        response = client.get(
            SEARCH_PATH,
            params={"q": "call", "result_type": "documents"},
            headers=user_token_headers,
        )
        assert response.status_code == 503, response.text


# --------------------------------------------------------------------------- #
# Permission matrix row T5                                                     #
# --------------------------------------------------------------------------- #


class TestPermissionMatrixT5:
    def test_leak_a_summary_shared_only_via_a_different_collection_is_invisible(
        self,
        client,
        user_token_headers,
        normal_user,
        other_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        blocked = _make_file(
            db_session, other_user, summary={"bluf": "The confidential merger term-sheet."}
        )
        visible = _make_file(db_session, other_user, summary={"bluf": "The public roadmap update."})
        _share_with(db_session, other_user, normal_user, visible)

        response = client.get(
            SEARCH_PATH,
            params={"q": "merger", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary_total"] == 0
        assert body["summary_results"] == []
        assert str(blocked.uuid) not in [h["file_uuid"] for h in body["summary_results"]]

    def test_shared_visibility_a_real_share_makes_the_summary_reachable(
        self,
        client,
        user_token_headers,
        normal_user,
        other_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        shared = _make_file(db_session, other_user, summary={"bluf": "The public roadmap update."})
        _share_with(db_session, other_user, normal_user, shared)

        response = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary_total"] == 1
        assert [h["file_uuid"] for h in body["summary_results"]] == [str(shared.uuid)]
        assert body["summary_results"][0]["matches"], "a shared-visibility hit must carry its match"


# --------------------------------------------------------------------------- #
# Quarantine (DMCA/abuse) parity with the transcript leg                       #
# --------------------------------------------------------------------------- #


class TestQuarantinedSummaryHitsAreDropped:
    def test_a_quarantined_files_summary_is_dropped_for_a_non_admin(
        self,
        client,
        user_token_headers,
        normal_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        media_file = _make_file(db_session, normal_user, summary={"bluf": "roadmap review"})
        media_file.is_quarantined = True
        db_session.commit()

        body = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=user_token_headers,
        ).json()
        assert body["summary_total"] == 0
        assert body["summary_results"] == []

    def test_an_admin_still_sees_it(
        self,
        client,
        admin_token_headers,
        admin_user,
        db_session,
        transcript_search_must_not_be_called,
    ):
        media_file = _make_file(db_session, admin_user, summary={"bluf": "roadmap review"})
        media_file.is_quarantined = True
        db_session.commit()

        body = client.get(
            SEARCH_PATH,
            params={"q": "roadmap", "result_type": "summaries"},
            headers=admin_token_headers,
        ).json()
        assert body["summary_total"] == 1
        assert [h["file_uuid"] for h in body["summary_results"]] == [str(media_file.uuid)]
