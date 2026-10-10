"""Document-shape units for the OpenSearch summary plane (issue #963).

Modelled on ``test_digest_index_mapping.py``. Nothing here touches OpenSearch —
these hold ``build_summary_documents``/``summary_document_ids``/the sentinel
helpers to the shape the write path (``indexing_service._index_summary_plane``)
and the read path (``hybrid_search_service.search_summary_plane``) both depend
on.
"""

from __future__ import annotations

from typing import Any

from app.services.ingest_artifacts import index_mapping as target


def _base_metadata() -> dict:
    return {
        "user_id": 7,
        "title": "Q3 Planning",
        "tags": ["planning"],
        "upload_time": "2024-03-15T00:00:00",
        "language": "en",
        "content_type": "audio/mp4",
        "duration": 1800.0,
        "file_size": 12345,
        "collection_ids": [3],
        "accessible_user_ids": [7, 8],
        "indexed_at": "2024-03-16T00:00:00",
        "embedding_model": "all-MiniLM-L6-v2",
        # A digest document pops this; a summary builder must too (T-B4).
        "speaker": "should never survive onto a summary document",
    }


def test_every_document_carries_both_file_id_and_file_uuid():
    """G5: an ACL rewrite keys on file_id, the tenant backfill on file_uuid.

    A summary document missing either becomes unreachable by one of them — a
    permission leak, not a relevance bug (T-B3).
    """
    docs = target.build_summary_documents(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=42,
        summary_data={"bluf": "Ship the thing.", "risks": ["budget", "timeline"]},
        facts={"roster": ["Dana", "Marcus"], "recorded_at": "2024-03-15"},
        base_metadata=_base_metadata(),
    )
    assert docs
    for doc in docs:
        assert doc["file_id"] == 42
        assert doc["file_uuid"] == "11111111-1111-1111-1111-111111111111"


def test_speaker_singular_is_absent_speakers_plural_is_present():
    """T-B4: a summary leaf is not attributable to one speaker."""
    docs = target.build_summary_documents(
        file_uuid="u",
        file_id=1,
        summary_data={"bluf": "Text."},
        facts={"roster": ["Dana"], "recorded_at": None},
        base_metadata=_base_metadata(),
    )
    assert docs
    for doc in docs:
        assert "speaker" not in doc
        assert doc["speakers"] == ["Dana"]


def test_start_time_and_end_time_are_absent():
    """T-B9: a summary leaf has no timespan — do not fabricate one."""
    docs = target.build_summary_documents(
        file_uuid="u",
        file_id=1,
        summary_data={"bluf": "Text."},
        facts={},
        base_metadata=_base_metadata(),
    )
    assert docs, "fixture produced no documents — this test would pass vacuously"
    for doc in docs:
        assert "start_time" not in doc
        assert "end_time" not in doc


def test_key_path_values_match_the_leaf_walker_exactly():
    """T-B5: two walkers would be two chances to disagree about key_path."""
    from app.services.search.summary_search import walk_summary_leaves

    summary_data = {
        "major_topics": [{"key_points": ["first point", "second point"]}],
        "bluf": "Summary line.",
    }
    docs = target.build_summary_documents(
        file_uuid="u",
        file_id=1,
        summary_data=summary_data,
        facts={},
        base_metadata=_base_metadata(),
    )
    expected_paths = [p for p, _ in walk_summary_leaves(summary_data, "")]
    assert [d["summary_key_path"] for d in docs] == expected_paths
    assert "major_topics[0].key_points[0]" in expected_paths


def test_metadata_key_is_skipped_only_at_the_top_level():
    """T-B6: machine metadata is not searchable content, but a NESTED
    ``metadata`` key (e.g. a section literally titled "metadata") is real
    content and must still be indexed.
    """
    summary_data = {
        "metadata": {"provider": "openai", "model": "gpt-4"},
        "major_topics": [{"metadata": "a section actually named metadata"}],
    }
    docs = target.build_summary_documents(
        file_uuid="u",
        file_id=1,
        summary_data=summary_data,
        facts={},
        base_metadata=_base_metadata(),
    )
    paths = [d["summary_key_path"] for d in docs]
    assert not any(p.startswith("metadata.") or p == "metadata" for p in paths)
    assert "major_topics[0].metadata" in paths


def test_none_empty_and_non_dict_summary_data_produce_no_documents_and_never_raise():
    """T-B7: #462 rejection 3 — the no-LLM deployment has no summaries at all."""
    bad_values: tuple[Any, ...] = (None, {}, "a plain string", ["a", "list"], 42)
    for bad in bad_values:
        assert (
            target.build_summary_documents(
                file_uuid="u",
                file_id=1,
                summary_data=bad,
                facts={},
                base_metadata=_base_metadata(),
            )
            == []
        )
        assert target.summary_document_ids("u", bad) == []


def test_embedding_text_reuses_build_embedding_text_not_a_second_format():
    """T-B8: one contextualization header format, shared with the digest plane."""
    docs = target.build_summary_documents(
        file_uuid="u",
        file_id=1,
        summary_data={"bluf": "The plan is set."},
        facts={"roster": ["Dana"], "recorded_at": "2024-03-15"},
        base_metadata={**_base_metadata(), "title": "Kickoff"},
    )
    assert docs
    expected = target.build_embedding_text(
        title="Kickoff", recorded_at="2024-03-15", roster=["Dana"], body="The plan is set."
    )
    assert docs[0]["embedding_text"] == expected


def test_the_sentinel_bands_never_collide():
    """T-G7: digest and summary sentinels, and their document ids, never collide."""
    for n in range(5000):
        assert target.digest_chunk_index(n) != target.summary_chunk_index(n)
        assert target.summary_chunk_index(n) < target.SUMMARY_CHUNK_INDEX_BASE + 1
        summary_id = target.summary_document_id("u", n)
        assert summary_id != target.digest_document_id("u", n)
        assert summary_id != f"u_{n}"


def test_a_summary_is_never_verbatim():
    """T-G4: the structural half of #462 rejection 1 — a summary must never
    surface as if a speaker had said it.
    """
    assert target.DOC_TYPE_SUMMARY not in target.VERBATIM_DOC_TYPES
    assert target.DOC_TYPE_SUMMARY in target.DOC_TYPES


def test_summary_plane_clause_matches_only_the_summary_doc_type():
    clause = target.summary_plane_clause()
    assert clause == {"term": {"doc_type": "summary"}}


def test_additive_version_is_now_three():
    """T-B10: step 3 landed additively; steps 1 and 2 stay byte-for-byte."""
    from app.services.search.indexing_service import _ADDITIVE_VERSION
    from app.services.search.indexing_service import ADDITIVE_MAPPING_STEPS

    assert _ADDITIVE_VERSION == 3
    versions = [s.version for s in ADDITIVE_MAPPING_STEPS]
    assert versions == sorted(versions)
    step3 = next(s for s in ADDITIVE_MAPPING_STEPS if s.version == 3)
    assert "summary_key_path" in step3.properties
    assert "summary_leaf_index" in step3.properties
    assert step3.properties["summary_key_path"]["type"] == "keyword"
