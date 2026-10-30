"""Autocomplete suggestions and the filter-options facets, both quarantine-excluded."""

import logging
from typing import Any

from app.core.config import settings
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.opensearch_service import opensearch_client
from app.services.search.access_filter import access_filter_clause
from app.services.search.quarantine_exclusion import _quarantined_file_uuids
from app.services.search.search_infra import _chunks_index_verified

logger = logging.getLogger(__name__)


def get_suggestions(
    prefix: str,
    user_id: int,
    limit: int = 8,
    organization_id: int | None = None,
    is_admin: bool = False,
) -> list[dict[str, Any]]:
    """Get auto-complete suggestions.

    Args:
        prefix: Search prefix text.
        user_id: Current user ID.
        limit: Maximum number of suggestions.
        organization_id: Active org id (None = personal) — tenant gate.
        is_admin: When True, skip the quarantine exclusion on both legs —
            matches the admin review bypass ``get_available_filters`` and
            the results page already apply.

    Returns:
        List of suggestion dicts with type, text, and optional metadata.

    Raises:
        QuarantineExclusionUnavailableError: For a non-admin, the quarantine
            exclusion set could not be resolved completely, so neither the
            title leg nor the speaker leg can be proven free of taken-down
            content. Fail closed — the caller turns this into a 503 rather
            than serving suggestions built from an unproven exclusion.
    """
    if not opensearch_client:
        return []

    index_name = settings.OPENSEARCH_CHUNKS_INDEX

    if not _chunks_index_verified(opensearch_client, index_name):
        return []

    suggestions = []

    from app.services.search.tenant_scope import org_filter_clauses

    scope_filter = [
        access_filter_clause(user_id, organization_id),
        *org_filter_clauses(organization_id),
        # Addendum G3: this reader builds its own filter list and so does not
        # inherit `_build_filters`' chunk-plane gate. Without the clause a
        # digest section pollutes title autocomplete and contributes a bogus
        # speaker bucket — derived text offered as if somebody had said it.
        chunk_plane_clause(),
    ]

    # Quarantine is Postgres-only (`takedown_service.quarantine_file` never
    # touches OpenSearch), so — same reasoning as `get_available_filters` —
    # BOTH legs below need an explicit exclusion, not a field to filter on:
    # the title leg used to be covered only by a DB post-filter in search.py
    # (which this replaces), and the speaker leg carries no file linkage at
    # all, so nothing could ever post-filter it — a quarantined file's
    # speaker names leaked into autocomplete for everyone who had access
    # before the takedown, forever (issue #817). Deliberately OUTSIDE the
    # try below: a QuarantineExclusionUnavailableError must reach the
    # endpoint, not be folded into the empty-suggestions fallback the
    # `except Exception` below returns — same posture as #876.
    quarantined_uuids = [] if is_admin else _quarantined_file_uuids()
    scope_must_not = [{"terms": {"file_uuid": quarantined_uuids}}] if quarantined_uuids else []

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
                        **({"must_not": scope_must_not} if scope_must_not else {}),
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
                        **({"must_not": scope_must_not} if scope_must_not else {}),
                    }
                },
                "aggs": {"speakers": {"terms": {"field": "speaker", "size": 4}}},
            },
        ]

        response = opensearch_client.msearch(body=msearch_body)

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
    user_id: int, organization_id: int | None = None, is_admin: bool = False
) -> dict[str, Any]:
    """Return available filter options for the current user.

    Args:
        user_id: Current user ID.
        organization_id: Active org id (None = personal) — tenant gate.
        is_admin: When True, skip the quarantine exclusion — matches the
            admin review bypass ``search.py``'s ``_drop_quarantined_search_hits``
            already applies on the results page beside this endpoint.

    Returns:
        Dict with speakers, tags, and date_range.

    Raises:
        QuarantineExclusionUnavailableError: For a non-admin, the quarantine
            exclusion set could not be resolved completely, so these facets
            cannot be proven free of taken-down content. Fail closed — the
            caller turns this into a 503 rather than serving the buckets.
    """
    if not opensearch_client:
        return {"speakers": [], "tags": [], "date_range": {}}

    index_name = settings.OPENSEARCH_CHUNKS_INDEX

    if not _chunks_index_verified(opensearch_client, index_name):
        return {"speakers": [], "tags": [], "date_range": {}}

    from app.services.search.tenant_scope import org_filter_clauses

    query_filter: list[dict[str, Any]] = [
        access_filter_clause(user_id, organization_id),
        *org_filter_clauses(organization_id),
        # Addendum G3: facet counts are per-document, so
        # digest sections would inflate every speaker and
        # tag bucket by a file-shaped amount.
        chunk_plane_clause(),
    ]
    query_must_not: list[dict[str, Any]] = []

    # A quarantine (takedown) is Postgres-only — takedown_service.quarantine_file
    # never touches OpenSearch — so without this, a quarantined file's speaker
    # names, tag names and upload-time date range keep appearing in these facets
    # for everyone who had access before the takedown, including the file's own
    # owner (who takedown_service.is_hidden_for says must not see it at all).
    # search.py's search_summaries already post-filters quarantined HITS off a
    # results page; there is no equivalent hit list here to post-filter, since
    # this endpoint returns aggregated buckets, not documents — so the exclusion
    # has to be built into the aggregation query itself. Quarantine is rare and
    # the OpenSearch `terms` clause is naturally bounded, unlike the general
    # accessible-file set, so excluding by quarantined uuid (rather than trying
    # to enumerate every accessible-and-non-quarantined uuid) keeps this cheap.
    # Deliberately OUTSIDE the try below: a QuarantineExclusionUnavailableError
    # must reach the endpoint, not be folded into the empty-facets fallback
    # that the OpenSearch failure path returns (issue #876).
    quarantined_uuids = [] if is_admin else _quarantined_file_uuids()
    if quarantined_uuids:
        query_must_not.append({"terms": {"file_uuid": quarantined_uuids}})

    try:
        response = opensearch_client.search(
            index=index_name,
            body={
                "size": 0,
                "query": {
                    "bool": {
                        "filter": query_filter,
                        **({"must_not": query_must_not} if query_must_not else {}),
                    }
                },
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
        logger.error(f"Error getting filters: {e}")
        return {"speakers": [], "tags": [], "date_range": {}}
