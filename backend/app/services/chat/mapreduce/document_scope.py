"""The document arm of the #403 Stage-6 mixed-collection gate.

Split out of the former single-file ``mapreduce.py``. Kept as its own module
because it is the one piece of the map that talks to the document plane
(``services.ingest_artifacts.scope``) rather than the media/recording plane —
a natural seam, and a small one.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def document_scope_hits(db, file_uuids: list[str], sections_per_file: int) -> tuple[list[Any], int]:
    """The document arm of the #403 Stage-6 mixed-collection gate.

    Delegates the join to :func:`ingest_artifacts.scope.scope_facts_for_uuids`
    — it already gets the "outer join, not inner" and the
    ``document -> file_facts.document_id`` join right — rather than restating
    that logic a second time, and converts its ``document``-kind hits into the
    same :class:`ChunkHit` shape the media arm produces (in
    ``file_summaries.scope_digest_hits``), tagged ``source_kind="document"``
    (:attr:`ChunkHit.is_document`) so every reader downstream — ``mask_digests``
    chief among them — can tell the two apart.

    Documents have no speaker roster or duration (unlike a recording's
    ``FileSummary.speakers``/``duration``); ``file_summaries.build_file_summaries``
    already tolerates an empty roster/None duration for exactly this reason,
    and ``#464``'s LLM-summary tiering (``use_summaries``) is media-only —
    ``Document`` carries no ``summary_data``/``summary_status`` at all, so
    this arm always falls through to the digest sections.

    Returns:
        ``(hits, files_without_artifacts)`` — never raises; a read failure
        degrades to ``([], len(file_uuids))`` so one broken arm cannot take
        down a map that the media half already answered.
    """
    from app.services.ingest_artifacts.scope import scope_facts_for_uuids
    from app.services.search.chunk_retrieval import ChunkHit

    try:
        coverage = scope_facts_for_uuids(db, file_uuids)
    except Exception:  # noqa: BLE001 — the document half degrades, never breaks the turn
        logger.exception("Could not read file_facts for the document half of the scope map")
        return [], len(file_uuids)

    hits: list[Any] = []
    for hit in coverage.hits:
        if hit.kind != "document":
            # This arm is only ever called with uuids the media query above
            # did NOT match, so a "media"-kind hit here should not occur —
            # skip defensively rather than assume the caller's input was
            # constructed correctly.
            continue
        sections = (hit.digest or {}).get("sections", [])
        for section in sections[:sections_per_file]:
            hits.append(
                ChunkHit(
                    file_uuid=hit.uuid,
                    file_id=hit.source_id,
                    chunk_index=-1 - int(section.get("index", 0)),
                    content=str(section.get("text") or ""),
                    title=hit.title,
                    start_time=float(section.get("start_time") or 0.0),
                    end_time=section.get("end_time"),
                    digest_section=int(section.get("index", 0)),
                    source_kind="document",
                )
            )
    return hits, coverage.files_without_artifacts


# Backward-compatible private alias — the pre-split module exposed this
# function as `_document_scope_hits`, and both `mapreduce/__init__.py` and a
# handful of tests import it by that name directly.
_document_scope_hits = document_scope_hits
