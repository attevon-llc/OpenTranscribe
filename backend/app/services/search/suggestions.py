"""Autocomplete suggestions and the filter-options facets.

Both exclude trashed and quarantined files inside the query, through the ACL clause
(``access_filter_clause``) and the ``availability`` field the takedown and trash actions stamp;
``is_admin`` is the quarantine review exemption (``file_availability.unavailable_list_exempt``).
"""

import logging
from typing import Any

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.opensearch_service import client as _client
from app.services.search.access_filter import access_filter_clause
from app.services.search.hidden_exclusion import exclude_files_clauses
from app.services.search.search_infra import _chunks_index_verified

logger = logging.getLogger(__name__)


def get_suggestions(
    prefix: str,
    user_id: int,
    limit: int = 8,
    organization_id: int | None = None,
    is_admin: bool = False,
    exclude_file_uuids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get auto-complete suggestions.

    Args:
        prefix: Search prefix text.
        user_id: Current user ID.
        limit: Maximum number of suggestions.
        organization_id: Active org id (None = personal) — tenant gate.
        is_admin: The quarantine review exemption: quarantined files' titles and
            speakers are suggested. Trashed files' never are.
        exclude_file_uuids: The caller's hidden files (issue #1102), dropped from
            both legs.

    Returns:
        List of suggestion dicts with type, text, and optional metadata.
    """
    if not _client.opensearch_client:
        return []

    index_name = settings.OPENSEARCH_CHUNKS_INDEX

    if not _chunks_index_verified(_client.opensearch_client, index_name):
        return []

    suggestions = []

    from app.services.search.tenant_scope import org_filter_clauses

    # Both legs share the scope, so a taken-down file's speaker names (which carry no file
    # linkage a post-filter could check, issue #817) are excluded inside the aggregation.
    scope_filter = [
        access_filter_clause(user_id, organization_id, include_quarantined=is_admin),
        *org_filter_clauses(organization_id),
        # Addendum G3: this reader builds its own filter list and so does not
        # inherit `_build_filters`' chunk-plane gate. Without the clause a
        # digest section pollutes title autocomplete and contributes a bogus
        # speaker bucket — derived text offered as if somebody had said it.
        chunk_plane_clause(),
        *exclude_files_clauses(exclude_file_uuids),
    ]

    try:
        # Multi-search for title and speaker suggestions
        msearch_body = [
            # Title matches
            {"index": index_name},
            {
                "size": 4,
                "query": {
                    "bool": {
                        "must": [{"match_phrase_prefix": {"title": prefix}}],
                        "filter": scope_filter,
                    }
                },
                "_source": ["title", "file_uuid"],
                "collapse": {"field": "file_uuid"},
            },
            # Speaker matches
            {"index": index_name},
            {
                "size": 0,
                "query": {
                    "bool": {
                        "must": [{"prefix": {"speaker": {"value": prefix.lower()}}}],
                        "filter": scope_filter,
                    }
                },
                "aggs": {"speakers": {"terms": {"field": "speaker", "size": 4}}},
            },
        ]

        response = _client.opensearch_client.msearch(body=msearch_body)

        # Process title matches
        if len(response.get("responses", [])) > 0:
            title_resp = response["responses"][0]
            for hit in title_resp.get("hits", {}).get("hits", []):
                source = hit["_source"]
                suggestions.append(
                    {
                        "type": "title",
                        "text": source["title"],
                        "file_uuid": source.get("file_uuid"),
                    }
                )

        # Process speaker matches
        if len(response.get("responses", [])) > 1:
            speaker_resp = response["responses"][1]
            buckets = speaker_resp.get("aggregations", {}).get("speakers", {}).get("buckets", [])
            for bucket in buckets:
                suggestions.append(
                    {
                        "type": "speaker",
                        "text": bucket["key"],
                        "count": bucket["doc_count"],
                    }
                )

    except Exception as e:
        logger.error(f"Error getting suggestions: {e}")

    return suggestions[:limit]


def get_available_filters(
    user_id: int,
    organization_id: int | None = None,
    is_admin: bool = False,
    exclude_file_uuids: list[str] | None = None,
) -> dict[str, Any]:
    """Return available filter options for the current user.

    Args:
        user_id: Current user ID.
        organization_id: Active org id (None = personal) — tenant gate.
        is_admin: The quarantine review exemption, as on the results page
            (``search.py``'s ``_drop_quarantined_search_hits``). Trashed files are
            never in a facet.
        exclude_file_uuids: The caller's hidden files (issue #1102), left out of
            every facet.

    Returns:
        Dict with speakers, tags, and date_range.

    Raises:
        ServiceUnavailableError: The aggregation query failed.
    """
    if not _client.opensearch_client:
        return {"speakers": [], "tags": [], "date_range": {}}

    index_name = settings.OPENSEARCH_CHUNKS_INDEX

    if not _chunks_index_verified(_client.opensearch_client, index_name):
        return {"speakers": [], "tags": [], "date_range": {}}

    from app.services.search.tenant_scope import org_filter_clauses

    # A facet is an aggregation with no hit list a post-filter could correct, so the takedown
    # and the trash are excluded inside the query (the ACL clause's `availability` term).
    query_filter: list[dict[str, Any]] = [
        access_filter_clause(user_id, organization_id, include_quarantined=is_admin),
        *org_filter_clauses(organization_id),
        # Addendum G3: facet counts are per-document, so
        # digest sections would inflate every speaker and
        # tag bucket by a file-shaped amount.
        chunk_plane_clause(),
        *exclude_files_clauses(exclude_file_uuids),
    ]

    try:
        response = _client.opensearch_client.search(
            index=index_name,
            body={
                "size": 0,
                "query": {"bool": {"filter": query_filter}},
                "aggs": {
                    "speakers": {"terms": {"field": "speaker", "size": 100}},
                    "tags": {"terms": {"field": "tags", "size": 100}},
                    "date_range": {"stats": {"field": "upload_time"}},
                },
            },
        )

        aggs = response.get("aggregations", {})
        speakers = [
            {"name": b["key"], "count": b["doc_count"]}
            for b in aggs.get("speakers", {}).get("buckets", [])
        ]
        tags = [
            {"name": b["key"], "count": b["doc_count"]}
            for b in aggs.get("tags", {}).get("buckets", [])
        ]
        date_stats = aggs.get("date_range", {})

        return {
            "speakers": speakers,
            "tags": tags,
            "date_range": {
                "min": date_stats.get("min_as_string"),
                "max": date_stats.get("max_as_string"),
            },
        }
    except Exception as e:
        # Empty facets are a legitimate answer; an unreachable cluster is not.
        logger.error("Error getting filters: %s", e, exc_info=True)
        raise ServiceUnavailableError("Search filters are temporarily unavailable.") from e
