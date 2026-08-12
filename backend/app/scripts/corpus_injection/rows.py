"""Pure row construction — no session, no I/O.

Split out of :mod:`.injector` so the exact rows a run *would* write can be built
without a database. Two callers need that:

* the write path, which inserts them;
* the **skip** path, which must still emit the manifest's turn table. A re-run
  that produced a manifest with an empty ``turns.jsonl`` would be worse than no
  manifest at all — the gold-span-to-chunk mapping lives in that table, so it has
  to be reproducible from the corpus alone, not only as a side effect of writing.

Everything here is deterministic in ``(corpus, meeting_id, seed)``.
"""

from __future__ import annotations

from typing import Any

from app.scripts.corpus_injection import ids
from app.scripts.corpus_injection.model import MeetingDoc

#: Step used to separate two segments that would otherwise be identical under
#: ``uq_transcript_segment_content``. See :func:`separate_duplicate_spans`.
DUPLICATE_SPAN_NUDGE_S = 0.001

#: Not a real media type. Deliberately not ``audio/*`` or ``video/*``: the row has
#: no media, and a MIME type claiming otherwise invites a player to try.
INJECTED_CONTENT_TYPE = "text/vnd.opentranscribe.injected-transcript"

#: Marker key written into ``MediaFile.metadata_important``. Anything reading a
#: file's timings should look here first; the manifest is the offline mirror.
RAG_EVAL_KEY = "rag_eval"


def storage_path(user_id: int, file_id: int, meeting_id: str) -> str:
    """A structurally valid storage key that intentionally points at nothing.

    ``MediaFile.storage_path`` is ``NOT NULL`` and nothing validates that the key
    resolves. Mirroring ``utils/filename.get_safe_storage_filename``'s
    ``user_{uid}/file_{fid}/{name}`` shape keeps anything that parses the key
    working, while ``no-media`` in the leaf names the row honestly for a human
    reading the table.
    """
    # Dots are dropped along with separators: a meeting id is attacker-adjacent
    # (it comes from a third-party corpus's filenames) and ".." surviving into a
    # storage key is the kind of thing that is only harmless until someone joins
    # the key onto a filesystem path.
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in meeting_id)[:96]
    return f"user_{user_id}/file_{file_id}/no-media.{safe}.injected"


def eval_metadata(doc: MeetingDoc, seed: str, tool_version: str, digest: str) -> dict[str, Any]:
    """The ``rag_eval`` block stamped onto ``MediaFile.metadata_important``."""
    return {
        RAG_EVAL_KEY: {
            "injected": True,
            "tool_version": tool_version,
            "content_sha256": digest,
            "corpus": doc.corpus,
            "meeting_id": doc.meeting_id,
            "seed": seed,
            "timing_source": doc.timing.source,
            "timing_reference": doc.timing.reference,
            "timing_aligned_turns": doc.timing.aligned_turns,
            "timing_alignment_rate": round(doc.timing.alignment_rate, 4),
            "synthetic_timing_params": doc.timing.params,
            # Read this before computing anything from a segment time.
            "timings_are_measurements": doc.timing.is_real,
            "has_media": False,
            "turn_count": len(doc.turns),
            **doc.extra,
        }
    }


def separate_duplicate_spans(rows: list[dict[str, Any]]) -> int:
    """Make ``(start_time, end_time, text)`` unique within a file.

    The live DDL carries ``UNIQUE (media_file_id, start_time, end_time,
    md5(text))`` (``uq_transcript_segment_content``) — an index the ORM class
    does not declare, so it is invisible until an insert aborts. Real meetings
    hit it: two speakers say "Yeah ." over each other and the reference gives
    both the same span, and interpolated backchannels can land on one instant.
    ASR never produces the collision because it emits one segment per detected
    utterance; a turn-per-segment corpus does.

    ``end`` is walked forward in 1 ms steps until the triple is unique. Nudging
    the end rather than the start leaves every segment's onset — the thing a
    citation points at — exactly where the reference put it, and 1 ms is three
    orders of magnitude below the granularity of the underlying annotations.
    Returns the number of rows adjusted, which the manifest records so the
    perturbation is never silent.
    """
    seen: set[tuple[float, float, str]] = set()
    nudged = 0
    for row in rows:
        key = (row["start_time"], row["end_time"], row["text"])
        if key not in seen:
            seen.add(key)
            continue
        while key in seen:
            row["end_time"] = round(row["end_time"] + DUPLICATE_SPAN_NUDGE_S, 6)
            key = (row["start_time"], row["end_time"], row["text"])
        seen.add(key)
        nudged += 1
    return nudged


def build_segment_rows(
    doc: MeetingDoc,
    seed: str,
    media_file_id: int | None = None,
    speaker_ids: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Build insertable segment rows and the manifest's turn table.

    Segments are emitted in **time order**, because that is the order
    ``index_transcript_search_task`` reads them back in, and turn order and time
    order genuinely differ once overlapping speech is aligned from a real
    recording. The source ``turn_index`` rides along in the turn table so a gold
    relevance span — which addresses turns, not seconds — can still be mapped
    onto the right segment and from there onto the chunks the indexer produced.

    Args:
        doc: A meeting whose timings have already been resolved.
        seed: UUID namespace suffix.
        media_file_id: PK to stamp on each row; ``None`` when building for a
            manifest-only (skip) pass.
        speaker_ids: Speaker label -> ``speaker.id``; ``None`` leaves them unset.

    Returns:
        ``(segment_rows, turn_rows, duplicate_spans_nudged)``.
    """
    speaker_ids = speaker_ids or {}
    ordered = sorted(doc.turns, key=lambda t: (t.start or 0.0, t.end or 0.0, t.turn_index))

    segment_rows: list[dict[str, Any]] = []
    turn_rows: list[dict[str, Any]] = []
    for position, turn in enumerate(ordered):
        if not turn.text:
            continue
        seg_uuid = ids.segment_uuid(doc.corpus, doc.meeting_id, seed, position)
        segment_rows.append(
            {
                "uuid": seg_uuid,
                "media_file_id": media_file_id,
                "speaker_id": speaker_ids.get(turn.speaker),
                "start_time": float(turn.start or 0.0),
                "end_time": float(turn.end or 0.0),
                "text": turn.text,
                "is_overlap": False,
                "overlap_group_id": None,
                "overlap_confidence": None,
                # NULL for a synthetic meeting by design: a word-timing metric
                # then has no rows to read rather than plausible-looking ones.
                "words": [w.as_dict() for w in turn.words] if turn.words else None,
                "confidence": None,
            }
        )
        turn_rows.append(
            {
                "turn_index": turn.turn_index,
                "segment_position": position,
                "segment_uuid": str(seg_uuid),
                "speaker": turn.speaker,
                "start": float(turn.start or 0.0),
                "end": float(turn.end or 0.0),
                "word_count": len(turn.text.split()),
            }
        )

    nudged = separate_duplicate_spans(segment_rows)
    for segment_row, turn_row in zip(segment_rows, turn_rows, strict=True):
        turn_row["end"] = segment_row["end_time"]
    return segment_rows, turn_rows, nudged
