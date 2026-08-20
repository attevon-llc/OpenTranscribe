"""The #506 no-stemmed-leg arm: a named BM25 field preset threaded through
``retrieve_chunks``/``retrieve_digests``, replacing the hard-coded field list
``_build_body`` used to carry unconditionally.

Pure unit tests only — body construction and preset resolution need no stack.
The response-cache-key half of this guarantee (folding the preset into
``retrieval_cache.cache_key`` so two arms cannot share one cached result) is
pinned in ``test_chat_retrieval.py`` beside the cache's other keying tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.search.chunk_retrieval import TEXT_FIELD_PRESET_DEFAULT
from app.services.search.chunk_retrieval import TEXT_FIELD_PRESET_NO_STEM
from app.services.search.chunk_retrieval import TEXT_FIELD_PRESETS
from app.services.search.chunk_retrieval import resolve_text_field_preset
from app.services.search.chunk_retrieval import retrieve_chunks
from app.services.search.chunk_retrieval import retrieve_digests
from app.services.search.hybrid_search_service import HybridSearchService


def _multi_match_fields(body: dict) -> list[str]:
    """Pull the field list every ``multi_match`` clause in the text leg was built with."""
    should = body["query"]["bool"]["must"][0]["bool"]["should"]
    fields = {tuple(clause["multi_match"]["fields"]) for clause in should}
    assert len(fields) == 1, (
        f"expected one consistent field list across should clauses, got {fields}"
    )
    return list(next(iter(fields)))


# ---------------------------------------------------------------------------
# resolve_text_field_preset — pure logic, no stack
# ---------------------------------------------------------------------------


def test_default_preset_resolves_to_none():
    """None tells _build_body to keep its own historical field list unchanged."""
    assert resolve_text_field_preset(TEXT_FIELD_PRESET_DEFAULT) is None


def test_no_stem_preset_reuses_get_search_fields_boost_logic():
    """The no-stem preset must not hand-roll a second field list — it reuses
    HybridSearchService's own boosted list (use_exact=True), which is what
    drops the stemmed `content` leg in favor of `content.exact`.
    """
    resolved = resolve_text_field_preset(TEXT_FIELD_PRESET_NO_STEM)
    expected = HybridSearchService()._get_search_fields(False, use_exact=True)
    assert resolved == expected
    # And, concretely: no bare stemmed `content` field anywhere in it.
    assert not any(f == "content" or f.startswith("content^") for f in resolved)
    assert any(f.startswith("content.exact") for f in resolved)


def test_no_stem_preset_honors_the_speaker_filter_flag():
    with_speaker = resolve_text_field_preset(TEXT_FIELD_PRESET_NO_STEM, has_speaker_filter=True)
    without_speaker = resolve_text_field_preset(TEXT_FIELD_PRESET_NO_STEM, has_speaker_filter=False)
    # The "no-stem" preset always resolves to a real field list, never None —
    # narrowed explicitly so the comprehensions below type-check.
    assert with_speaker is not None
    assert without_speaker is not None
    assert with_speaker != without_speaker
    assert not any(f.startswith("speaker") for f in with_speaker)
    assert any(f.startswith("speaker") for f in without_speaker)


def test_unknown_preset_raises():
    with pytest.raises(ValueError, match="bogus"):
        resolve_text_field_preset("bogus")


def test_text_field_presets_tuple_is_exactly_the_two_named_values():
    assert TEXT_FIELD_PRESETS == ("default", "no-stem")


# ---------------------------------------------------------------------------
# retrieve_chunks / retrieve_digests — text_fields actually reaches the body
# ---------------------------------------------------------------------------


def test_retrieve_chunks_default_text_fields_is_unchanged():
    """No override => the exact historical field list, byte for byte."""
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("q", user_id=1, search_mode="keyword")

    body = client.search.call_args.kwargs["body"]
    assert _multi_match_fields(body) == ["content", "content.exact", "title"]


def test_retrieve_chunks_honors_a_text_fields_override():
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    override = ["content.exact^3", "title^2"]
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("q", user_id=1, search_mode="keyword", text_fields=override)

    body = client.search.call_args.kwargs["body"]
    assert _multi_match_fields(body) == override


def test_retrieve_chunks_no_stem_preset_excludes_stemmed_content():
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    resolved = resolve_text_field_preset(TEXT_FIELD_PRESET_NO_STEM)
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("q", user_id=1, search_mode="keyword", text_fields=resolved)

    fields = _multi_match_fields(client.search.call_args.kwargs["body"])
    assert "content" not in fields
    assert any(f.startswith("content.exact") for f in fields)


def test_retrieve_digests_honors_a_text_fields_override():
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    override = ["content.exact^3", "title^2"]
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_digests("q", user_id=1, search_mode="keyword", text_fields=override)

    body = client.search.call_args.kwargs["body"]
    assert _multi_match_fields(body) == override
