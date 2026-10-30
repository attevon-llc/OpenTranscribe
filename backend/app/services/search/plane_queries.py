"""Per-plane OpenSearch query builders for the chunks index (chunk, digest, file, summary).

Every delete and every targeted rewrite against the chunks index is built here and nowhere
else, so the ``doc_type`` discriminator cannot be forgotten at a call site.
"""

from typing import Any

from app.services.ingest_artifacts import index_mapping as digest_mapping


def chunk_plane_query(
    file_uuid: str,
    *,
    from_chunk_index: int | None = None,
    extra_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the query that selects the CHUNK-plane documents of one file.

    **Every delete and every targeted rewrite against the chunks index is built
    here, or in its two siblings below, and nowhere else.**
    Since v6 the index also holds per-file **digest** documents
    (``doc_type: digest``), so a predicate matching only on ``file_uuid`` takes
    the digests with it — which is why the discriminator lives here rather than
    at each call site.

    The clause is :func:`~app.services.ingest_artifacts.index_mapping.chunk_plane_clause`,
    not a bare ``{"term": {"doc_type": "chunk"}}``, and the difference is the
    whole point: **every chunk written before v6 carries no ``doc_type`` at
    all**, so a bare term matches none of them and the #400 stale-tail prune
    silently stops working for an entire installed corpus. An explicit mapping
    does nothing for documents written before it existed.

    ``extra_filters`` exists so a caller that needs to narrow *within* the chunk
    plane (the rename propagation of issue #405 only wants the docs still
    carrying the old speaker name) still inherits whatever predicate this
    function grows.

    Args:
        file_uuid: UUID of the media file whose chunks are being selected.
        from_chunk_index: When set, restrict to chunks at or above this index —
            the stale tail left behind by a longer previous chunking.
        extra_filters: Additional ``filter`` clauses ANDed onto the predicate.
            Express an OR as one nested ``bool``/``should`` clause.

    Returns:
        An OpenSearch query body fragment (the value of ``"query"``).
    """
    filters: list[dict[str, Any]] = [
        {"term": {"file_uuid": file_uuid}},
        digest_mapping.chunk_plane_clause(),
    ]
    if from_chunk_index is not None:
        filters.append({"range": {"chunk_index": {"gte": from_chunk_index}}})
    if extra_filters:
        filters.extend(extra_filters)
    return {"bool": {"filter": filters}}


def digest_plane_query(
    file_uuid: str,
    *,
    from_section: int | None = None,
) -> dict[str, Any]:
    """The digest-plane sibling of :func:`chunk_plane_query`.

    No compatibility arm: digest documents are all new, so ``doc_type`` is
    always present on them.

    Args:
        file_uuid: UUID of the media file whose digest sections are selected.
        from_section: When set, restrict to sections at or above this index —
            the orphans left behind when a digest re-sections to fewer parts,
            exactly the way a shorter re-chunk orphans a chunk tail (#400).

    Returns:
        An OpenSearch query body fragment.
    """
    filters: list[dict[str, Any]] = [
        {"term": {"file_uuid": file_uuid}},
        digest_mapping.digest_plane_clause(),
    ]
    if from_section is not None:
        filters.append({"range": {"digest_section": {"gte": from_section}}})
    return {"bool": {"filter": filters}}


def file_plane_query(file_uuid: str) -> dict[str, Any]:
    """**Every** document of one file, whatever plane it belongs to.

    Deliberately has no ``doc_type`` predicate, and all three callers need that:
    deleting a media file must leave nothing behind, a full rebuild wants a clean
    slate before it regenerates both planes, and
    :meth:`TranscriptIndexingService.count_file_documents` verifies the delete
    against the same predicate the delete used. Using :func:`chunk_plane_query`
    for any of them would strand the digests — readable, with whatever ACL they
    were last stamped with.

    Args:
        file_uuid: UUID of the media file.

    Returns:
        An OpenSearch query body fragment.
    """
    return {"bool": {"filter": [{"term": {"file_uuid": file_uuid}}]}}


def summary_plane_query(
    file_uuid: str,
    *,
    from_leaf: int | None = None,
) -> dict[str, Any]:
    """The summary-plane sibling of :func:`chunk_plane_query` (issue #963).

    No compatibility arm: summary documents are all new, so ``doc_type`` is
    always present on them.

    Args:
        file_uuid: UUID of the media file whose summary leaves are selected.
        from_leaf: When set, restrict to leaves at or above this index — the
            orphans left behind when a re-summarization produces fewer
            leaves, exactly the way a shorter re-chunk orphans a chunk tail
            (#400) and a shorter re-sectioning orphans a digest tail.

    Returns:
        An OpenSearch query body fragment.
    """
    filters: list[dict[str, Any]] = [
        {"term": {"file_uuid": file_uuid}},
        digest_mapping.summary_plane_clause(),
    ]
    if from_leaf is not None:
        filters.append({"range": {"summary_leaf_index": {"gte": from_leaf}}})
    return {"bool": {"filter": filters}}
