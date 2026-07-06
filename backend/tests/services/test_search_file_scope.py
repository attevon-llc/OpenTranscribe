"""Unit tests for the single-file (`file_uuid`) search scope filter.

GPU/OpenSearch-free: exercises ``HybridSearchService._build_filters`` directly to
verify that scoping to one file adds the expected OpenSearch term clause. This is
what lets the in-page transcript find bar list every match across the whole
paginated transcript (including segments not yet loaded in the browser).
"""

from app.services.search.hybrid_search_service import HybridSearchService

FILE_UUID = "11111111-2222-3333-4444-555555555555"


def _term_file_uuid_clauses(filters: list[dict]) -> list[dict]:
    return [f for f in filters if f.get("term", {}).get("file_uuid") is not None]


def test_no_file_uuid_scope_by_default():
    """Global search must not restrict to a single file."""
    service = HybridSearchService()
    filters = service._build_filters(
        user_id=7,
        speakers=None,
        tags=None,
        date_from=None,
        date_to=None,
    )
    assert _term_file_uuid_clauses(filters) == []


def test_file_uuid_scope_adds_term_filter():
    """Passing file_uuid restricts results to that file via a term filter."""
    service = HybridSearchService()
    filters = service._build_filters(
        user_id=7,
        speakers=None,
        tags=None,
        date_from=None,
        date_to=None,
        file_uuid=FILE_UUID,
    )
    clauses = _term_file_uuid_clauses(filters)
    assert clauses == [{"term": {"file_uuid": FILE_UUID}}]


def test_file_uuid_scope_composes_with_other_filters():
    """The file scope coexists with other filters (e.g. language)."""
    service = HybridSearchService()
    filters = service._build_filters(
        user_id=7,
        speakers=["Alice"],
        tags=None,
        date_from=None,
        date_to=None,
        language="en",
        file_uuid=FILE_UUID,
    )
    assert {"term": {"file_uuid": FILE_UUID}} in filters
    assert {"term": {"language": "en"}} in filters
    assert {"terms": {"speaker": ["Alice"]}} in filters
    # The caller scope (accessible_user_ids) is always present.
    assert {"terms": {"accessible_user_ids": [7]}} in filters
