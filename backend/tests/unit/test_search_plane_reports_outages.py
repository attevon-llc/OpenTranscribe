"""An OpenSearch outage must not read as "no results" / "no facets"."""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.services.search.collapsed_search import _search_with_collapse


def _collapse(service: MagicMock, *, use_neural: bool):
    return _search_with_collapse(
        service,
        query="budget",
        search_query="budget",
        filters=[],
        page=1,
        page_size=10,
        sort_by="relevance",
        sort_order="desc",
        search_mode="hybrid" if use_neural else "keyword",
        filters_applied={},
        start_time=time.time(),
        has_speaker_filter=False,
        use_neural=use_neural,
        search_pipeline="p",
    )


def _service(client: MagicMock) -> MagicMock:
    service = MagicMock()
    service._client.return_value = client
    service._build_collapsed_search_body.return_value = ({"query": {}}, False)
    return service


def test_keyword_search_outage_raises_instead_of_an_empty_page():
    client = MagicMock()
    service = _service(client)
    service._execute_split_bm25_collapse.side_effect = ConnectionError("down")

    with pytest.raises(ServiceUnavailableError):
        _collapse(service, use_neural=False)


def test_hybrid_search_outage_with_failed_bm25_fallback_raises():
    client = MagicMock()
    client.search.side_effect = ConnectionError("down")
    service = _service(client)

    with pytest.raises(ServiceUnavailableError):
        _collapse(service, use_neural=True)
    assert client.search.call_count == 3  # first try, retry, BM25 fallback


def test_available_filters_outage_raises_instead_of_empty_facets():
    from app.services.search import suggestions

    client = MagicMock()
    client.search.side_effect = ConnectionError("down")
    with (
        patch.object(suggestions._client, "opensearch_client", client),
        patch.object(suggestions, "_chunks_index_verified", return_value=True),
        patch.object(suggestions, "_quarantined_file_uuids", return_value=[]),
        pytest.raises(ServiceUnavailableError),
    ):
        suggestions.get_available_filters(user_id=1, organization_id=None, is_admin=False)
