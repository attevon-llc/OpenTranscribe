"""OpenSearch filter clauses for the transcript and summary planes.

:func:`_build_filters` is the single place a search reader gets its ACL term
(``access_filter_clause``), its tenant gate (``org_filter_clauses``) and its plane
clause; every reader of ``transcript_chunks`` that takes a filter list takes this one.
"""

import logging
from typing import Any

from app.core.constants import FILE_TYPE_MIME_PREFIXES
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.search.access_filter import access_filter_clause

logger = logging.getLogger(__name__)


#: OpenSearch's default ``index.max_terms_count``: the largest file set one ``terms`` clause
#: can carry. A collection larger than this falls back to the denormalized field.
_COLLECTION_FILTER_UUID_CAP = 65536


def _collection_filter_clause(collection_id: int) -> dict[str, Any]:
    """The search filter for one collection, from its CURRENT membership in Postgres.

    The chunk documents' ``collection_ids`` is written only when a file is (re)indexed, and
    nothing rewrites it when a file joins or leaves a collection afterwards, so filtering on
    it matched nothing for an AI auto-collection or any "add to collection" made after the
    transcript was indexed (measured on the dev stack: every file's chunks carried
    ``collection_ids: []`` while each sat in two collections). Membership is resolved here
    instead, the same way RAG chat resolves its collection scope (``file_uuids`` above).
    An empty collection, including ``collection_visibility.NO_VISIBLE_COLLECTION``, matches
    nothing. Only past ``_COLLECTION_FILTER_UUID_CAP`` members does it fall back to the field.
    """
    from app.db.session_utils import session_scope
    from app.services.collection_visibility import collection_member_file_uuids

    with session_scope() as db:
        uuids = collection_member_file_uuids(db, collection_id, _COLLECTION_FILTER_UUID_CAP)
    if uuids is None:
        logger.warning(
            "Collection %s has more than %d files; filtering on the indexed collection_ids",
            collection_id,
            _COLLECTION_FILTER_UUID_CAP,
        )
        return {"term": {"collection_ids": collection_id}}
    return {"terms": {"file_uuid": uuids}}


def _append_range_filter(
    filters: list[dict[str, Any]],
    field: str,
    gte_value: Any | None,
    lte_value: Any | None,
) -> None:
    """Append a range filter clause if at least one bound is provided.

    Args:
        filters: List of filter clauses to append to.
        field: OpenSearch field name for the range.
        gte_value: Lower bound (inclusive), or None.
        lte_value: Upper bound (inclusive), or None.
    """
    if gte_value is None and lte_value is None:
        return
    range_clause: dict[str, Any] = {}
    if gte_value is not None:
        range_clause["gte"] = gte_value
    if lte_value is not None:
        range_clause["lte"] = lte_value
    filters.append({"range": {field: range_clause}})


