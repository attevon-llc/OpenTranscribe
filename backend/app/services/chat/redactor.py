"""Re-mask retrieved chunks before they reach an LLM provider.

**The OpenSearch chunk index stores transcript text UNREDACTED.** That is correct
for search (the user searching their own transcripts should find their own words),
but it means retrieval hands back raw text — including anything the redaction
policy says must never leave the deployment.

So chat re-masks every retrieved chunk whenever ``enabled && redact_before_llm``
applies — the same gate condition summarization uses (``tasks/summarization.py``),
with the admin force floor folded in by ``resolve_effective_config``, but resolved
for the **requesting user** rather than the file owner (see ``mask_chunks``).
Masking is read-time only; stored transcripts are never modified.

Primary path: rebuild the chunk from its ``TranscriptSegment`` rows, whose
detection spans are cached in JSONB — sub-millisecond, no detector runs. Fallback
for files whose detection has not completed: mask inline (slower, logged).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)


@dataclass
class MaskedChunk:
    """A chunk whose text is safe to place in a prompt."""

    source: ChunkHit
    content: str
    was_masked: bool = False

    @property
    def file_uuid(self) -> str:
        return self.source.file_uuid

    @property
    def title(self) -> str:
        return self.source.title

    @property
    def speaker(self) -> str | None:
        return self.source.speaker

    @property
    def start_time(self) -> float:
        return self.source.start_time

    @property
    def end_time(self) -> float | None:
        return self.source.end_time

    @property
    def chunk_index(self) -> int:
        return self.source.chunk_index


def _mask_from_segments(db: Session, chunk: ChunkHit, cfg) -> str | None:
    """Rebuild a chunk's text from its segments, masked via cached spans.

    Returns None whenever the cached-span path cannot be trusted, so the caller
    falls back to inline detection rather than sending unmasked text.

    **The redaction_status gate is the important part.** Cached spans only exist
    once detection has finished for the file. Without this check the function
    would happily "mask" a file whose ``redactions`` are still NULL — masking
    nothing and returning the raw text, which the caller would then treat as
    safe. Chat is exactly the surface where you ask about recordings you never
    opened, so unscanned files are the common case, not the edge case.

    Deliberately does NOT use ``transcript_builders.mask_segment_text``: that helper
    swallows masking errors and returns the ORIGINAL text, which is the opposite
    of what this path needs. Exceptions propagate to the caller's fail-closed
    handler instead.
    """
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.redaction.service import RedactionService

    status = db.query(MediaFile.redaction_status).filter(MediaFile.id == chunk.file_id).scalar()
    if status != C.REDACTION_STATUS_DONE:
        # Detection hasn't run (or didn't finish) — there are no spans to apply.
        return None

    end_time = chunk.end_time if chunk.end_time is not None else chunk.start_time
    segments = (
        db.query(TranscriptSegment)
        .filter(
            TranscriptSegment.media_file_id == chunk.file_id,
            TranscriptSegment.end_time >= chunk.start_time,
            TranscriptSegment.start_time <= end_time,
        )
        .order_by(TranscriptSegment.start_time)
        .all()
    )
    if not segments:
        return None

    masked_parts = []
    for segment in segments:
        text = str(segment.text or "")
        if not text:
            continue
        masked, _applied = RedactionService.mask_segment(
            text, segment.redactions or [], segment.words, cfg, set()
        )
        masked_parts.append(masked)

    return " ".join(masked_parts).strip() or None


def _mask_inline(text: str, cfg) -> str:
    """Detect and mask on the fly — for files whose cached spans aren't ready.

    Toxicity classification is skipped: it is the expensive detector and loading
    it on an interactive request would blow the latency budget. PII/profanity
    still run, which is what ``redact_before_llm`` primarily protects.
    """
    try:
        from app.services.redaction.config import detection_config_for_all
        from app.services.redaction.service import RedactionService

        spans, _toxicity = RedactionService.detect_segment_spans(
            text, None, detection_config_for_all(), run_toxicity=False
        )
        masked, _applied = RedactionService.mask_segment(text, spans, None, cfg, set())
        return masked
    except Exception:  # noqa: BLE001
        logger.exception("Inline chunk masking failed; dropping chunk content")
        # Failing closed: an unmaskable chunk must not reach the provider.
        return ""


def mask_chunks(db: Session, chunks: list[ChunkHit], user_id: int) -> list[MaskedChunk]:
    """Apply the REQUESTING user's redact-before-LLM policy to retrieved chunks.

    **This is deliberately asymmetric with summarization, and the asymmetry matters.**
    Summarization resolves the *file owner's* config (``redaction/llm_guard.py``'s
    ``resolve_llm_masking`` reads ``media_file.user_id``), because it processes one
    file on that owner's behalf. Chat retrieves across a whole library of shared
    recordings in a single turn — there is no single owner to resolve — so the policy
    applied is the asker's, with the admin force floor folded in by
    ``resolve_effective_config``. A sharee with a laxer personal policy therefore sees
    chunks masked to *their* policy, not the owner's.

    Anything building a summary tier on top of chat retrieval (#383) has to pick one of
    these two subjects explicitly; inheriting whichever the surrounding code happened to
    use is how the two paths would silently diverge.

    Args:
        db: Database session.
        chunks: Chunks straight out of retrieval (unredacted index content).
        user_id: The requesting user, whose effective policy governs (admin force
            floor included) — NOT the owner of the files the chunks came from.

    Returns:
        Chunks with prompt-safe text. When the policy does not apply, content is
        passed through untouched.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        cfg = resolve_effective_config(db, user_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not resolve redaction config; masking all chunk content")
        # Fail CLOSED: if we cannot tell whether masking is required, don't send text.
        return [MaskedChunk(source=c, content="", was_masked=True) for c in chunks]

    if not (cfg.enabled and cfg.redact_before_llm):
        return [MaskedChunk(source=c, content=c.content) for c in chunks]

    masked: list[MaskedChunk] = []
    inline_fallbacks = 0
    for chunk in chunks:
        try:
            text = _mask_from_segments(db, chunk, cfg)
        except Exception:  # noqa: BLE001
            logger.exception("Cached-span masking failed for chunk; withholding content")
            # Fail CLOSED — an unmaskable chunk contributes nothing.
            masked.append(MaskedChunk(source=chunk, content="", was_masked=True))
            continue

        if text is None:
            inline_fallbacks += 1
            text = _mask_inline(chunk.content, cfg)
        masked.append(MaskedChunk(source=chunk, content=text, was_masked=True))

    if inline_fallbacks:
        logger.info(
            "Chat masking used the inline fallback for %d/%d chunks "
            "(segments unavailable — detection may still be running)",
            inline_fallbacks,
            len(chunks),
        )
    return masked
