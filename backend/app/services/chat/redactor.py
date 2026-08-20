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
            # ⚠️ Document-origin chunks NEVER take the MediaFile lookup below.
            # ``Document.id`` and ``MediaFile.id`` are independent SERIAL sequences that
            # collide in any real deployment — a document chunk querying `MediaFile.id ==
            # chunk.file_id` can silently match an UNRELATED media file and, if their time
            # ranges happen to overlap, serve that file's transcript content as if it were
            # this document's masked text. Route by ``source_kind`` before any query runs;
            # never infer the source from "the lookup returned None".
            #
            # Both branches return ``_SegmentSpans`` (plain data), so the masking phase in
            # :func:`_apply_chunk_plan` is shared and holds no session either way.
            if chunk.is_document:
                plans.append(_ChunkPlan(segments=_gather_document_chunk_spans(db, chunk, cfg)))
                continue
            plans.append(_ChunkPlan(segments=_gather_chunk_segments(db, chunk, cfg)))
        except Exception:  # noqa: BLE001
            logger.exception("Cached-span lookup failed for chunk; withholding content")
            # Fail CLOSED — an unmaskable chunk contributes nothing.
            plans.append(_ChunkPlan(failed=True))
    return plans


def _load_digest_rows(db: Session, file_ids: list[int]) -> dict[int, Any]:
    """``media_file_id -> digest JSON`` for every digest hit in ONE masking call.

    A single ``file_id IN (...)`` query, instead of the one-query-per-hit
    :func:`_digest_sentences` used to run inside :func:`_gather_digest_plans`'s
    loop. A bounded-scope summarize turn's map (``mapreduce.scope_digest_hits``)
    is exactly the shape that made this an N+1 in practice: with the default
    ``sections_per_file``, one hit per file in scope means one round trip per
    file — 25 files, 25 round trips, for data that fits in a single ``IN``
    query. Deliberately a plain dict lookup afterward, not a second query per
    hit, however many hits share a file (multiple sections of one file).

    Args:
        db: Session.
        file_ids: The ``file_id`` of every digest hit this masking call covers.
            Duplicates are fine; the query dedupes via ``set()``.

    Returns:
        A dict covering every file that HAS a ``file_facts`` row. A file with
        no row (never ingested, or predates ``file_facts``) is simply absent —
        callers already treat an absent/empty digest as "fall through to the
        inline masker", the same contract :func:`_digest_sentences` had.
    """
    from app.models.file_facts import FileFacts

    if not file_ids:
        return {}
    rows = (
        db.query(FileFacts.media_file_id, FileFacts.digest)
        .filter(FileFacts.media_file_id.in_(set(file_ids)))
        .all()
    )
    return {int(file_id): digest for file_id, digest in rows}


def _filter_sentences_by_speaker(sentences: list[dict], speaker_filter: str) -> list[dict]:
    """Keep only the sentences ``speaker_filter`` names. THE MASKING SEAM (W2.3).

    ``mapreduce.scope_speaker_digest_hits`` already filtered a real section's
    sentences by speaker once, to build its hit's own (unmasked, pre-mask)
    ``content`` — but masking comes back to ``file_facts.digest`` and re-reads
    the WHOLE real section fresh, because that section may hold OTHER
    speakers' sentences too (a digest section is a relevance-selected group of
    sentences, not a per-speaker one). Skipping this filter here rebuilds the
    full section regardless of which speaker was asked about, and "a summary
    of Alice" comes back quoting Bob. ``tests/unit/test_chat_digest_masking.py``
    pins this with a must-fire guard proving the raw section mixes speakers,
    beside the must-stay-clean proof that the real masked output does not.

    Args:
        sentences: One section's stored sentences (already resolved to the
            right section by :func:`_digest_sentences_from_row`).
        speaker_filter: :attr:`ChunkHit.speaker`, as set by
            ``scope_speaker_digest_hits`` — the requested names, pipe-joined.
            An ordinary (non-speaker-scoped) digest hit never sets this field,
            so this function is a no-op for every pre-W2.3 caller.

    Returns:
        The subset of ``sentences`` whose own ``speaker`` matches one of the
        pipe-joined names (case-insensitive). Empty when nothing matches —
        the caller (:func:`_digest_sentences_from_row`) already treats an
        empty sentence list as unresolvable and falls through to the inline
        masker, which is safe here too: it detects directly over the hit's
        own (already speaker-filtered) ``content``, touching no file lookup.
    """
    wanted = {piece.strip().casefold() for piece in speaker_filter.split("|") if piece.strip()}
    if not wanted:
        return sentences
    return [s for s in sentences if str(s.get("speaker") or "").strip().casefold() in wanted]


