"""Carry a user's speaker corrections across a re-run that replaces the file's speakers.

Issue #1233. An unassignment (or a rejection, or one inherited by a merge) is recorded
against a speaker ROW: ``speaker_profile_blacklist`` and ``speaker_cannot_link``, both
``ON DELETE CASCADE``. Re-diarizing, re-transcribing and retrying a file delete every
speaker of the file, so the corrections went with them, and the voiceprint stage then
matched the new speakers against the profiles again — the section the user said was not
Bob became Bob again by the very comparison that had been wrong.

What the user judged was the AUDIO, so the correction is carried by it:

1. :func:`stash_file_corrections`, before a re-run deletes the speakers, writes one
   ``speaker_correction_carryover`` row per correction of a speaker of the file: that
   speaker's time spans and what it was ruled out from (a profile, or a speaker of
   ANOTHER file — a partner in the same file is being deleted too).
2. :func:`reapply_file_corrections`, when the re-run saves its segments
   (``tasks/transcription/storage.save_transcript_segments``, the one chokepoint both the
   transcription and the re-diarization write paths go through, and which runs before the
   voiceprint stage matches anything), gives each correction to every new speaker that
   SPOKE MOSTLY INSIDE those spans, through ``speaker_cleanup.record_unassignment``, and
   deletes the rows.

"Mostly" is :data:`INHERIT_SHARE` of the new speaker's own speech. The correction says
"this voice is not Bob": a new speaker that is predominantly that audio is that voice. A
speaker that merely touches it (a boundary, a turn the new diarization merged in) is not,
and blocking it could keep the real Bob from his own profile.

A new speaker with no transcribed speech inherits nothing; a correction no new speaker
inherits is dropped with its row (the re-run found no such voice). A correction carried by
a deleted profile, partner speaker or file goes with it (every foreign key cascades).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: A new speaker inherits a correction when at least this share of its own speech lies
#: inside the corrected speaker's spans.
INHERIT_SHARE = 0.5

_Spans = list[tuple[float, float]]


def _merged(spans: Iterable[tuple[float, float]]) -> _Spans:
    """Sorted, non-overlapping, positive-length spans."""
    out: _Spans = []
    for start, end in sorted((float(s), float(e)) for s, e in spans if e > s):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _length(spans: _Spans) -> float:
    return sum(end - start for start, end in spans)


def _overlap(a: _Spans, b: _Spans) -> float:
    """Total time two merged span lists share."""
    total, i, j = 0.0, 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            total += hi - lo
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def _spans_by_speaker(db: Session, media_file_id: int) -> dict[int, _Spans]:
    from app.models.media import TranscriptSegment

    raw: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for speaker_id, start, end in (
        db.query(
            TranscriptSegment.speaker_id, TranscriptSegment.start_time, TranscriptSegment.end_time
        )
        .filter(
            TranscriptSegment.media_file_id == media_file_id,
            TranscriptSegment.speaker_id.isnot(None),
        )
        .all()
    ):
        raw[int(speaker_id)].append((float(start), float(end)))
    return {sid: _merged(spans) for sid, spans in raw.items()}


def stash_file_corrections(db: Session, media_file_id: int) -> int:
    """Before a re-run deletes this file's speakers: keep their corrections by their audio.

    Call it while the speakers AND their segments still exist (the rediarize path nulls
    the segments' speaker before deleting speakers, so it must call this first). Adds to
    the session; the caller commits with its own delete.

    Returns:
        The number of corrections carried.
    """
    from sqlalchemy import or_

    from app.models.media import Speaker
    from app.models.media import SpeakerCannotLink
    from app.models.media import SpeakerCorrectionCarryover
    from app.models.media import SpeakerProfileBlacklist

    file_speakers = {
        int(r[0]) for r in db.query(Speaker.id).filter(Speaker.media_file_id == media_file_id)
    }
    if not file_speakers:
        return 0
    ids = sorted(file_speakers)
    # (speaker of this file, profile, partner of another file, reason)
    corrections: list[tuple[int, int | None, int | None, str | None]] = [
        (int(sid), int(pid), None, reason)
        for sid, pid, reason in db.query(
            SpeakerProfileBlacklist.speaker_id,
            SpeakerProfileBlacklist.profile_id,
            SpeakerProfileBlacklist.reason,
        )
        .filter(SpeakerProfileBlacklist.speaker_id.in_(ids))
        .all()
    ]
    seen_pairs: set[tuple[int, int]] = set()
    for a, b, reason in (
        db.query(
            SpeakerCannotLink.speaker_id,
            SpeakerCannotLink.cannot_link_speaker_id,
            SpeakerCannotLink.reason,
        )
        .filter(
            or_(
                SpeakerCannotLink.speaker_id.in_(ids),
                SpeakerCannotLink.cannot_link_speaker_id.in_(ids),
            )
        )
        .all()
    ):
        mine, partner = (int(a), int(b)) if int(a) in file_speakers else (int(b), int(a))
        if partner in file_speakers or (mine, partner) in seen_pairs:
            continue
        seen_pairs.add((mine, partner))
        corrections.append((mine, None, partner, reason))
    if not corrections:
        return 0

    spans = _spans_by_speaker(db, media_file_id)
    carried = 0
    for sid, profile_id, partner_id, reason in corrections:
        own = spans.get(sid)
        if not own:
            continue  # no transcribed speech: no audio to recognise it by
        db.add(
            SpeakerCorrectionCarryover(
                media_file_id=media_file_id,
                profile_id=profile_id,
                cannot_link_speaker_id=partner_id,
                spans=[[start, end] for start, end in own],
                reason=reason,
            )
        )
        carried += 1
    db.flush()
    if carried:
        logger.info(
            "Carrying %d speaker correction(s) of file %s across its re-run", carried, media_file_id
        )
    return carried


def reapply_file_corrections(db: Session, media_file_id: int) -> int:
    """After a re-run saved its segments: give each carried correction to its voice.

    Every new speaker of the file whose speech lies at least :data:`INHERIT_SHARE` inside
    a carried correction's spans inherits it; every carried row is then deleted. Adds to
    the session; the caller commits.

    Returns:
        The number of (speaker, correction) pairs recorded.
    """
    from app.models.media import Speaker
    from app.models.media import SpeakerCorrectionCarryover
    from app.services.speaker_cleanup import REASON_UNASSIGNED
    from app.services.speaker_cleanup import record_unassignment

    rows = (
        db.query(SpeakerCorrectionCarryover)
        .filter(SpeakerCorrectionCarryover.media_file_id == media_file_id)
        .all()
    )
    if not rows:
        return 0
    file_speakers = {
        int(r[0]) for r in db.query(Speaker.id).filter(Speaker.media_file_id == media_file_id)
    }
    spans = {
        sid: own
        for sid, own in _spans_by_speaker(db, media_file_id).items()
        if sid in file_speakers
    }
    applied = 0
    for row in rows:
        corrected = _merged((float(s), float(e)) for s, e in row.spans)
        heirs = [
            sid
            for sid, own in spans.items()
            if _length(own) > 0 and _overlap(own, corrected) >= INHERIT_SHARE * _length(own)
        ]
        for sid in heirs:
            record_unassignment(
                db,
                [sid],
                against_speaker_ids=[int(row.cannot_link_speaker_id)]
                if row.cannot_link_speaker_id is not None
                else [],
                profile_id=int(row.profile_id) if row.profile_id is not None else None,
                reason=row.reason or REASON_UNASSIGNED,
            )
            applied += 1
        db.delete(row)
    db.flush()
    logger.info(
        "Re-applied %d carried speaker correction(s) to the re-run of file %s",
        applied,
        media_file_id,
    )
    return applied
