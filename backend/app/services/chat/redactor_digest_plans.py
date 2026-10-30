"""Phase A of :func:`~app.services.chat.redactor.mask_digests`: gather each section's provenance.

Postgres reads only, no masking. A digest section is re-masked through the ``segment_ids``
each stored sentence records (#403 D3), so this reads ``file_facts.digest`` once per masking
call, resolves the section (and, for a per-speaker hit, that speaker's sentences), and gathers
the segments behind every sentence as plain data (:class:`_DigestPlan`). It fails closed per
SENTENCE: an empty entry withholds that sentence. The masking half is
:mod:`app.services.chat.redactor`; see this package's CLAUDE.md for why a digest must never go
through the chunk path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core import redaction_defaults
from app.services.chat.redactor_chunk_plans import _effective_cfg_for_owner
from app.services.chat.redactor_chunk_plans import _segment_spans
from app.services.chat.redactor_chunk_plans import _SegmentSpans
from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)


@dataclass
class _DigestPlan:
    """What the gather learned about one digest section.

    ``cfg``/``applies`` carry the same meaning as :class:`_ChunkPlan`'s: this
    section's own effective (owner ∪ requester) policy, and whether it calls
    for masking this section at all. ``sentences is None`` means masking
    applies but the provenance could not be resolved, so the rendered section
    is masked inline. Otherwise ``sentences`` is one entry per stored
    sentence, in order; an **empty** entry is a sentence that must be withheld
    (unknown provenance kind, no segment ids, rows gone, or a failed read).
    """

    sentences: list[list[_SegmentSpans]] | None = None
    unresolvable: bool = False
    cfg: Any = None
    applies: bool = False


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
    """Keep only the sentences ``speaker_filter`` names. THE MASKING SEAM.

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
            so this function is a no-op for every caller that predates speaker-scoped digests.

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
    """
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


def _gather_digest_plans(
    db: Session, digests: list[ChunkHit], requester_user_id: int, requester_cfg
) -> list[_DigestPlan]:
    """Phase A for :func:`mask_digests`. Fails closed per SENTENCE, not per section."""
    from app.models.media import MediaFile
    from app.services.redaction.coverage import uncovered_detectors

    owner_cache: dict[Any, Any] = {}

    # ONE query for every digest hit's `file_facts.digest`, not one per hit —
    # see `_load_digest_rows`'s docstring for the N+1 this replaces. A failure
    # here degrades every hit to "no digest row read", which the per-hit
    # `.get()` below already treats as None — the same fail-closed outcome a
    # per-hit query failure produced before batching, just via one failure
    # point instead of N independent ones.
    try:
        digest_rows = _load_digest_rows(db, [d.file_id for d in digests])
    except Exception:  # noqa: BLE001
        logger.exception("Batched digest read failed; every hit falls through to unresolvable")
        digest_rows = {}

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
                    MediaFile.user_id,
                    MediaFile.organization_id,
                )
                .filter(MediaFile.id == digest.file_id)
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
            # Same v392 coverage gate as the chunk path: `done` means the scan
            # finished, not that every relied-on detector ran. A gap falls
            # through to the inline masker rather than applying cached spans
            # that cover less than this policy masks. `effective` is the
            # strictest-wins union (task #40), never the requester alone.
            if (
                scan is not None
                and scan.redaction_status == redaction_defaults.REDACTION_STATUS_DONE
                and not uncovered_detectors(scan, effective)
            ):
                sentences = _digest_sentences_from_row(digest_rows.get(digest.file_id), digest)
        except Exception:  # noqa: BLE001
            logger.exception("Digest provenance lookup failed; withholding the section")
            sentences = None
            effective = _effective_cfg_for_owner(
                db, None, requester_user_id, requester_cfg, owner_cache
            )

        applies = bool(
            getattr(effective, "enabled", False) and getattr(effective, "redact_before_llm", False)
        )

        if sentences is None:
            plans.append(
                _DigestPlan(sentences=None, unresolvable=True, cfg=effective, applies=applies)
            )
            continue

        gathered: list[list[_SegmentSpans]] = []
        for sentence in sentences:
            try:
                gathered.append(_gather_sentence_segments(db, sentence))
            except Exception:  # noqa: BLE001
                logger.exception("Digest sentence lookup failed; dropping that sentence")
                gathered.append([])
        plans.append(_DigestPlan(sentences=gathered, cfg=effective, applies=applies))
    return plans
