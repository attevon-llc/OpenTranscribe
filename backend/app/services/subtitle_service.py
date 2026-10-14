"""
Subtitle files (SRT / WebVTT / TXT) and the bulk subtitle archive.

SRT and WebVTT cues come from ``app.services.captions``; this module loads the file,
applies the export gates and serializes. TXT is a timestamped reading transcript, not
cues, and keeps its own formatter here.
"""

import io
import logging
import zipfile
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.captions import CaptionTrack
    from app.services.redaction.config import EffectiveRedactionConfig

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.utils.time_format import format_timestamp_simple

logger = logging.getLogger(__name__)


class SubtitleService:
    """Service for generating subtitle files from transcript data.

    SRT and WebVTT cue segmentation is NOT done here: both go through
    ``app.services.captions`` (the one segmenter, shared with the file-detail player's
    caption track and the transcript export), so a download and the player agree.
    """

    @staticmethod
    def _speaker_map(db: Session, segments: list[TranscriptSegment]) -> dict[int, str]:
        """``{speaker_id: canonical label}`` for the speakers these segments name."""
        from app.services.captions.source import speaker_labels_for

        return speaker_labels_for(db, {int(s.speaker_id) for s in segments if s.speaker_id})

    @staticmethod
    def _group_overlapping_segments(
        segments: list[TranscriptSegment],
    ) -> list[list[TranscriptSegment]]:
        """Group segments by overlap_group_id for merged display.

        Returns a list of segment groups. Non-overlapping segments are in
        single-element lists, while overlapping segments are grouped together.
        """
        overlap_groups: dict[str, list[TranscriptSegment]] = defaultdict(list)

        for segment in segments:
            if segment.overlap_group_id:
                overlap_groups[str(segment.overlap_group_id)].append(segment)

        processed_overlap_ids: set[str] = set()
        result_with_positions: list[tuple[float, list[TranscriptSegment]]] = []

        for segment in segments:
            if segment.overlap_group_id:
                group_id = str(segment.overlap_group_id)
                if group_id not in processed_overlap_ids:
                    processed_overlap_ids.add(group_id)
                    group = overlap_groups[group_id]
                    min_start = min(float(s.start_time) for s in group)
                    result_with_positions.append((min_start, group))
            else:
                result_with_positions.append((float(segment.start_time), [segment]))

        result_with_positions.sort(key=lambda x: x[0])
        return [group for _, group in result_with_positions]

    @staticmethod
    def _redact_segments_inplace(
        segments: list[TranscriptSegment],
        redaction_cfg: "EffectiveRedactionConfig | None" = None,
        reveal_categories: set | None = None,
    ) -> None:
        """Mask segment text in memory (read-only export path; never committed).

        Mutating the loaded ORM objects' ``text`` makes every downstream formatter
        inherit the masked text without touching each extraction site.

        ⚠️ **Each segment is EXPUNGED from its session before it is mutated**, and that
        is not tidiness. "The DB row is never updated (no commit here)" was true only of
        the API request path, where ``get_db`` closes without committing. Both export
        workers run inside ``session_scope``, which **commits on exit** — so wiring
        masking into them (issue #85) would have flushed the masked text back into
        ``transcript_segment.text`` and destroyed the original transcript, the one thing
        this whole subsystem promises never to do (read-time masking over cached spans).
        Detaching makes the guarantee structural instead of a property of whichever
        caller happens to hold the session. Only already-loaded column values are read
        downstream (``speaker_id`` and a separately-queried speaker map, never
        ``seg.speaker``), so nothing lazy-loads off a detached instance.

        ``blur`` is masked as ``label`` (``export_policy.plain_text_policy``): its
        placeholder wraps the ORIGINAL text for the page to blur with CSS, and a file has
        no CSS.
        """
        if redaction_cfg is None or not getattr(redaction_cfg, "enabled", False):
            return

        from sqlalchemy.orm import object_session

        from app.services.redaction.export_policy import plain_text_policy
        from app.services.redaction.service import RedactionService

        cfg = plain_text_policy(redaction_cfg)
        for seg in segments:
            session = object_session(seg)
            if session is not None:
                session.expunge(seg)
            try:
                masked, _ = RedactionService.mask_segment(
                    str(seg.text or ""),
                    seg.redactions or [],
                    seg.words,
                    cfg,
                    reveal_categories or set(),
                )
            except Exception:
                # FAIL CLOSED. The assignment used to sit inside a
                # `contextlib.suppress(Exception)`, so a masking failure left
                # `seg.text` as the raw DB text and every downstream formatter
                # wrote it into the exported subtitle file. Redaction is enabled
                # here by definition, so substitute a placeholder: a rendering
                # gap is recoverable, an exported unmasked file is not.
                logger.exception(
                    "Redaction masking failed for a segment; withholding its text from the export"
                )
                seg.text = "[redacted — masking unavailable]"
                continue
            seg.text = masked

    @staticmethod
    def _caption_track(
        db: Session,
        media_file_id: int,
        include_speakers: bool,
        redaction_cfg: "EffectiveRedactionConfig | None",
        reveal_categories: set | None,
    ) -> "CaptionTrack":
        from app.services.captions import build_caption_track
        from app.services.captions.source import load_caption_segments

        if db.query(MediaFile.id).filter(MediaFile.id == media_file_id).first() is None:
            raise ValueError(f"Media file with ID {media_file_id} not found")
        return build_caption_track(
            load_caption_segments(
                db,
                media_file_id,
                include_speakers=include_speakers,
                redaction_cfg=redaction_cfg,
                reveal_categories=reveal_categories,
            )
        )

    @staticmethod
    def generate_webvtt_content(
        db: Session,
        media_file_id: int,
        include_speakers: bool = True,
        redaction_cfg: "EffectiveRedactionConfig | None" = None,
        reveal_categories: set | None = None,
    ) -> str:
        """WebVTT subtitles, cut into broadcast-style cues by ``app.services.captions``."""
        from app.services.captions import to_webvtt

        return to_webvtt(
            SubtitleService._caption_track(
                db, media_file_id, include_speakers, redaction_cfg, reveal_categories
            )
        )

    @staticmethod
    def generate_srt_content(
        db: Session,
        media_file_id: int,
        include_speakers: bool = True,
        redaction_cfg: "EffectiveRedactionConfig | None" = None,
        reveal_categories: set | None = None,
    ) -> str:
        """SRT subtitles, cut into broadcast-style cues by ``app.services.captions``."""
        from app.services.captions import to_srt

        return to_srt(
            SubtitleService._caption_track(
                db, media_file_id, include_speakers, redaction_cfg, reveal_categories
            )
        )

    @staticmethod
    def format_timestamp_simple(seconds: float) -> str:
        """Convert seconds to simple MM:SS or H:MM:SS format."""
        return format_timestamp_simple(seconds)

    @staticmethod
    def generate_txt_content(
        db: Session,
        media_file_id: int,
        include_speakers: bool = True,
        redaction_cfg: "EffectiveRedactionConfig | None" = None,
        reveal_categories: set | None = None,
    ) -> str:
        """Generate plain text transcript with timestamps and speaker labels.

        For overlapping speech, formats as:
        [00:02:15 - 00:02:20] OVERLAPPING SPEECH:
          Alice (00:02:15 - 00:02:18): That's a great idea but--
          Bob (00:02:16 - 00:02:20): I completely disagree!
        """
        # Get media file and transcript segments
        media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
        if not media_file:
            raise ValueError(f"Media file with ID {media_file_id} not found")

        # Get all transcript segments ordered by start time
        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file_id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )

        if not segments:
            raise ValueError("No transcript segments found for this media file")

        speaker_map = SubtitleService._speaker_map(db, segments) if include_speakers else {}

        # Read-time content redaction (mutates text in memory only)
        SubtitleService._redact_segments_inplace(segments, redaction_cfg, reveal_categories)

        # Group overlapping segments
        segment_groups = SubtitleService._group_overlapping_segments(segments)

        # Flatten segment groups into a list of (speaker_id, segment) tuples,
        # preserving overlap groups as special entries
        flat_entries: list[dict] = []
        for group in segment_groups:
            if len(group) > 1:
                # Overlapping segments - keep as special block (not combinable)
                group_start = min(float(s.start_time) for s in group)
                group_end = max(float(s.end_time) for s in group)
                lines = []
                for segment in sorted(group, key=lambda s: float(s.start_time)):
                    s_name = (
                        speaker_map.get(segment.speaker_id, "Unknown")  # type: ignore[call-overload]
                        if segment.speaker_id
                        else "Unknown"
                    )
                    seg_start = SubtitleService.format_timestamp_simple(float(segment.start_time))
                    seg_end = SubtitleService.format_timestamp_simple(float(segment.end_time))
                    text = str(segment.text).strip()
                    if include_speakers:
                        lines.append(f"  {s_name} ({seg_start} - {seg_end}): {text}")
                    else:
                        lines.append(f"  ({seg_start} - {seg_end}): {text}")
                start_ts = SubtitleService.format_timestamp_simple(group_start)
                end_ts = SubtitleService.format_timestamp_simple(group_end)
                flat_entries.append(
                    {
                        "type": "overlap",
                        "text": f"[{start_ts} - {end_ts}] OVERLAPPING SPEECH:\n" + "\n".join(lines),
                    }
                )
            else:
                segment = group[0]
                flat_entries.append(
                    {
                        "type": "single",
                        "speaker_id": segment.speaker_id,
                        "start_time": float(segment.start_time),
                        "end_time": float(segment.end_time),
                        "text": str(segment.text).strip(),
                    }
                )

        # Combine consecutive single segments from the same speaker
        txt_lines = []
        i = 0
        while i < len(flat_entries):
            entry = flat_entries[i]
            if entry["type"] == "overlap":
                txt_lines.append(entry["text"])
                i += 1
                continue

            # Start a new speaker group
            speaker_id = entry["speaker_id"]
            group_start = entry["start_time"]
            group_end = entry["end_time"]
            texts = [entry["text"]]

            # Merge consecutive single segments with the same speaker
            j = i + 1
            while j < len(flat_entries):
                next_entry = flat_entries[j]
                if next_entry["type"] != "single" or next_entry["speaker_id"] != speaker_id:
                    break
                group_end = next_entry["end_time"]
                texts.append(next_entry["text"])
                j += 1

            speaker_name = (
                speaker_map.get(speaker_id, "Unknown")  # type: ignore[call-overload]
                if speaker_id
                else "Unknown"
            )
            start_ts = SubtitleService.format_timestamp_simple(group_start)
            end_ts = SubtitleService.format_timestamp_simple(group_end)
            combined_text = " ".join(texts)

            if include_speakers:
                txt_lines.append(f"[{start_ts} - {end_ts}] {speaker_name}:\n{combined_text}")
            else:
                txt_lines.append(f"[{start_ts} - {end_ts}]\n{combined_text}")

            i = j

        return "\n\n".join(txt_lines)

    @staticmethod
    def build_subtitle_archive(
        db: Session,
        file_specs: list[tuple[int, str]],
        subtitle_format: str,
        include_speakers: bool,
        redaction_cfg: "EffectiveRedactionConfig",
        *,
        include_quarantined: bool = False,
    ) -> tuple[bytes, int, int]:
        """Build a ZIP of subtitle files for a batch of media files.

        ``redaction_cfg`` is **required and not optional** (issue #85). It used to be
        absent entirely, so every generator here received ``None`` — which
        :meth:`_redact_segments_inplace` reads as "redaction is disabled", a state
        indistinguishable from "the caller forgot to pass one". The batch therefore
        exported the raw transcript for every user, including under the admin
        ``force_export_redacted`` floor. A disabled policy is now spelled by an
        ``EffectiveRedactionConfig`` whose ``enabled`` is False, and there is no
        argument value that means "unspecified".

        Nothing is ever revealed here: the bulk endpoint has no ``?redact=false``
        counterpart, so the archive is always the masked view of the requesting user's
        policy.

        Args:
            db: Database session.
            file_specs: ``[(media_file_id, base_filename), ...]`` — already
                permission-filtered by the caller.
            subtitle_format: ``srt`` | ``webvtt`` | ``txt``.
            include_speakers: Whether to embed speaker labels.
            redaction_cfg: The **requesting user's** effective policy — see
                ``services/redaction/export_policy.py`` for why the reader and not the
                file owner.
            include_quarantined: Admin review bypass (issue #818). Default False: a
                file taken down between dispatch and this build is skipped like any
                other unusable entry, never exported.

        Returns:
            ``(zip_bytes, exported, skipped)``. Files whose subtitle generation fails,
            whose redaction scan has not produced spans yet, that were taken down
            after dispatch, or that yield empty content are counted as skipped, never
            aborting the batch.
        """
        fmt = subtitle_format.lower()
        ext = "vtt" if fmt == "webvtt" else fmt
        buf = io.BytesIO()
        exported = skipped = 0
        file_ids = [fid for fid, _ in file_specs]
        withheld = SubtitleService._files_awaiting_redaction(db, file_ids, redaction_cfg)
        taken_down = SubtitleService._files_withheld_by_takedown(
            db, file_ids, include_quarantined=include_quarantined
        )
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_id, base_name in file_specs:
                try:
                    if file_id in withheld:
                        # The single-file export answers 409 for this; a batch skips
                        # the file rather than failing the other 99.
                        logger.info(
                            f"Skipping file {file_id} in bulk export: "
                            "content redaction has not finished for it"
                        )
                        skipped += 1
                        continue
                    if file_id in taken_down:
                        logger.info(
                            f"Skipping file {file_id} in bulk export: it was taken down "
                            "(or removed) after this export was requested"
                        )
                        skipped += 1
                        continue
                    if fmt == "webvtt":
                        content = SubtitleService.generate_webvtt_content(
                            db, file_id, include_speakers, redaction_cfg
                        )
                    elif fmt == "txt":
                        content = SubtitleService.generate_txt_content(
                            db, file_id, include_speakers, redaction_cfg
                        )
                    else:
                        content = SubtitleService.generate_srt_content(
                            db, file_id, include_speakers, redaction_cfg
                        )
                    if not content.strip():
                        skipped += 1
                        continue
                    zf.writestr(f"{base_name}.{ext}", content.encode("utf-8"))
                    exported += 1
                except Exception:  # noqa: BLE001 - one bad file shouldn't abort the batch
                    skipped += 1
        return buf.getvalue(), exported, skipped

    @staticmethod
    def _files_awaiting_redaction(
        db: Session, file_ids: list[int], redaction_cfg: "EffectiveRedactionConfig"
    ) -> set[int]:
        """Which of ``file_ids`` must not be exported yet, in ONE query.

        Masking reads each segment's *cached* spans, so a file whose scan never ran or
        is still running has nothing to apply and would export raw — with nothing in
        the produced file to say so.
        """
        from app.services.redaction.export_policy import export_masking_is_pending

        if not redaction_cfg.enabled or not file_ids:
            return set()
        rows = (
            db.query(MediaFile.id, MediaFile.redaction_status)
            .filter(MediaFile.id.in_(file_ids))
            .all()
        )
        known = {int(fid): status for fid, status in rows}
        # A file id with no row at all is left to the generator, which raises and
        # counts it as skipped like any other unusable entry.
        return {
            fid
            for fid in file_ids
            if fid in known and export_masking_is_pending(redaction_cfg, known[fid])
        }

    @staticmethod
    def _files_withheld_by_takedown(
        db: Session, file_ids: list[int], *, include_quarantined: bool
    ) -> set[int]:
        """Which of file_ids must not be exported because of an abuse/DMCA takedown.

        The complement of takedown_service.exclude_quarantined over the same ids, so the
        quarantine predicate keeps exactly one spelling in the codebase. A file DELETED
        between dispatch and execution also lands here — it cannot be proven visible.

        The prepare endpoint already permission-filtered these ids, but a takedown can
        land AFTER dispatch and before the worker runs — the whole reason quarantine is a
        run-time predicate, not a dispatch-time one.
        """
        from app.services.takedown_service import exclude_quarantined

        if include_quarantined or not file_ids:
            return set()
        visible = {
            int(row[0])
            for row in exclude_quarantined(
                db.query(MediaFile.id).filter(MediaFile.id.in_(file_ids))
            ).all()
        }
        return {fid for fid in file_ids if fid not in visible}

    @staticmethod
    def validate_subtitle_timing(db: Session, media_file_id: int) -> list[str]:
        """Validate subtitle timing and return any issues found."""
        issues = []

        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file_id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )

        prev_end = 0.0
        for i, segment in enumerate(segments):
            start_time = float(segment.start_time)
            end_time = float(segment.end_time)

            # Check for negative duration
            if end_time <= start_time:
                issues.append(f"Segment {i + 1}: Invalid duration (end <= start)")

            # Check for overlapping segments
            if start_time < prev_end:
                issues.append(f"Segment {i + 1}: Overlaps with previous segment")

            # Check for extremely short segments
            duration = end_time - start_time
            if duration < 0.1:  # Less than 100ms
                issues.append(f"Segment {i + 1}: Very short duration ({duration:.2f}s)")

            prev_end = end_time

        return issues
