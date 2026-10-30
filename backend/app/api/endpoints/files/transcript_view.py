"""Transcript page loading and shaping for file reads.

Loads a page of transcript segments, groups them for display, formats them (with read-time
masking) and applies the speaker privacy policy to every speaker row a transcript response
serializes.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.services.formatting_service import FormattingService
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.speaker_provenance_view import ProvenanceViewer
from app.services.speaker_provenance_view import attach_speaker_provenance


def _get_transcript_segments(
    db: Session, file_id: int, segment_limit: int | None, segment_offset: int
) -> tuple[list[TranscriptSegment], int]:
    """
    Get transcript segments with pagination and compute total count.

    Args:
        db: Database session
        file_id: File ID
        segment_limit: Maximum number of segments to return
        segment_offset: Offset for pagination

    Returns:
        Tuple of (list of transcript segments, total count)
    """
    from sqlalchemy import func
    from sqlalchemy.orm import joinedload

    total_segments = (
        db.query(func.count(TranscriptSegment.id))
        .filter(TranscriptSegment.media_file_id == file_id)
        .scalar()
    )

    # Eager-load each segment's speaker AND that speaker's linked profile so the
    # downstream add_computed_status (which reads speaker.profile) never triggers a
    # lazy SELECT per distinct speaker.
    segment_query = (
        db.query(TranscriptSegment)
        .options(joinedload(TranscriptSegment.speaker).selectinload(Speaker.profile))
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
    )

    if segment_offset > 0:
        segment_query = segment_query.offset(segment_offset)
    if segment_limit is not None:
        segment_query = segment_query.limit(segment_limit)

    return segment_query.all(), total_segments


def _count_speaker_segments(db: Session, file_id: int) -> int:
    """
    Count the number of speaker segments (adjacent same-speaker segments merged).

    This uses a SQL window function to detect speaker changes, counting
    the number of groups where consecutive segments share the same speaker.
    """
    from sqlalchemy import text

    result = db.execute(
        text("""
            SELECT COUNT(*) FROM (
                SELECT speaker_id,
                       LAG(speaker_id) OVER (ORDER BY start_time) AS prev_speaker_id
                FROM transcript_segment
                WHERE media_file_id = :file_id
            ) sub
            WHERE speaker_id IS DISTINCT FROM prev_speaker_id
               OR prev_speaker_id IS NULL
        """),
        {"file_id": file_id},
    ).scalar()
    return result or 0


def _build_grouped_segments(formatted_segments: list[Any], index_offset: int = 0) -> list[Any]:
    """Build the authoritative display grouping for a page of transcript segments.

    Consecutive segments sharing a non-null ``overlap_group_id`` are collapsed into a
    single overlap group **only when the run has more than one member**; every other
    segment becomes its own single-member group. Group ``start_time``/``end_time`` are
    the min/max across the group's segments.

    Groups reference their segments by UUID (``segment_uuids``) rather than embedding
    copies — see ``GroupedTranscriptSegment`` for why (issue #352).

    ``overlap_group_id`` is set on **every** group that has one, including single-member
    groups. A group whose overlap run straddles a pagination boundary arrives as two
    groups sharing that id, and the client stitches them back together by it; without
    the id on the single-member branch the tail of a split run would be unstitchable.

    Args:
        formatted_segments: Formatted ``TranscriptSegment`` schema objects, in order.
        index_offset: Absolute index of ``formatted_segments[0]`` in the full
            transcript. Pass the request's ``segment_offset`` so
            ``start_segment_index`` stays global across pagination — the SPA's
            reading-progress bar divides it by the total segment count, so page-local
            indices make progress jump backwards.

    Returns:
        List of ``GroupedTranscriptSegment`` schema objects.
    """
    from app.schemas.transcript import GroupedTranscriptSegment

    groups: list[Any] = []
    i = 0
    n = len(formatted_segments)

    while i < n:
        segment = formatted_segments[i]

        if segment.overlap_group_id:
            overlap_group_id = segment.overlap_group_id
            overlap_segments = [segment]
            j = i + 1
            while j < n and formatted_segments[j].overlap_group_id == overlap_group_id:
                overlap_segments.append(formatted_segments[j])
                j += 1

            if len(overlap_segments) > 1:
                groups.append(
                    GroupedTranscriptSegment(
                        is_overlap_group=True,
                        overlap_group_id=overlap_group_id,
                        start_time=min(s.start_time for s in overlap_segments),
                        end_time=max(s.end_time for s in overlap_segments),
                        start_segment_index=index_offset + i,
                        segment_uuids=[s.uuid for s in overlap_segments],
                    )
                )
                i = j
                continue

            # Single segment carrying an overlap flag: renders as a regular group, but
            # keep the id so a run split across a page boundary can be stitched.
            groups.append(
                GroupedTranscriptSegment(
                    is_overlap_group=False,
                    overlap_group_id=overlap_group_id,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    start_segment_index=index_offset + i,
                    segment_uuids=[segment.uuid],
                )
            )
            i += 1
        else:
            groups.append(
                GroupedTranscriptSegment(
                    is_overlap_group=False,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    start_segment_index=index_offset + i,
                    segment_uuids=[segment.uuid],
                )
            )
            i += 1

    return groups


def redact_response_speakers(
    db: Session,
    speakers: list[Speaker],
    transcript_segments: list[TranscriptSegment] | None = None,
    viewer: ProvenanceViewer | None = None,
) -> None:
    """Apply the privacy policy to every speaker row a transcript response will serialize.

    A segment embeds its full speaker, so the file's own speaker list and each segment's
    speaker are the same surface: both are redacted here, together, with the shared
    ``redact_speaker_rows`` (inferred gender and unverified voiceprint suggestions are
    withheld while the owner's scope has them off). Call it after the last commit of the
    request and before the rows are validated into response schemas, because a commit
    expires the instances and a reload would put the stored values back.

    Args:
        db: Database session.
        speakers: The file's speaker rows.
        transcript_segments: Segments whose ``speaker`` is serialized with them.
        viewer: Who is looking, for the read-only ``identity`` / ``suggestion`` provenance
            attached after the redaction (``None`` hides who made an identity).
    """
    unique: dict[int, Speaker] = {int(s.id): s for s in speakers}
    for segment in transcript_segments or []:
        if segment.speaker is not None:
            unique.setdefault(int(segment.speaker.id), segment.speaker)
    if unique:
        view = PrivacyView(db)
        redact_speaker_rows(view, unique.values())
        attach_speaker_provenance(db, unique.values(), viewer, view=view)


def _format_transcript_segments(
    transcript_segments: list[TranscriptSegment],
    speakers: list[Speaker],
    redaction_cfg: Any = None,
    reveal_categories: set | None = None,
) -> list[Any]:
    """
    Format transcript segments with speaker labels and timestamps.

    Args:
        transcript_segments: List of transcript segments (DB models)
        speakers: List of speakers for name mapping
        redaction_cfg: Optional ``EffectiveRedactionConfig`` for read-time masking
        reveal_categories: Categories an authorized owner chose to reveal

    Returns:
        List of formatted transcript segment schemas
    """
    speaker_mapping: dict[str, str] = {
        str(speaker.name): FormattingService.format_speaker_name(speaker) for speaker in speakers
    }

    return [
        FormattingService.format_transcript_segment(
            segment, speaker_mapping, redaction_cfg, reveal_categories
        )
        for segment in transcript_segments
    ]
