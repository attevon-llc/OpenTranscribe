"""The ``availability`` field every per-file OpenSearch document carries (issues #1101, #1253).

A trashed or taken-down (quarantined) file stays in Postgres and keeps its index documents, so
the index has to know: every per-file document (transcript chunks, digests and summary leaves,
the legacy ``transcripts`` document, per-file speaker voiceprints) carries ``availability`` =
:data:`AVAILABLE`, :data:`QUARANTINED` or :data:`TRASHED`, stamped by every writer from the row
(``file_availability.index_availability``) and rewritten in place by ``availability_stamp`` on
trash, restore, quarantine and release.

One keyword, not two: the trash hides a file from everyone and the quarantine only from callers
the review exemption does not reach (``file_availability.unavailable_list_exempt``), so a file
in both states is stamped ``trashed`` and every reader's rule stays one clause.

Readers exclude with ``must_not`` (:func:`exclude_unavailable_clause`), never a positive
``term availability: available``: documents written before the field existed have no value at
all, and "missing" must read as available. The mapping is added to existing indices by an
additive ``put_mapping`` on startup, so deploying this needs no reindex; until the stamp
converges (``search.availability_reconcile``, dispatched once per boot and every 15 minutes),
the Postgres post-filter (``file_availability.unavailable_among``) is the backstop for result
lists.

Why a field and not a Postgres-resolved ``must_not terms`` id list: such a list grows with the
trash or the takedowns until it cannot be sent at all, and it cannot reach the speaker kNN
reads that build their own filters. A per-document keyword costs the same at any size.
"""

from __future__ import annotations

from typing import Any

#: The document field.
AVAILABILITY_FIELD = "availability"

#: The values a writer stamps. Anything else (including a missing field) reads as available.
AVAILABLE = "available"
QUARANTINED = "quarantined"
TRASHED = "trashed"

#: Every value a writer may stamp.
VALUES = (AVAILABLE, QUARANTINED, TRASHED)

#: The field's mapping on every index that carries it. ``keyword``: it is only ever matched
#: exactly, never analysed or scored.
AVAILABILITY_MAPPING: dict[str, Any] = {"type": "keyword"}


def unavailable_values(*, include_quarantined: bool = False) -> list[str]:
    """The values a reader must not see: the trash always, the quarantine unless exempt."""
    return [TRASHED] if include_quarantined else [TRASHED, QUARANTINED]


def unavailable_term(*, include_quarantined: bool = False) -> dict[str, Any]:
    """The query matching the documents a reader must not see.

    Args:
        include_quarantined: The quarantine review exemption, decided by the caller from the
            request's bypass (``file_availability.unavailable_list_exempt``), never from the
            bare admin role. It never exempts the trash.
    """
    return {
        "terms": {AVAILABILITY_FIELD: unavailable_values(include_quarantined=include_quarantined)}
    }


def exclude_unavailable_clause(*, include_quarantined: bool = False) -> dict[str, Any]:
    """A clause for any ``filter`` list that drops trashed and quarantined files' documents.

    ``bool.must_not`` rather than a positive ``term``: a document with no ``availability``
    field (written before the field existed) must stay visible. Filter context, so it does
    not change scores (OpenSearch docs, "Boolean query": ``filter`` and ``must_not`` clauses
    run in filter context). It is a plain clause, not a top-level body change, so it fits
    every query shape in the app: a ``bool.filter`` list, and the ``filter`` of a Lucene
    ``knn`` query (OpenSearch docs, "k-NN search with filters": efficient filtering applies the
    filter during the graph search, so ``k`` neighbours are still returned after exclusion).

    The speaker kNN readers take the default: matching, similarity, clustering and profile
    suggestions are not review surfaces, so a taken-down voice is excluded for every caller.
    """
    return {"bool": {"must_not": [unavailable_term(include_quarantined=include_quarantined)]}}