def _file_type_filter_clause(file_type: list[str]) -> dict[str, Any]:
    """Match ``content_type`` against coarse file-type filters (issue #463 lane).

    ``content_type`` stores a FULL MIME type — ``audio/mpeg``, ``video/mp4`` — but
    ``/api/search``'s ``file_type`` query param documents itself as accepting
    ``audio``/``video`` and that is exactly what the SPA sends. The filter this
    replaced, ``{"terms": {"content_type": file_type}}``, compares the literal
    string ``"audio"`` against ``"audio/mpeg"``: it can never match, so every
    file-type filter ever applied silently returned zero results. It went
    unnoticed because the filter is normally combined with a text query that
    still matches plenty on its own — a filter that excludes everything and a
    filter that was never applied look identical from the result page.

    Each requested value becomes a ``prefix`` match on its MIME family. A value
    that is not one of the two known coarse buckets is treated as a literal MIME
    string/prefix instead of being dropped, so a caller that already has a full
    MIME type (``audio/mpeg``) still gets an exact match rather than silently
    losing the filter a second way.

    Args:
        file_type: Non-empty list of coarse buckets and/or literal MIME values.

    Returns:
        A ``bool``/``should`` clause suitable for a ``filter`` array entry.
    """
    should: list[dict[str, Any]] = []
    for value in file_type:
        prefix = FILE_TYPE_MIME_PREFIXES.get(value.lower())
        if prefix:
            should.append({"prefix": {"content_type": prefix}})
        else:
            should.append({"term": {"content_type": value}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _collect_filters_applied(
    speakers: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    file_type: list[str] | None = None,
    collection_id: int | None = None,
    language: str | None = None,
    title_filter: str | None = None,
) -> dict[str, Any]:
    """Build a dict of non-None filter values for the response metadata."""
    candidates: list[tuple[str, Any]] = [
        ("speakers", speakers),
        ("tags", tags),
        ("date_from", date_from),
        ("date_to", date_to),
        ("file_type", file_type),
        ("collection_id", collection_id),
        ("language", language),
        ("title_filter", title_filter),
    ]
    return {key: value for key, value in candidates if value is not None}


def _build_filters(
    user_id: int,
    speakers: list[str] | None,
    tags: list[str] | None,
    date_from: str | None,
    date_to: str | None,
    file_type: list[str] | None = None,
    collection_id: int | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    min_file_size: int | None = None,
    max_file_size: int | None = None,
    language: str | None = None,
    title_filter: str | None = None,
    organization_id: int | None = None,
    file_uuid: str | None = None,
    file_uuids: list[str] | None = None,
    plane_clause: dict[str, Any] | None = None,
    speaker_field: str = "speaker",
) -> list[dict[str, Any]]:
    """Build OpenSearch filter clauses.

    ``speaker_field`` names the keyword field the ``speakers`` filter matches. A
    transcript chunk has one speaker (``speaker``); a summary leaf has none of its own
    and carries the file's roster (``speakers``), so the summary leg passes that.

    The ``accessible_user_ids`` term scopes to the caller; ``organization_id``
    adds the default-deny tenant gate (org term when set, else exclude any
    org-stamped doc). Community-edition invariance: ``organization_id`` is
    always None and docs are org-less, so the personal gate is a no-op.

    ``file_uuid`` scopes results to a single file — used by the in-page
    transcript find bar so it can list every match across the whole
    (paginated) transcript, including segments not yet loaded in the browser.

    ``file_uuids`` scopes to an explicit set of files — used by RAG chat,
    whose scope (files / collections / tags) is resolved to file uuids in
    Postgres first, so share and quarantine semantics are decided there
    rather than trusting the denormalized fields on the OpenSearch doc.
    """
    from app.services.search.tenant_scope import org_filter_clauses

    filters: list[dict[str, Any]] = [access_filter_clause(user_id, organization_id)]
    filters.extend(org_filter_clauses(organization_id))
    # The chunk plane, compatibility-armed, UNLESS a caller passed its own
    # plane clause (issue #963: `search_summary_plane` passes
    # `summary_plane_clause()` here). Search results are somebody's own
    # words; a digest or a summary is derived text and must not surface as
    # if it were a quote (addendum G6 / #963 — neither carries the
    # read-time masking treatment a chunk does).
    filters.append(plane_clause if plane_clause is not None else chunk_plane_clause())

    if file_uuid:
        filters.append({"term": {"file_uuid": file_uuid}})
    if file_uuids is not None:
        # An empty resolved scope must match nothing, NOT everything.
        filters.append({"terms": {"file_uuid": list(file_uuids)}})
    if speakers:
        filters.append({"terms": {speaker_field: speakers}})
    if tags:
        filters.append({"terms": {"tags": tags}})
    if file_type:
        filters.append(_file_type_filter_clause(file_type))
    if collection_id is not None:
        filters.append(_collection_filter_clause(collection_id))
    if language:
        filters.append({"term": {"language": language}})
    if title_filter:
        # Escape wildcard special characters to prevent injection
        escaped = title_filter.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")
        filters.append(
            {"wildcard": {"title": {"value": f"*{escaped.lower()}*", "case_insensitive": True}}}
        )

    # Range filters for date, duration, and file size
    _append_range_filter(filters, "upload_time", date_from, date_to)
    _append_range_filter(filters, "duration", min_duration, max_duration)
    _append_range_filter(filters, "file_size", min_file_size, max_file_size)

    return filters
