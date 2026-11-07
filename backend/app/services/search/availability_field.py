"""The ``availability`` field every per-file OpenSearch document carries (issue #1101).

A trashed file stays in Postgres and keeps its index documents until it is purged, so the
index has to know it is trashed: every per-file document (transcript chunks, digests and
summary leaves, the legacy ``transcripts`` document, per-file speaker voiceprints) carries
``availability`` = :data:`AVAILABLE` or :data:`TRASHED`, stamped by every writer from the
row and rewritten in place by ``availability_stamp`` on trash and restore.

Readers exclude with ``must_not term availability: trashed`` (:func:`exclude_unavailable_clause`),
never ``term availability: available``: documents written before the field existed have no
value at all, and "missing" must read as available. The mapping is added to existing indices
by an additive ``put_mapping`` on startup, so deploying this needs no reindex; until the
backfill/stamp converges, the Postgres post-filter (``file_availability.unavailable_among``)
is the backstop.

Why a field and not the quarantine exclusion list (``quarantine_exclusion``): that list is a
Postgres-resolved ``must_not terms`` set capped at 10,000 ids that fails closed with a 503 past
the cap. An organization trash routinely holds more files than that for its 30-day window, so
the list approach would put search into a permanent 503. A per-document keyword costs the
same at any trash size.
"""

from __future__ import annotations

from typing import Any

#: The document field.
AVAILABILITY_FIELD = "availability"

#: The two values a writer stamps. Anything else (including a missing field) reads as available.
AVAILABLE = "available"
TRASHED = "trashed"

#: The field's mapping on every index that carries it. ``keyword``: it is only ever matched
#: exactly, never analysed or scored.
AVAILABILITY_MAPPING: dict[str, Any] = {"type": "keyword"}


def trashed_term() -> dict[str, Any]:
    """The term matching a trashed file's documents."""
    return {"term": {AVAILABILITY_FIELD: TRASHED}}


def exclude_unavailable_clause() -> dict[str, Any]:
    """A clause for any ``filter`` list that drops trashed files' documents.

    ``bool.must_not`` rather than a positive ``term``: a document with no ``availability``
    field (written before the field existed) must stay visible. Filter context, so it does
    not change scores (OpenSearch docs, "Boolean query": ``filter`` and ``must_not`` clauses
    run in filter context). It is a plain clause, not a top-level body change, so it fits
    every query shape in the app: a ``bool.filter`` list, and the ``filter`` of a Lucene
    ``knn`` query (OpenSearch docs, "k-NN search with filters": efficient filtering applies the
    filter during the graph search, so ``k`` neighbours are still returned after exclusion).
    """
    return {"bool": {"must_not": [trashed_term()]}}
