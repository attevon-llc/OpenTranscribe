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

from app.core import constants as C  # noqa: N812
from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)


@dataclass
class MaskedChunk:
    """A chunk whose text is safe to place in a prompt.

    ``was_masked`` records that the redaction policy **applied** to this chunk,
    not that anything was found in it — a chunk that failed closed carries
    ``content == ""`` and ``was_masked=True``, because the policy is precisely
    what emptied it. The safety signal is ``content``; nothing may infer
    "this text is masked" from the flag alone.
    """

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
    from app.services.redaction.coverage import uncovered_detectors
    from app.services.redaction.service import RedactionService

    # Four columns rather than the ORM row: `uncovered_detectors` reads
    # `redaction_coverage` and `language` by getattr, and a Row exposes both by
    # label. Keeping this a plain Row also honours the phase-4 rule that nothing
    # ORM-shaped escapes the masking session (see this package's CLAUDE.md).
    scan = (
        db.query(
            MediaFile.id,
            MediaFile.redaction_status,
            MediaFile.redaction_coverage,
            MediaFile.language,
        )
        .filter(MediaFile.id == chunk.file_id)
        .first()
    )
    if scan is None or scan.redaction_status != C.REDACTION_STATUS_DONE:
        # Detection hasn't run (or didn't finish) — there are no spans to apply.
        return None

    # `done` says the scan FINISHED, not that it LOOKED (v392). An unavailable
    # detector resolves to `done` + `skipped_detectors` rather than `failed`,
    # deliberately, so trusting the status alone here would serve cached spans
    # from a scan whose PII detector never ran — masking nothing and returning
    # raw text the caller treats as safe. Returning None falls through to
    # `_mask_inline`, which fails closed.
    #
    # `cfg` is the REQUESTING USER's policy, not the file owner's: one chat turn
    # retrieves across a library of shared recordings with no single owner. That
    # is the opposite subject from `llm_guard`, and deliberately so.
    gap = uncovered_detectors(scan, cfg)
    if gap:
        logger.warning(
            "Cached spans do not cover %s for file %s; masking inline instead",
            sorted(gap),
            chunk.file_id,
        )
        return None

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


def _mask_from_document_chunk(db: Session, chunk: ChunkHit, cfg) -> str | None:
    """The document analog of :func:`_mask_from_segments`.

    Simpler than the transcript case by construction: a ``document_chunk`` row
    already **is** the retrieval unit indexed into OpenSearch (1:1) — there is no
    "rebuild from several overlapping rows" step, just a direct lookup by
    ``(document_id, chunk_index)`` and a read of that row's own cached spans.

    Returns ``None`` whenever the cached-span path cannot be trusted (mirrors
    ``_mask_from_segments`` exactly: unscanned, or scanned with a coverage gap
    against this policy), so the caller falls back to inline detection.
    """
    from app.models.document import Document
    from app.models.document import DocumentChunk
    from app.services.redaction.coverage import uncovered_detectors

    scan = (
        db.query(
            Document.id,
            Document.redaction_status,
            Document.redaction_coverage,
            Document.language,
        )
        .filter(Document.id == chunk.file_id)
        .first()
    )
    if scan is None or scan.redaction_status != C.REDACTION_STATUS_DONE:
        return None

    gap = uncovered_detectors(scan, cfg)
    if gap:
        logger.warning(
            "Cached spans do not cover %s for document %s; masking inline instead",
            sorted(gap),
            chunk.file_id,
        )
        return None

    from app.services.redaction.service import RedactionService

    row = (
        db.query(DocumentChunk.text, DocumentChunk.redactions)
        .filter(
            DocumentChunk.document_id == chunk.file_id,
            DocumentChunk.chunk_index == chunk.chunk_index,
        )
        .first()
    )
    if row is None or not row.text:
        return None

    masked, _applied = RedactionService.mask_segment(
        row.text, row.redactions or [], None, cfg, set()
    )
    return masked or None


def _mask_one_document_chunk(db: Session, chunk: ChunkHit, cfg) -> MaskedChunk:
    """Fail-closed wrapper mirroring ``mask_chunks``'s per-chunk loop body."""
    try:
        text = _mask_from_document_chunk(db, chunk, cfg)
    except Exception:  # noqa: BLE001
        logger.exception("Cached-span masking failed for document chunk; withholding content")
        return MaskedChunk(source=chunk, content="", was_masked=True)

    if text is None:
        text = _mask_inline(chunk.content, cfg)
    return MaskedChunk(source=chunk, content=text, was_masked=True)


