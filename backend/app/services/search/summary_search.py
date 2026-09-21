"""OpenSearch summary-plane search (issue #963).

A summary lives canonically in ``media_file.summary_data`` (#67 retired the
``transcript_summaries`` OpenSearch index). Since #963 it is ALSO indexed, as a
**derived, rebuildable plane** inside the existing ``transcript_chunks`` index
(``doc_type: "summary"``, :func:`~app.services.ingest_artifacts.index_mapping.summary_plane_clause`)
— never a second store of record. Nothing here writes to Postgres, and nothing
in the write path (``TranscriptIndexingService._index_summary_plane``) accepts
a summary payload as an argument: it reads ``media_file.summary_data`` itself,
on its own session, which is what makes the #67 shape ("the same dict written
to the column and to the index in one task") structurally impossible here.

This module used to run ``websearch_to_tsquery`` directly against Postgres.
That query engine cannot express semantic (embedding) similarity and its
``ts_rank`` has no meaningful cross-leg relevance signal; #963 replaces it with
:meth:`~app.services.search.hybrid_search_service.HybridSearchService.search_summary_plane`,
a real BM25 + kNN hybrid query fused by the same RRF pipeline the transcript
leg uses, against the same index, through the same ACL/tenant filter builder
(``HybridSearchService._build_filters``) — one place for that rule, not two.

Matching happens against the RAW (unredacted) indexed leaf text, exactly the
precedent ``hybrid_search_service.py`` sets for transcript chunks: masking
happens only at snippet time, per leaf actually returned, under the
*requesting* user's policy (never the file owner's — the #85 read-surface
rule). Index-time masking was considered and rejected: one index copy cannot
carry N users' policies, and it would make a user's own masked content
unfindable by them.

⚠️ ``unmask_for_local`` / ``is_local_provider`` have NO bearing here, and never
should. Those key the **egress** question ("may this text be sent to a model
that runs off this host?") off where an LLM runs. This is a **display**
surface — no model is involved in serving a search result — so there is no
egress event to exempt. Do not thread local-provider logic into this module.

Quarantine (abuse/DMCA takedown) is resolved from Postgres by the caller
(``hybrid_search_service._quarantined_file_uuids``, fail-closed) and applied
as an OpenSearch PRE-filter inside ``search_summary_plane`` — not a post-filter
on the hit list — because a post-filter cannot correct total/offset and a
count that includes a taken-down file is a content oracle (issue #818, same
class as #876's ``/search/count``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.summary_redaction import _UNMASKED_TOP_LEVEL_KEYS
from app.services.redaction.summary_redaction import mask_summary_leaf

logger = logging.getLogger(__name__)

#: Longest snippet returned for one matching leaf. Summary sections are
#: normally a sentence or two; this only guards against a pathological custom
#: prompt producing a very long leaf.
_MAX_SNIPPET_CHARS = 400


@dataclass
class SummarySectionMatch:
    """One matching leaf inside a summary, identified by its JSON key-path.

    ``key_path`` is a JS/Python-style path (``major_topics[0].key_points[2]``)
    so the frontend can walk ``summary_data`` with it directly to scroll to the
    matching section — no separate id scheme to keep in sync with the summary
    renderer. ``score`` is the OpenSearch RRF fusion score for this leaf
    (issue #831 item 2) — never comparable across legs.
    """

    key_path: str
    snippet: str
    score: float = 0.0


@dataclass
class SummaryHit:
    """A file-level summary search result."""

    file_uuid: str
    file_id: int
    title: str
    matches: list[SummarySectionMatch] = field(default_factory=list)


@dataclass
class SummarySearchResult:
    """A page of summary search results."""

    results: list[SummaryHit]
    total: int


def walk_summary_leaves(node: Any, path: str) -> list[tuple[str, str]]:
    """Recursively collect ``(key_path, text)`` for every string leaf.

    **The single walker.** Both the OpenSearch document builder
    (``ingest_artifacts.index_mapping.build_summary_documents``) and this
    module's redaction/snippet path import this function rather than
    restating it — two walkers would be two chances for the ``key_path``
    format (a frontend wire contract, ``frontend/src/lib/utils/summaryKeyPath.ts``)
    to disagree.

    Mirrors ``summary_redaction._mask_node``'s walk (dict / list / string
    leaf), but also emits the path alongside the leaf, and skips
    ``_UNMASKED_TOP_LEVEL_KEYS`` (machine-generated ``metadata`` — provider,
    model, timings — is not searchable content), only at the top level.
    """
    if isinstance(node, str):
        return [(path, node)] if node.strip() else []
    if isinstance(node, dict):
        out: list[tuple[str, str]] = []
        for key, value in node.items():
            if path == "" and key in _UNMASKED_TOP_LEVEL_KEYS:
                continue
            child_path = f"{path}.{key}" if path else key
            out.extend(walk_summary_leaves(value, child_path))
        return out
    if isinstance(node, list):
        out = []
        for index, item in enumerate(node):
            out.extend(walk_summary_leaves(item, f"{path}[{index}]"))
        return out
    return []


def _snippet(text_value: str) -> str:
    if len(text_value) <= _MAX_SNIPPET_CHARS:
        return text_value
    return text_value[:_MAX_SNIPPET_CHARS].rstrip() + "…"


def search_summaries(
    db: Session,
    query: str,
    user_id: int,
    *,
    organization_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
    redaction_cfg: EffectiveRedactionConfig | None = None,
    include_quarantined: bool = False,
    speakers: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    file_type: list[str] | None = None,
    collection_id: int | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    min_file_size: int | None = None,
    max_file_size: int | None = None,
    language: str | None = None,
    title_filter: str | None = None,
    file_uuid: str | None = None,
) -> SummarySearchResult:
    """Hybrid (BM25 + kNN) search over the OpenSearch summary plane (issue #963).

    ``db`` is accepted for call-site symmetry with the endpoint (which already
    holds a session for redaction-config resolution) but is not read here:
    quarantine exclusion (``hybrid_search_service._quarantined_file_uuids``)
    deliberately opens its OWN short session rather than borrowing this one,
    and access control itself is enforced by
    :meth:`HybridSearchService._build_filters`'s ``accessible_user_ids``/org
    clauses inside the OpenSearch query — the same authority the transcript
    leg uses, never a second SQL predicate here.

    Filter kwargs (``speakers``, ``tags``, ``date_from``, …) are forwarded
    straight into ``HybridSearchService.search_summary_plane`` /
    ``_build_filters`` — the SAME filter builder the transcript leg uses, so
    the two legs of one ``/api/search`` request can no longer disagree about
    which files were asked for (closes issue #831's whole defect class:
    there is one ``_build_filters``, not a second SQL implementation of it).
    ``date_from``/``date_to`` are raw strings; OpenSearch parses them (no
    400-on-bad-date arm — a documented behaviour change from the retired
    Postgres leg, which used ``parse_date_bound``).

    Returns:
        A page of file-level hits, each carrying every matching leaf's
        key-path, (masked) snippet text, and its RRF score. ``total`` counts
        distinct files within the fused ``SEARCH_RRF_WINDOW_SIZE`` window —
        the same kind of number ``total_files`` already is for the transcript
        leg, not an exact ``COUNT(*)`` (a wire-semantics change from the
        retired Postgres leg, which counted exactly).

    Raises:
        SummaryMaskingUnavailableError: propagated from ``mask_summary_leaf``
            when a detector feeding one of the caller's enabled categories
            could not run on a leaf about to be returned. The caller must
            fail closed (503), not fall back to the unmasked summary. A
            detector outage on a leaf that is not returned does not raise
            this (issue #822's narrowing, preserved).
        QuarantineExclusionUnavailableError: the quarantine exclusion set
            could not be resolved from Postgres. The caller must fail closed
            (503) rather than silently search without the exclusion.
    """
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.hybrid_search_service import _quarantined_file_uuids

    quarantined = [] if include_quarantined else _quarantined_file_uuids()

    service = HybridSearchService()
    raw_hits, total = service.search_summary_plane(
        query,
        user_id,
        organization_id=organization_id,
        page=page,
        page_size=page_size,
        speakers=speakers,
        tags=tags,
        date_from=date_from,
        date_to=date_to,
        file_type=file_type,
        collection_id=collection_id,
        min_duration=min_duration,
        max_duration=max_duration,
        min_file_size=min_file_size,
        max_file_size=max_file_size,
        language=language,
        title_filter=title_filter,
        file_uuid=file_uuid,
        quarantined_file_uuids=quarantined,
    )

    results: list[SummaryHit] = []
    for hit in raw_hits:
        # Mask only the leaves actually returned (issue #822), never batched
        # (issue: batching leaks repeated names across leaves — see
        # `redaction/summary_redaction.py`'s module docstring).
        matches = [
            SummarySectionMatch(
                key_path=m["key_path"],
                snippet=_snippet(
                    mask_summary_leaf(m["content"], redaction_cfg)
                    if redaction_cfg is not None
                    else m["content"]
                ),
                score=m["score"],
            )
            for m in hit["matches"]
        ]
        results.append(
            SummaryHit(
                file_uuid=str(hit["file_uuid"]),
                file_id=int(hit["file_id"]) if hit["file_id"] is not None else 0,
                title=hit["title"],
                matches=matches,
            )
        )

    return SummarySearchResult(results=results, total=total)
