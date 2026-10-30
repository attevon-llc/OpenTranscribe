"""Phase A of :func:`~app.services.chat.redactor.mask_chunks`: gather each chunk's inputs.

Postgres reads only, no masking. For every retrieved chunk this resolves the chunk's own
STRICTEST-WINS effective policy (task #40, :func:`_effective_cfg_for_owner`, shared with
:mod:`app.services.chat.redactor_digest_plans`) and reads the transcript segments backing it,
returning plain data (:class:`_SegmentSpans`, :class:`_ChunkPlan`), never an ORM row. The
masking half, and every decision about what a plan means for the text, is
:mod:`app.services.chat.redactor`; see its module docstring and this package's CLAUDE.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core import redaction_defaults
from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SegmentSpans:
    """One transcript segment's text and its cached detection spans, as plain data.

    Read inside the gather session and carried out of it. Deliberately **not** a
    ``TranscriptSegment``: the first attribute read on a detached ORM row would
    open a fresh transaction in the masking phase, which is the whole thing this
    split exists to prevent.
    """

    text: str
    redactions: list[Any]
    words: Any


@dataclass
class _ChunkPlan:
    """What the gather learned about one chunk.

    ``cfg`` is this chunk's own EFFECTIVE (strictest-wins) policy —
    ``union(owner, requester)``, task #40 — resolved once per distinct file
    during gather and shared by every chunk from that file. ``applies`` is
    whether that effective policy calls for masking THIS chunk at all: when
    False the chunk passes through untouched (neither side wants it masked),
    distinct from ``segments is None``, which means masking DOES apply but the
    cached-span path declined (no scan, an unfinished scan, a coverage gap, or
    no overlapping segments) and the chunk must be masked inline instead.
    ``failed`` means the gather itself raised, and the chunk fails closed
    **without** an inline retry — exactly as before the split, where an
    exception out of the cached path appended ``""`` and moved on.
    """

    segments: list[_SegmentSpans] | None = None
    failed: bool = False
    cfg: Any = None
    applies: bool = False


def _effective_cfg_for_owner(
    db: Session,
    owner_id: Any,
    requester_user_id: int,
    requester_cfg: Any,
    owner_cache: dict[Any, Any],
    owner_organization_id: int | None = None,
) -> Any:
    """Strictest-wins union of ``requester_cfg`` with the file owner's own policy.

    Task #40. ``owner_id`` is whatever the scan row's ``user_id`` column held (or
    ``None`` when the row itself was missing) — read structurally, never assumed
    to be an ``int``, so a duck-typed test double stays exercised the same way
    the real column does.

    ``owner_organization_id`` is the SAME scan row's ``organization_id`` — the
    file's own tenant, not necessarily the requester's active org (a sharee can
    read across orgs). Threaded into the owner's ``resolve_effective_config``
    call so a per-org redaction floor (issue #982/#987) actually applies to the
    owner side of the union (#988); the requester side already carries its own
    org via ``requester_cfg``, resolved by the caller.

    Fails CLOSED to :func:`~app.services.redaction.config.most_restrictive_config`
    whenever the owner cannot be identified at all, or their own policy cannot be
    read — a DB error, a missing row, or (defensively) a deleted owner. That is
    deliberately NOT the same outcome as "owner has no opinion": unioning with an
    absent policy must never look like unioning with a permissive one, because the
    two are indistinguishable to a reader of the masked output.

    Args:
        db: The single short-lived session this gather call already holds.
        owner_id: The file's ``user_id`` column, or ``None``.
        requester_user_id: The turn's own requesting user — used only to skip a
            redundant resolve when the requester also owns the file (the common
            case), not to change the outcome: resolving twice would return an
            equal config anyway.
        requester_cfg: The already-resolved requester policy.
        owner_cache: Memoizes one owner id's resolved policy across every
            chunk/digest from the same file within this ``_gather`` call.

    Returns:
        ``union_effective_config(requester_cfg, owner_cfg)`` — see that
        function's docstring for exactly what "union" means per field, and for
        why ``a is b`` (the common case here) returns the shared config
        unchanged rather than an equal reconstruction.
    """
    from app.services.redaction.config import most_restrictive_config
    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.config import union_effective_config

    if owner_id is None:
        owner_cfg = most_restrictive_config()
    elif owner_id == requester_user_id:
        # Self-owned file: the requester's own already-resolved config IS the
        # owner's, so `union_effective_config`'s `a is b` shortcut applies for
        # free and no second resolve is needed.
        owner_cfg = requester_cfg
    elif owner_id in owner_cache:
        owner_cfg = owner_cache[owner_id]
    else:
        try:
            owner_cfg = resolve_effective_config(
                db, owner_id, organization_id=owner_organization_id
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not resolve the file owner's redaction policy for chat "
                "egress masking; failing closed to the most restrictive policy"
            )
            owner_cfg = most_restrictive_config()
        owner_cache[owner_id] = owner_cfg

    return union_effective_config(requester_cfg, owner_cfg)


# --------------------------------------------------------------------------- #
# Phase A — Postgres. Everything below runs with a session and returns plain data.
# --------------------------------------------------------------------------- #


def _segment_spans(row) -> _SegmentSpans:
    """Copy one segment row's masking inputs out of the ORM."""
    return _SegmentSpans(
        text=str(row.text or ""),
        redactions=list(row.redactions or []),
        words=row.words,
    )


def _gather_chunk_segments(
    db: Session,
    chunk: ChunkHit,
    requester_user_id: int,
    requester_cfg,
    owner_cache: dict[Any, Any],
) -> tuple[list[_SegmentSpans] | None, Any]:
    """Read the segments backing one chunk, or None to mean "mask inline".

    Returns ``(segments_or_None, effective_cfg)`` — the SAME query that answers
    the coverage gate also carries the file's ``user_id``, so this chunk's
    strictest-wins effective policy (task #40, ``union(owner, requester)``) is
    resolved from that ONE row rather than a second round trip.

    **The redaction_status gate is the important part.** Cached spans only exist
    once detection has finished for the file. Without this check the caller
    would happily "mask" a file whose ``redactions`` are still NULL — masking
    nothing and returning the raw text, which it would then treat as safe. Chat
    is exactly the surface where you ask about recordings you never opened, so
    unscanned files are the common case, not the edge case.

    Deliberately does NOT use ``transcript_builders.mask_segment_text``: that helper
    swallows masking errors and returns the ORIGINAL text, which is the opposite
    of what this path needs. Exceptions propagate to the caller's fail-closed
    handler instead.
    """
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.redaction.coverage import uncovered_detectors

    # Six columns rather than the ORM row: `uncovered_detectors` reads
    # `redaction_coverage` and `language` by getattr, and a Row exposes both by
    # label. Keeping this a plain Row also honours the phase-4 rule that nothing
    # ORM-shaped escapes the masking session (see this package's CLAUDE.md).
    # `user_id`/`organization_id` are read the same way, for the owner side of
    # the strictest-wins union (#988 threads the latter into that resolve) —
    # never a second query for either.
    scan = (
        db.query(
            MediaFile.id,
            MediaFile.redaction_status,
            MediaFile.redaction_coverage,
            MediaFile.language,
            MediaFile.user_id,
            MediaFile.organization_id,
        )
        .filter(MediaFile.id == chunk.file_id)
        .first()
    )
    effective = _effective_cfg_for_owner(
        db,
        getattr(scan, "user_id", None),
        requester_user_id,
        requester_cfg,
        owner_cache,
        getattr(scan, "organization_id", None),
    )
    if scan is None or scan.redaction_status != redaction_defaults.REDACTION_STATUS_DONE:
        # Detection hasn't run (or didn't finish) — there are no spans to apply.
        return None, effective

    # `done` says the scan FINISHED, not that it LOOKED (v392). An unavailable
    # detector resolves to `done` + `skipped_detectors` rather than `failed`,
    # deliberately, so trusting the status alone here would serve cached spans
    # from a scan whose PII detector never ran — masking nothing and returning
    # raw text the caller treats as safe. Returning None falls through to
    # `_mask_inline`, which fails closed.
    #
    # `effective` is the STRICTEST-WINS union (task #40) of the file owner's
    # policy and the requesting user's — never one subject alone: one chat turn
    # retrieves across a library of shared recordings with no single owner, and
    # a sharee's more permissive policy must never unmask what the owner hides.
    gap = uncovered_detectors(scan, effective)
    if gap:
        logger.warning(
            "Cached spans do not cover %s for file %s; masking inline instead",
            sorted(gap),
            chunk.file_id,
        )
        return None, effective

    end_time = chunk.end_time if chunk.end_time is not None else chunk.start_time
    segments = (
        db.query(TranscriptSegment)
        .filter(
            TranscriptSegment.media_file_id == chunk.file_id,
            TranscriptSegment.end_time >= chunk.start_time,
            TranscriptSegment.start_time <= end_time,
        )
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
        .all()
    )
    if not segments:
        return None, effective
    return [_segment_spans(segment) for segment in segments], effective


def _gather_chunk_plans(
    db: Session, chunks: list[ChunkHit], requester_user_id: int, requester_cfg
) -> list[_ChunkPlan]:
    """Phase A for :func:`mask_chunks`. One failed read withholds one chunk."""
    owner_cache: dict[Any, Any] = {}
    plans: list[_ChunkPlan] = []
    for chunk in chunks:
        try:
            # Returns ``_SegmentSpans`` (plain data), so the masking phase in
            # :func:`_apply_chunk_plan` holds no session.
            segments, effective = _gather_chunk_segments(
                db, chunk, requester_user_id, requester_cfg, owner_cache
            )
        except Exception:  # noqa: BLE001
            logger.exception("Cached-span lookup failed for chunk; withholding content")
            # Fail CLOSED — an unmaskable chunk contributes nothing. The effective
            # policy could not be resolved either, so masking is treated as
            # applying (never "we couldn't tell, so pass it through").
            plans.append(_ChunkPlan(failed=True, cfg=requester_cfg, applies=True))
            continue
        applies = bool(
            getattr(effective, "enabled", False) and getattr(effective, "redact_before_llm", False)
        )
        plans.append(_ChunkPlan(segments=segments, cfg=effective, applies=applies))
    return plans
