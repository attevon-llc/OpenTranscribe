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

⚠️ **Both public maskers are TWO-PHASE: gather from Postgres, close the session,
then mask (issue #83).** They take a *session factory*, not a ``Session``, for the
same reason ``aggregation_service`` does. The fallback above runs Presidio, and a
cold ``AnalyzerEngine`` build is **~10 s**; running it inside the caller's
transaction put a chat turn ``idle in transaction`` for that long — measured
**13,898 ms** against real Postgres — which queues every ``ALTER TABLE`` behind it
and hangs an Alembic upgrade mid-release. ``redaction/warmup.py`` makes the cold
build rare, not impossible: the warm-up gate is evaluated **once** at API startup,
so a deployment that enables redaction afterwards still pays it, and a request
landing mid-warm-up waits for the remainder.

Phase A returns PLAIN DATA (:class:`_SegmentSpans`), never an ORM instance —
attribute access on a detached row re-opens a session and silently undoes the
split. See this package's CLAUDE.md.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)

#: A callable returning a session context manager — ``db.session_utils.session_scope``
#: in production, the test's own session in the suites. Same contract as
#: ``aggregation_service.SessionFactory``, except that ``None`` is not accepted:
#: "no Postgres" is not a decline here, it is a policy that cannot be resolved,
#: and this module fails closed on that rather than passing text through.
SessionFactory = Callable[[], contextlib.AbstractContextManager[Session]]


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

    ``segments is None`` means the cached-span path declined (no scan, an
    unfinished scan, a coverage gap, or no overlapping segments) and the chunk
    must be masked inline. ``failed`` means the gather itself raised, and the
    chunk fails closed **without** an inline retry — exactly as before the split,
    where an exception out of the cached path appended ``""`` and moved on.
    """

    segments: list[_SegmentSpans] | None = None
    failed: bool = False


@dataclass
class _DigestPlan:
    """What the gather learned about one digest section.

    ``sentences is None`` means the provenance could not be resolved and the
    rendered section is masked inline. Otherwise it is one entry per stored
    sentence, in order; an **empty** entry is a sentence that must be withheld
    (unknown provenance kind, no segment ids, rows gone, or a failed read).
    """

    sentences: list[list[_SegmentSpans]] | None = None
    unresolvable: bool = False


@dataclass
class _MaskingInputs:
    """Everything the masking phase needs, with no session held.

    ``cfg`` is ``None`` only when the policy could not be resolved at all, which
    fails closed for every item — the one condition that is not per-item.
    """

    cfg: Any = None
    applies: bool = False
    chunks: list[_ChunkPlan] = field(default_factory=list)
    digests: list[_DigestPlan] = field(default_factory=list)


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


def _gather_chunk_segments(db: Session, chunk: ChunkHit, cfg) -> list[_SegmentSpans] | None:
    """Read the segments backing one chunk, or None to mean "mask inline".

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
    return [_segment_spans(segment) for segment in segments]


def _gather_chunk_plans(db: Session, chunks: list[ChunkHit], cfg) -> list[_ChunkPlan]:
    """Phase A for :func:`mask_chunks`. One failed read withholds one chunk."""
    plans: list[_ChunkPlan] = []
    for chunk in chunks:
        try:
            plans.append(_ChunkPlan(segments=_gather_chunk_segments(db, chunk, cfg)))
        except Exception:  # noqa: BLE001
            logger.exception("Cached-span lookup failed for chunk; withholding content")
            # Fail CLOSED — an unmaskable chunk contributes nothing.
            plans.append(_ChunkPlan(failed=True))
    return plans


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


def _gather_sentence_segments(db: Session, sentence: dict) -> list[_SegmentSpans]:
    """The segments one digest sentence was drawn from.

    Returns ``[]`` when the provenance is a kind this reader does not understand
    — a ``char_range`` document sentence (#362) reaching a transcript masker is
    a bug, and guessing at it would send unmasked text. An empty list withholds
    the sentence.
    """
    from app.models.media import TranscriptSegment
    from app.services.ingest_artifacts.provenance import KIND_SEGMENT_IDS

    provenance = sentence.get("provenance") or {}
    if provenance.get("kind") != KIND_SEGMENT_IDS:
        return []
    segment_ids = [int(i) for i in provenance.get("segment_ids") or []]
    if not segment_ids:
        return []

    rows = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.id.in_(segment_ids))
        .order_by(TranscriptSegment.start_time, TranscriptSegment.end_time, TranscriptSegment.id)
        .all()
    )
    return [_segment_spans(row) for row in rows]