def mask_document_chunks(db: Session, chunks: list[ChunkHit], user_id: int) -> list[MaskedChunk]:
    """Apply the requesting user's redact-before-LLM policy to document chunks.

    The document counterpart of :func:`mask_chunks`, addressed by
    ``(document_id, chunk_index)`` rather than a transcript's time range — a document
    has no timeline, so ``chunk.char_start``/``char_end`` (stamped on every
    ``DocumentChunk`` by the chunker) is the addressing scheme, exactly as
    anticipated in this module's own history: "expect a new
    ``mask_document_chunks``-style function keyed on char offsets, following the
    same fail-closed contract." Same fail-closed contract as ``mask_chunks``:
    unresolvable policy or unmaskable content becomes ``""``, never raw text.

    Callers that already know they only have document-origin chunks can call this
    directly; ``mask_chunks`` also routes to the same logic internally for a mixed
    list, so calling the wrong one on a document chunk is not a safety hole — it is
    redundant, not incorrect.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        cfg = resolve_effective_config(db, user_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not resolve redaction config; masking all document chunk content")
        return [MaskedChunk(source=c, content="", was_masked=True) for c in chunks]

    if not (cfg.enabled and cfg.redact_before_llm):
        return [MaskedChunk(source=c, content=c.content) for c in chunks]

    return [_mask_one_document_chunk(db, chunk, cfg) for chunk in chunks]


def _mask_inline(text: str, cfg) -> str:
    """Detect and mask on the fly — for files whose cached spans aren't ready.

    Toxicity classification is skipped: it is the expensive detector and loading
    it on an interactive request would blow the latency budget. PII/profanity
    still run, which is what ``redact_before_llm`` primarily protects.

    **The ``failures`` sink is what makes the fail-closed promise true.**
    ``detect_segment_spans`` *swallows* a PII-detector exception and returns
    whatever spans it managed to collect, so "found nothing" and "could not look"
    are the same return value (issue #324). Without the sink this function
    returned the chunk **verbatim** whenever Presidio was broken or absent, and
    ``mask_chunks`` then labelled it masked and sent it — on the path that
    egresses to a third-party provider. A `try`/`except` around a call that never
    raises is not a fail-closed control; the docstring said it was.

    Narrow on purpose (see ``blocking_detector_failures``): only a failure of a
    detector feeding a category this user actually masks withholds the chunk. A
    CPU-only deployment with no Presidio that never enabled ``pii`` must keep
    answering rather than lose every excerpt to a category it never asked for.
    """
    try:
        from app.services.redaction.config import blocking_detector_failures
        from app.services.redaction.config import detection_config_for_all
        from app.services.redaction.service import RedactionService

        failures: list[str] = []
        spans, _toxicity = RedactionService.detect_segment_spans(
            text, None, detection_config_for_all(), run_toxicity=False, failures=failures
        )
        blocking = blocking_detector_failures(failures, cfg.enabled_categories)
        if blocking:
            raise RuntimeError(f"detectors unavailable for enabled categories: {sorted(blocking)}")
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
        # ⚠️ Document-origin chunks NEVER take the MediaFile lookup below.
        # ``Document.id`` and ``MediaFile.id`` are independent SERIAL sequences that
        # collide in any real deployment — a document chunk querying `MediaFile.id ==
        # chunk.file_id` can silently match an UNRELATED media file and, if their time
        # ranges happen to overlap, serve that file's transcript content as if it were
        # this document's masked text. Route by ``source_kind`` before any query runs;
        # never infer the source from "the lookup returned None".
        if chunk.is_document:
            masked.append(_mask_one_document_chunk(db, chunk, cfg))
            continue

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


def _digest_sentences(db: Session, chunk: ChunkHit) -> list[dict] | None:
    """The stored sentences of one digest section, with their provenance.

    The INDEXED digest document carries only its rendered text — no segment ids
    — so re-masking has to come back to ``file_facts`` for the provenance the
    extractive builder recorded. ``None`` means the row or the section is not
    resolvable, and the caller must then fail closed rather than fall back.
    """
    from app.models.file_facts import FileFacts

    row = db.query(FileFacts.digest).filter(FileFacts.media_file_id == chunk.file_id).first()
    if row is None or not row[0]:
        return None
    # `is None`, never `or`: section 0 is a real section and it is the FIRST one
    # of every digest, so `chunk.digest_section or -1` matched nothing for it and
    # sent every leading section down the inline-masking fallback. Found by the
    # per-sentence test, not by reading.
    wanted = -1 if chunk.digest_section is None else int(chunk.digest_section)
    sections = (row[0] or {}).get("sections") or []
    for section in sections:
        if int(section.get("index", -1)) == wanted:
            sentences = section.get("sentences") or []
            return list(sentences) if sentences else None
    return None


def mask_digests(db: Session, digests: list[ChunkHit], user_id: int) -> list[MaskedChunk]:
    """Re-mask digest sections **through their provenance**, failing closed per sentence.

    ⚠️ **Never route a digest through :func:`mask_chunks`.** That path rebuilds a
    chunk's text from every ``TranscriptSegment`` overlapping its time range,
    which is correct for a chunk — a chunk *is* contiguous turns — and
    catastrophically wrong for a digest, whose text is a handful of
    non-contiguous *selected* sentences spanning the whole recording. The
    rebuild would return the entire span verbatim in place of a short summary:
    strictly **more** text than was asked for, emitted by a function whose name
    asserts it was masked. No reviewer looks twice at a call to ``mask_*``.

    So the unit of masking here is the **sentence**, addressed by the
    ``segment_ids`` its provenance records (#403 D3). Fail-closed is per
    sentence too: an unmaskable sentence contributes nothing and the rest of the
    section still reaches the prompt, where a chunk fails closed whole. Those are
    genuinely different contracts, which is the second reason this is not an
    overload of ``mask_chunks``.

    Args:
        db: Database session.
        digests: Digest hits from ``retrieve_digests``.
        user_id: Subject of the effective redaction policy (the requester, as in
            chat generally — not the file owner).

    Returns:
        Masked digests. A section whose provenance cannot be resolved comes back
        with empty content and is dropped by the caller, never passed through raw.
    """
    if not digests:
        return []
    try:
        from app.services.redaction.config import resolve_effective_config

        cfg = resolve_effective_config(db, user_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not resolve redaction config; withholding all digest content")
        return [MaskedChunk(source=d, content="", was_masked=True) for d in digests]

    if not (cfg.enabled and cfg.redact_before_llm):
        return [MaskedChunk(source=d, content=d.content) for d in digests]

    from app.models.media import MediaFile
    from app.services.redaction.coverage import uncovered_detectors
    from app.services.redaction.service import RedactionService

    masked: list[MaskedChunk] = []
    unresolvable = 0
    for digest in digests:
        sentences = None
        try:
            scan = (
                db.query(
                    MediaFile.id,
                    MediaFile.redaction_status,
                    MediaFile.redaction_coverage,
                    MediaFile.language,
                )
                .filter(MediaFile.id == digest.file_id)
                .first()
            )
            # Same v392 coverage gate as the chunk path: `done` means the scan
            # finished, not that every relied-on detector ran. A gap falls
            # through to the inline masker below rather than applying cached
            # spans that cover less than this policy masks.
            if (
                scan is not None
                and scan.redaction_status == C.REDACTION_STATUS_DONE
                and not uncovered_detectors(scan, cfg)
            ):
                sentences = _digest_sentences(db, digest)
        except Exception:  # noqa: BLE001
            logger.exception("Digest provenance lookup failed; withholding the section")
            sentences = None

        if sentences is None:
            # No cached spans to apply. Masking the rendered section inline is
            # the only remaining option that cannot over-disclose — and if even
            # that fails, `_mask_inline` already returns "".
            unresolvable += 1
            masked.append(
                MaskedChunk(
                    source=digest, content=_mask_inline(digest.content, cfg), was_masked=True
                )
            )
            continue

        kept: list[str] = []
        for sentence in sentences:
            try:
                text = _mask_sentence(db, sentence, cfg, RedactionService)
            except Exception:  # noqa: BLE001
                logger.exception("Digest sentence masking failed; dropping that sentence")
                text = ""
            if text:
                kept.append(text)
        masked.append(MaskedChunk(source=digest, content=" ".join(kept).strip(), was_masked=True))

    if unresolvable:
        logger.info(
            "Digest masking fell back to inline detection for %d/%d sections "
            "(no cached provenance — detection may still be running)",
            unresolvable,
            len(digests),
        )
    return masked


def _mask_sentence(db: Session, sentence: dict, cfg, redaction_service) -> str:
    """Mask one digest sentence from the cached spans of its own segments.

    Returns ``""`` when the provenance is a kind this reader does not understand
    — a ``char_range`` document sentence (#362) reaching a transcript masker is
    a bug, and guessing at it would send unmasked text.
    """
    from app.models.media import TranscriptSegment
    from app.services.ingest_artifacts.provenance import KIND_SEGMENT_IDS

    provenance = sentence.get("provenance") or {}
    if provenance.get("kind") != KIND_SEGMENT_IDS:
        return ""
    segment_ids = [int(i) for i in provenance.get("segment_ids") or []]
    if not segment_ids:
        return ""

    rows = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.id.in_(segment_ids))
        .order_by(TranscriptSegment.start_time, TranscriptSegment.end_time, TranscriptSegment.id)
        .all()
    )
    if not rows:
        return ""
    parts = []
    for row in rows:
        text = str(row.text or "")
        if not text:
            continue
        piece, _applied = redaction_service.mask_segment(
            text, row.redactions or [], row.words, cfg, set()
        )
        parts.append(piece)
    return " ".join(parts).strip()