def _digest_sentences_from_row(digest_json: Any, chunk: ChunkHit) -> list[dict] | None:
    """The stored sentences of one digest section, given an ALREADY-LOADED digest.

    Split out of :func:`_digest_sentences` so the section-matching rule has one
    implementation whether the digest JSON came from a single-hit query (that
    function) or from the batched read :func:`_load_digest_rows` performs once
    per masking call (:func:`_gather_digest_plans`). ``None`` means the row or
    the section is not resolvable, and the caller must then fail closed rather
    than fall back.

    ⚠️ When ``chunk.speaker`` is set (a per-speaker map hit from
    ``mapreduce.scope_speaker_digest_hits``), the section's sentences are
    additionally filtered to that speaker via :func:`_filter_sentences_by_speaker`
    — see that function's docstring for why this is not optional. An ordinary
    digest hit never sets ``speaker``, so this is a no-op for every other caller.
    """
    if not digest_json:
        return None
    # `is None`, never `or`: section 0 is a real section and it is the FIRST one
    # of every digest, so `chunk.digest_section or -1` matched nothing for it and
    # sent every leading section down the inline-masking fallback. Found by the
    # per-sentence test, not by reading.
    wanted = -1 if chunk.digest_section is None else int(chunk.digest_section)
    sections = (digest_json or {}).get("sections") or []
    for section in sections:
        if int(section.get("index", -1)) == wanted:
            sentences = section.get("sentences") or []
            if chunk.speaker:
                sentences = _filter_sentences_by_speaker(sentences, chunk.speaker)
            return list(sentences) if sentences else None
    return None


def _digest_sentences(db: Session, chunk: ChunkHit) -> list[dict] | None:
    """The stored sentences of one digest section, with their provenance.

    The INDEXED digest document carries only its rendered text — no segment ids
    — so re-masking has to come back to ``file_facts`` for the provenance the
    extractive builder recorded. ``None`` means the row or the section is not
    resolvable, and the caller must then fail closed rather than fall back.

    The single-hit form: reads its own row. :func:`_gather_digest_plans` does
    NOT call this — it batches the read for every hit in the masking call via
    :func:`_load_digest_rows` and calls :func:`_digest_sentences_from_row`
    directly, to avoid the N+1 this function's per-hit query would otherwise
    reproduce inside a loop. This form remains for any caller that genuinely
    has one hit and no batch to amortize the query over.

    ``None`` for a document-origin ``chunk`` (:attr:`ChunkHit.is_document`),
    defensively, even though nothing on today's call path reaches this
    function with one: ``FileFacts.media_file_id`` is never a document's id,
    and querying it that way risks the same id-collision hazard
    :func:`_gather_digest_plans` routes document-origin hits around entirely.
    """
    if chunk.is_document:
        return None
    from app.models.file_facts import FileFacts

    row = db.query(FileFacts.digest).filter(FileFacts.media_file_id == chunk.file_id).first()
    if row is None:
        return None
    return _digest_sentences_from_row(row[0], chunk)


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

    # ONE query for every MEDIA digest hit's `file_facts.digest`, not one per
    # hit — see `_load_digest_rows`'s docstring for the N+1 this replaces. A
    # failure here degrades every media hit to "no digest row read", which the
    # per-hit `.get()` below already treats as None — the same fail-closed
    # outcome a per-hit query failure produced before batching, just via one
    # failure point instead of N independent ones. Document-origin hits are
    # deliberately excluded from this query's `file_id` list — see the
    # `is_document` branch below for why.
    try:
        digest_rows = _load_digest_rows(db, [d.file_id for d in digests if not d.is_document])
    except Exception:  # noqa: BLE001
        logger.exception("Batched digest read failed; every hit falls through to unresolvable")
        digest_rows = {}

    plans: list[_DigestPlan] = []
    for digest in digests:
        if digest.is_document:
            # ⚠️ Document-origin digest hits (`mapreduce._document_scope_hits`,
            # #403 Stage-6 mixed-collection coverage) NEVER take the MediaFile
            # lookup below. `Document.id` and `MediaFile.id` are independent
            # SERIAL sequences that collide in any real deployment — the same
            # hazard `_gather_chunk_plans` already routes around for the chunk
            # plane. A collision here would silently mask (and serve) an
            # UNRELATED media file's cached spans as if they belonged to this
            # document: `_load_digest_rows`/`_digest_sentences_from_row` would
            # be handed that other file's real digest JSON, and its section
            # matching the same `digest_section` index would return a
            # different file's REAL sentences with REAL provenance — not a
            # missing-lookup failure, an actively wrong answer.
            #
            # The document analog of per-sentence provenance masking exists
            # (`ingest_artifacts.document_digest_masking.mask_char_range_provenance`)
            # but is deliberately NOT wired in here — every document-origin
            # digest hit therefore falls straight through to the inline
            # masker below (`_mask_inline(digest.content, cfg)`), which
            # detects directly over the hit's own content (already correctly
            # scoped by `_document_scope_hits`) and touches no `file_id`
            # lookup at all, so it cannot cross a file boundary either way.
            # See `services/chat/CLAUDE.md` for the follow-on work this
            # leaves open.
            plans.append(_DigestPlan(sentences=None, unresolvable=True))
            continue

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
                sentences = _digest_sentences_from_row(digest_rows.get(digest.file_id), digest)
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
    unmask_for_local: bool = False,
) -> _MaskingInputs:
    """Open ONE short session, read everything, close it.

    Raises whatever the factory or the config read raises; the callers turn that
    into a fail-closed result for every item, because a policy that cannot be
    resolved is not a policy that permits sending text.

    Args:
        unmask_for_local: True when the turn's LLM config is local
            (``llm_guard.is_local_provider``) — the text never leaves the
            machine, so masking it before the call costs recall for no egress
            benefit. Only takes effect when the policy would otherwise mask
            AND the admin has not forced ``redact_before_llm``
            (``cfg.redact_before_llm_locked``): the force floor exists
            specifically to override a per-provider exemption like this one, so
            a locked policy masks for every provider, local included.
    """
    from app.services.redaction.config import resolve_effective_config

    with session_factory() as db:
        cfg = resolve_effective_config(db, user_id)
        applies = bool(cfg.enabled and cfg.redact_before_llm)
        if applies and unmask_for_local and not cfg.redact_before_llm_locked:
            applies = False
        inputs = _MaskingInputs(cfg=cfg, applies=applies)
        if not inputs.applies:
            return inputs
        if chunks is not None:
            inputs.chunks = _gather_chunk_plans(db, chunks, cfg)
        if digests is not None:
            inputs.digests = _gather_digest_plans(db, digests, cfg)
        return inputs