def _gather_digest_plans(db: Session, digests: list[ChunkHit], cfg) -> list[_DigestPlan]:
    """Phase A for :func:`mask_digests`. Fails closed per SENTENCE, not per section."""
    from app.models.media import MediaFile
    from app.services.redaction.coverage import uncovered_detectors

    plans: list[_DigestPlan] = []
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
            # through to the inline masker rather than applying cached spans
            # that cover less than this policy masks.
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
            plans.append(_DigestPlan(sentences=None, unresolvable=True))
            continue

        gathered: list[list[_SegmentSpans]] = []
        for sentence in sentences:
            try:
                gathered.append(_gather_sentence_segments(db, sentence))
            except Exception:  # noqa: BLE001
                logger.exception("Digest sentence lookup failed; dropping that sentence")
                gathered.append([])
        plans.append(_DigestPlan(sentences=gathered))
    return plans


def _gather(
    session_factory: SessionFactory,
    user_id: int,
    *,
    chunks: list[ChunkHit] | None = None,
    digests: list[ChunkHit] | None = None,
) -> _MaskingInputs:
    """Open ONE short session, read everything, close it.

    Raises whatever the factory or the config read raises; the callers turn that
    into a fail-closed result for every item, because a policy that cannot be
    resolved is not a policy that permits sending text.
    """
    from app.services.redaction.config import resolve_effective_config

    with session_factory() as db:
        cfg = resolve_effective_config(db, user_id)
        inputs = _MaskingInputs(cfg=cfg, applies=bool(cfg.enabled and cfg.redact_before_llm))
        if not inputs.applies:
            return inputs
        if chunks is not None:
            inputs.chunks = _gather_chunk_plans(db, chunks, cfg)
        if digests is not None:
            inputs.digests = _gather_digest_plans(db, digests, cfg)
        return inputs


# --------------------------------------------------------------------------- #
# Phase B — CPU. Nothing below may touch the database.
# --------------------------------------------------------------------------- #


