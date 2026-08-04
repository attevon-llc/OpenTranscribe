"""Re-mask retrieved chunks before they reach an LLM provider.

**The OpenSearch chunk index stores transcript text UNREDACTED.** That is correct
for search (the user searching their own transcripts should find their own words),
but it means retrieval hands back raw text — including anything the owner's
redaction policy says must never leave the deployment.

So chat re-masks every retrieved chunk whenever ``enabled && redact_before_llm``
applies, using exactly the gate summarization uses (``tasks/summarization.py``),
with the admin force floor folded in by ``resolve_effective_config``. Masking is
read-time only; stored transcripts are never modified.

Primary path: rebuild the chunk from its ``TranscriptSegment`` rows, whose
detection spans are cached in JSONB — sub-millisecond, no detector runs. Fallback
for files whose detection has not completed: mask inline (slower, logged).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

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

    Returns None when the chunk can't be reconstructed (no overlapping segments),
    so the caller can fall back rather than silently sending unmasked text.
    """
    from app.models.media import TranscriptSegment
    from app.utils.transcript_builders import _seg_text

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

    return " ".join(_seg_text(segment, cfg) for segment in segments).strip() or None


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
    """Apply the owner's redact-before-LLM policy to retrieved chunks.

    Args:
        db: Database session.
        chunks: Chunks straight out of retrieval (unredacted index content).
        user_id: Owner whose effective policy governs (admin force floor included).

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
        text = _mask_from_segments(db, chunk, cfg)
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