def _gather_document_chunk_spans(db: Session, chunk: ChunkHit, cfg) -> list[_SegmentSpans] | None:
    """The document analog of :func:`_gather_chunk_segments`. Phase A — reads only.

    Simpler than the transcript case by construction: a ``document_chunk`` row
    already **is** the retrieval unit indexed into OpenSearch (1:1) — there is no
    "rebuild from several overlapping rows" step, just a direct lookup by
    ``(document_id, chunk_index)`` and a read of that row's own cached spans.

    Returns ``None`` whenever the cached-span path cannot be trusted (mirrors
    :func:`_gather_chunk_segments` exactly: unscanned, or scanned with a coverage
    gap against this policy), so the masking phase falls back to inline detection.
    Returns plain ``_SegmentSpans``, never an ORM row — the first attribute read on
    a detached row would re-open a transaction in the masking phase, which is the
    whole point of the split.
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

    # One row, not a rebuild: a ``document_chunk`` IS the indexed retrieval unit (1:1).
    # ``words=None`` — a document has no word timings; ``mask_segment`` takes that.
    return [_SegmentSpans(text=row.text, redactions=list(row.redactions or []), words=None)]


# --------------------------------------------------------------------------- #
# Phase B — CPU. Nothing below may touch the database.
# --------------------------------------------------------------------------- #


def mask_document_chunks(
    session_factory: SessionFactory,
    chunks: list[ChunkHit],
    user_id: int,
    *,
    unmask_for_local: bool = False,
) -> list[MaskedChunk]:
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

    Two-phase like :func:`mask_chunks` (issue #83): one short session gathers, the
    session closes, and the masking — which may pay a cold Presidio build on the
    inline fallback — runs with nothing held.
    """
    return mask_chunks(session_factory, chunks, user_id, unmask_for_local=unmask_for_local)


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
    session_factory: SessionFactory,
    chunks: list[ChunkHit],
    user_id: int,
    *,
    unmask_for_local: bool = False,
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
        unmask_for_local: True when the turn's LLM is local
            (``redaction.llm_guard.is_local_provider``) — the excerpt text never
            leaves the machine, so this policy's masking is skipped unless the
            admin has forced ``redact_before_llm`` (``cfg.redact_before_llm_locked``),
            in which case the force floor wins and masking still applies.

    Returns:
        Chunks with prompt-safe text. When the policy does not apply — including
        when it is skipped for a local provider — content is passed through
        untouched.
    """
    try:
        inputs = _gather(session_factory, user_id, chunks=chunks, unmask_for_local=unmask_for_local)
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
    session_factory: SessionFactory,
    digests: list[ChunkHit],
    user_id: int,
    *,
    unmask_for_local: bool = False,
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
        unmask_for_local: Same rule as :func:`mask_chunks` — skip masking for a
            local LLM unless the admin force floor
            (``cfg.redact_before_llm_locked``) overrides the exemption.

    Returns:
        Masked digests. A section whose provenance cannot be resolved comes back
        with empty content and is dropped by the caller, never passed through raw.
    """
    if not digests:
        return []
    try:
        inputs = _gather(
            session_factory, user_id, digests=digests, unmask_for_local=unmask_for_local
        )
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