def _mask_inline(text: str, cfg) -> str:
    """Detect and mask on the fly — for files whose cached spans aren't ready.

    Toxicity classification is skipped: it is the expensive detector and loading
    it on an interactive request would blow the latency budget. PII/profanity
    still run, which is what ``redact_before_llm`` primarily protects.

    **Runs with no database session open** (#83) — a cold Presidio build is ~10 s,
    and paying it inside a transaction is the defect
    ``scripts/audit-session-lifetime.py`` exists to catch.

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


def _mask_from_spans(segments: list[_SegmentSpans], cfg) -> str:
    """Apply cached spans to gathered segment text. Pure — no I/O, no detectors."""
    from app.services.redaction.service import RedactionService

    parts = []
    for segment in segments:
        if not segment.text:
            continue
        masked, _applied = RedactionService.mask_segment(
            segment.text, segment.redactions, segment.words, cfg, set()
        )
        parts.append(masked)
    return " ".join(parts).strip()


def _apply_chunk_plan(plan: _ChunkPlan, chunk: ChunkHit, cfg) -> tuple[str, bool]:
    """Mask one chunk from its plan. Returns ``(content, used_inline)``."""
    if plan.failed:
        return "", False
    if plan.segments is None:
        return _mask_inline(chunk.content, cfg), True
    try:
        text = _mask_from_spans(plan.segments, cfg)
    except Exception:  # noqa: BLE001
        logger.exception("Cached-span masking failed for chunk; withholding content")
        # Fail CLOSED — an unmaskable chunk contributes nothing.
        return "", False
    if not text:
        return _mask_inline(chunk.content, cfg), True
    return text, False


# --------------------------------------------------------------------------- #
# The two public maskers. NOT interchangeable — see this package's CLAUDE.md.
# --------------------------------------------------------------------------- #


def mask_chunks(
    session_factory: SessionFactory, chunks: list[ChunkHit], user_id: int
) -> list[MaskedChunk]:
    """Apply the **requesting user's** redact-before-LLM policy to retrieved chunks.

    ⚠️ **The requester, not the file owner** (issue #402). This docstring said
    "the owner's" while its sole caller — ``chat/service.py`` — passes the message
    author, and that asymmetry is load-bearing rather than accidental: one chat
    turn can span recordings owned by several people, so there is no single owner
    to resolve. Summarization goes the other way and resolves the FILE OWNER
    (``tasks/summarization.py``), because that is an egress decision about whose
    content leaves for a third party. The two subjects differ deliberately;
    ``redaction/export_policy.py`` argues the general rule. Inheriting whichever
    the surrounding code used is a documented trap here, and a docstring naming
    the wrong one is how it gets inherited.

    Two phases: **one** short session gathers the policy and every chunk's cached
    spans, then the session closes and the masking (including a possible Presidio
    load) runs with nothing held (#83).

    Args:
        session_factory: Callable returning a session context manager
            (``session_scope``). NOT a ``Session`` — this function owns the
            transaction boundary precisely so it can close it before masking.
        chunks: Chunks straight out of retrieval (unredacted index content).
        user_id: The REQUESTING user, whose effective policy governs (admin force
            floor included) — matching :func:`mask_digests` beside it.

    Returns:
        Chunks with prompt-safe text. When the policy does not apply, content is
        passed through untouched.
    """
    try:
        inputs = _gather(session_factory, user_id, chunks=chunks)
    except Exception:  # noqa: BLE001
        logger.exception("Could not resolve redaction config; masking all chunk content")
        # Fail CLOSED: if we cannot tell whether masking is required, don't send text.
        return [MaskedChunk(source=c, content="", was_masked=True) for c in chunks]

    if not inputs.applies:
        return [MaskedChunk(source=c, content=c.content) for c in chunks]

    masked: list[MaskedChunk] = []
    inline_fallbacks = 0
    for chunk, plan in zip(chunks, inputs.chunks, strict=True):
        text, used_inline = _apply_chunk_plan(plan, chunk, inputs.cfg)
        inline_fallbacks += int(used_inline)
        masked.append(MaskedChunk(source=chunk, content=text, was_masked=True))

    if inline_fallbacks:
        logger.info(
            "Chat masking used the inline fallback for %d/%d chunks "
            "(segments unavailable — detection may still be running)",
            inline_fallbacks,
            len(chunks),
        )
    return masked


def mask_digests(
    session_factory: SessionFactory, digests: list[ChunkHit], user_id: int
) -> list[MaskedChunk]:
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

    Two-phase like :func:`mask_chunks`: the provenance read and every sentence's
    segments are gathered in one short session, which closes before any masking
    (#83).

    Args:
        session_factory: Callable returning a session context manager.
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
        inputs = _gather(session_factory, user_id, digests=digests)
    except Exception:  # noqa: BLE001
        logger.exception("Could not resolve redaction config; withholding all digest content")
        return [MaskedChunk(source=d, content="", was_masked=True) for d in digests]

    if not inputs.applies:
        return [MaskedChunk(source=d, content=d.content) for d in digests]

    masked: list[MaskedChunk] = []
    unresolvable = 0
    for digest, plan in zip(digests, inputs.digests, strict=True):
        if plan.sentences is None:
            # No cached spans to apply. Masking the rendered section inline is
            # the only remaining option that cannot over-disclose — and if even
            # that fails, `_mask_inline` already returns "".
            unresolvable += 1
            masked.append(
                MaskedChunk(
                    source=digest,
                    content=_mask_inline(digest.content, inputs.cfg),
                    was_masked=True,
                )
            )
            continue

        kept: list[str] = []
        for segments in plan.sentences:
            if not segments:
                continue
            try:
                text = _mask_from_spans(segments, inputs.cfg)
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
