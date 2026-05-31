"""
Subtitle generation service for creating SRT files from transcript segments.
Handles movie-style formatting with proper line lengths and speaker labels.
Supports overlapping speech display where multiple speakers talk simultaneously.
"""

import io
import re
import textwrap
import zipfile
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.redaction.config import EffectiveRedactionConfig

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.utils.time_format import format_srt_timestamp
from app.utils.time_format import format_timestamp_simple


class SubtitleService:
    """Service for generating subtitle files from transcript data."""

    # Movie-style subtitle formatting constants (industry standards)
    MAX_LINE_LENGTH = 42  # Optimal for readability
    MAX_LINES_PER_SUBTITLE = 2
    MIN_DISPLAY_TIME = 1.0  # Minimum 1 second display time
    MAX_DISPLAY_TIME = 6.0  # Maximum 6 seconds display time
    READING_SPEED_WPM = 200  # Words per minute for subtitle timing
    MAX_CHARS_PER_SECOND = 20  # Maximum 20 characters per second for comfortable reading

    @staticmethod
    def format_timestamp(seconds: float) -> str:
        """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
        return format_srt_timestamp(seconds)

    @staticmethod
    def calculate_optimal_display_time(text: str) -> float:
        """Calculate optimal display time based on text length and reading speed."""
        word_count = len(text.split())
        char_count = len(text)

        # Calculate time needed based on reading speed (words per minute)
        reading_time = (word_count / SubtitleService.READING_SPEED_WPM) * 60

        # Also check character-per-second limit (industry standard)
        chars_per_second_time = char_count / SubtitleService.MAX_CHARS_PER_SECOND

        # Use the longer of the two to ensure comfortable reading
        optimal_time = max(reading_time + 0.5, chars_per_second_time)

        # Apply min/max constraints
        optimal_time = max(optimal_time, SubtitleService.MIN_DISPLAY_TIME)
        return min(optimal_time, SubtitleService.MAX_DISPLAY_TIME)

    @staticmethod
    def _get_speaker_prefix(speaker_name: str | None) -> str:
        """Get formatted speaker prefix from speaker name."""
        if not speaker_name or speaker_name.upper() == "UNKNOWN":
            return ""
        clean_name = speaker_name.strip()
        return f"{clean_name}: " if clean_name else ""

    @staticmethod
    def _wrap_text(text: str) -> list[str]:
        """Wrap text to subtitle line length constraints."""
        return textwrap.wrap(
            text,
            width=SubtitleService.MAX_LINE_LENGTH,
            break_long_words=False,
            break_on_hyphens=False,
        )

    @staticmethod
    def _group_overlapping_segments(
        segments: list[TranscriptSegment],
    ) -> list[list[TranscriptSegment]]:
        """Group segments by overlap_group_id for merged display.

        Returns a list of segment groups. Non-overlapping segments are in
        single-element lists, while overlapping segments are grouped together.
        """
        overlap_groups: dict[str, list[TranscriptSegment]] = defaultdict(list)
        non_overlap_segments: list[tuple[int, TranscriptSegment]] = []

        # Separate overlapping and non-overlapping segments
        for idx, segment in enumerate(segments):
            if segment.overlap_group_id:
                overlap_groups[str(segment.overlap_group_id)].append(segment)
            else:
                non_overlap_segments.append((idx, segment))

        # Build result maintaining original order
        processed_overlap_ids: set[str] = set()
        result_with_positions: list[tuple[float, list[TranscriptSegment]]] = []

        for segment in segments:
            if segment.overlap_group_id:
                group_id = str(segment.overlap_group_id)
                if group_id not in processed_overlap_ids:
                    processed_overlap_ids.add(group_id)
                    group = overlap_groups[group_id]
                    # Use the earliest start time for positioning
                    min_start = min(float(s.start_time) for s in group)
                    result_with_positions.append((min_start, group))
            else:
                result_with_positions.append((float(segment.start_time), [segment]))

        # Sort by start time and return just the groups
        result_with_positions.sort(key=lambda x: x[0])
        return [group for _, group in result_with_positions]

    @staticmethod
    def _redact_segments_inplace(
        segments: list[TranscriptSegment],
        redaction_cfg: "EffectiveRedactionConfig | None" = None,
        reveal_categories: set | None = None,
    ) -> None:
        """Mask segment text in memory (read-only export path; never committed).

        Mutating the loaded ORM objects' ``text`` makes every downstream subtitle
        formatter (overlap merge, long-segment split) inherit the masked text without
        touching each extraction site. The DB row is never updated (no commit here).
        """
        if redaction_cfg is None or not getattr(redaction_cfg, "enabled", False):
            return
        import contextlib

        from app.services.redaction.service import RedactionService

        for seg in segments:
            # Never break export rendering if masking a single segment fails.
            with contextlib.suppress(Exception):
                masked, _ = RedactionService.mask_segment(
                    str(seg.text or ""),
                    seg.redactions or [],
                    seg.words,
                    redaction_cfg,
                    reveal_categories or set(),
                )
                seg.text = masked

    @staticmethod
    def _format_overlap_for_subtitle(
        segments: list[TranscriptSegment],
        speaker_map: dict[int, str],
        format_type: str = "srt",
    ) -> tuple[float, float, str]:
        """Format overlapping segments as a single subtitle cue with speaker labels.

        Returns (start_time, end_time, formatted_text).
        """
        # Get time range spanning all segments
        start_time = min(float(s.start_time) for s in segments)
        end_time = max(float(s.end_time) for s in segments)

        # Format each speaker's text with their label
        lines = []
        for segment in sorted(segments, key=lambda s: float(s.start_time)):
            speaker_name = (
                speaker_map.get(segment.speaker_id, "Unknown") if segment.speaker_id else "Unknown"  # type: ignore[call-overload]
            )
            text = str(segment.text).strip()
            lines.append(f"[{speaker_name}] {text}")

        return start_time, end_time, "\n".join(lines)

    @staticmethod
    def _create_subtitle_block(lines: list[str], speaker_prefix: str) -> str:
        """Create a subtitle block from lines, adding speaker prefix to first line."""
        block_lines = lines[: SubtitleService.MAX_LINES_PER_SUBTITLE]
        if speaker_prefix:
            block_lines[0] = f"{speaker_prefix}{block_lines[0]}"
        return "\n".join(block_lines)

    @staticmethod
    def _process_sentence(
        sentence: str,
        current_subtitle: str,
        speaker_prefix: str,
        formatted_subtitles: list[str],
    ) -> str:
        """Process a sentence and add to subtitles if needed. Returns updated current_subtitle."""
        if not current_subtitle:
            return sentence

        test_text = f"{current_subtitle} {sentence}"
        test_lines = SubtitleService._wrap_text(test_text)

        if len(test_lines) <= SubtitleService.MAX_LINES_PER_SUBTITLE:
            return test_text

        # Finalize current subtitle and start new one
        subtitle_lines = SubtitleService._wrap_text(current_subtitle)
        formatted_subtitles.append(
            SubtitleService._create_subtitle_block(subtitle_lines, speaker_prefix)
        )
        return sentence

    @staticmethod
    def _finalize_remaining_text(
        current_subtitle: str, speaker_prefix: str, formatted_subtitles: list[str]
    ) -> None:
        """Process remaining text into subtitle blocks."""
        if not current_subtitle:
            return

        subtitle_lines = SubtitleService._wrap_text(current_subtitle)
        for i in range(0, len(subtitle_lines), SubtitleService.MAX_LINES_PER_SUBTITLE):
            chunk = subtitle_lines[i : i + SubtitleService.MAX_LINES_PER_SUBTITLE]
            formatted_subtitles.append(
                SubtitleService._create_subtitle_block(chunk, speaker_prefix)
            )

    @staticmethod
    def format_text_for_subtitles(
        text: str, speaker_name: str | None = None, format_type: str = "srt"
    ) -> list[str]:
        """Format text for movie-style subtitles with proper line breaks and speaker continuity."""
        # Clean the text
        text = re.sub(r"\s+", " ", text.strip())

        # Get speaker prefix
        speaker_prefix = SubtitleService._get_speaker_prefix(speaker_name)

        # If text with speaker prefix fits in one line, return as single subtitle
        full_text = f"{speaker_prefix}{text}"
        if len(full_text) <= SubtitleService.MAX_LINE_LENGTH:
            return [full_text]

        # For longer text, process sentences into subtitle blocks
        formatted_subtitles: list[str] = []
        sentences = re.split(r"(?<=[.!?])\s+", text)
        current_subtitle = ""

        for sentence in sentences:
            current_subtitle = SubtitleService._process_sentence(
                sentence, current_subtitle, speaker_prefix, formatted_subtitles
            )

        # Handle remaining text
        SubtitleService._finalize_remaining_text(
            current_subtitle, speaker_prefix, formatted_subtitles
        )

        return formatted_subtitles if formatted_subtitles else [full_text]

    @staticmethod
    def split_long_segment(
        segment: TranscriptSegment,
        speaker_name: str | None = None,
        format_type: str = "srt",
    ) -> list[tuple[float, float, str]]:
        """Split long transcript segments into properly timed subtitle chunks with speaker continuity."""
        text = str(segment.text or "").strip()
        start = float(segment.start_time or 0.0)
        end = float(segment.end_time or start)
        duration = end - start

        # Format text and handle multi-part subtitles
        formatted_subtitles = SubtitleService.format_text_for_subtitles(
            text, speaker_name, format_type
        )

        # If only one subtitle, return as-is
        if len(formatted_subtitles) == 1:
            optimal_duration = SubtitleService.calculate_optimal_display_time(text)
            actual_duration = min(duration, optimal_duration)
            return [
                (
                    start,
                    start + actual_duration,
                    formatted_subtitles[0],
                )
            ]

        # Handle multiple subtitle parts with proper timing distribution
        subtitle_parts: list[tuple[float, float, str]] = []
        total_chars = sum(len(subtitle) for subtitle in formatted_subtitles)

        current_time = start
        segment_end_time = end
        for i, subtitle_text in enumerate(formatted_subtitles):
            # Calculate time allocation based on text length (proportional distribution)
            text_ratio = len(subtitle_text) / total_chars
            allocated_time = duration * text_ratio

            # Apply minimum and maximum display time constraints
            optimal_duration = SubtitleService.calculate_optimal_display_time(subtitle_text)
            actual_duration = min(
                max(allocated_time, SubtitleService.MIN_DISPLAY_TIME), optimal_duration
            )

            end_time = current_time + actual_duration

            # Ensure we don't exceed the original segment end time
            if end_time > segment_end_time:
                end_time = segment_end_time

            # Ensure minimum gap between subtitles for readability
            if i < len(formatted_subtitles) - 1:  # Not the last subtitle
                min_gap = 0.1  # 100ms gap
                if end_time + min_gap > segment_end_time:
                    end_time = segment_end_time - min_gap

            subtitle_parts.append((current_time, end_time, subtitle_text))
            current_time = end_time + 0.1  # Small gap between subtitles

            # Prevent overlapping with segment end
            if current_time >= segment_end_time:
                break

        return subtitle_parts

    @staticmethod
    def generate_webvtt_content(
        db: Session,
        media_file_id: int,
        include_speakers: bool = True,
        redaction_cfg: "EffectiveRedactionConfig | None" = None,
        reveal_categories: set | None = None,
    ) -> str:
        """Generate WebVTT subtitle content from transcript segments.

        Handles overlapping speech by merging segments with the same overlap_group_id
        into a single subtitle cue with speaker labels on separate lines.
        """
        # Get media file and transcript segments
        media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
        if not media_file:
            raise ValueError(f"Media file with ID {media_file_id} not found")

        # Get all transcript segments ordered by start time
        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file_id)
            .order_by(TranscriptSegment.start_time)
            .all()
        )

        if not segments:
            raise ValueError("No transcript segments found for this media file")

        # Build speaker map for quick lookup
        speaker_map: dict[int, str] = {}
        if include_speakers:
            speaker_ids = {s.speaker_id for s in segments if s.speaker_id}
            speaker_rows = (
                db.query(Speaker.id, Speaker.name, Speaker.display_name)
                .filter(Speaker.id.in_(speaker_ids))
                .all()
            )
            for row in speaker_rows:
                speaker_map[row.id] = str(row.display_name) if row.display_name else str(row.name)

        # Read-time content redaction (after speaker_map; mutates text in memory only)
        SubtitleService._redact_segments_inplace(segments, redaction_cfg, reveal_categories)

        # Group overlapping segments
        segment_groups = SubtitleService._group_overlapping_segments(segments)

        # Start WebVTT content
        webvtt_content = "WEBVTT\n\n"

        for group in segment_groups:
            if len(group) > 1:
                # Overlapping segments - merge into single cue
                start_time, end_time, text = SubtitleService._format_overlap_for_subtitle(
                    group, speaker_map, "webvtt"
                )
                start_timestamp = SubtitleService.format_timestamp(start_time).replace(",", ".")
                end_timestamp = SubtitleService.format_timestamp(end_time).replace(",", ".")

                webvtt_content += f"{start_timestamp} --> {end_timestamp}\n{text}\n\n"
            else:
                # Single segment - process normally
                segment = group[0]
                speaker_name = speaker_map.get(segment.speaker_id) if segment.speaker_id else None  # type: ignore[call-overload]

                # Split long segments into properly formatted subtitles for WebVTT
                subtitle_parts = SubtitleService.split_long_segment(segment, speaker_name, "webvtt")

                for start_time, end_time, text in subtitle_parts:
                    # Format WebVTT timestamps (using dots instead of commas)
                    start_timestamp = SubtitleService.format_timestamp(start_time).replace(",", ".")
                    end_timestamp = SubtitleService.format_timestamp(end_time).replace(",", ".")

                    # Add WebVTT cue
                    webvtt_content += f"{start_timestamp} --> {end_timestamp}\n{text}\n\n"

        return webvtt_content

    @staticmethod
    def generate_srt_content(
        db: Session,
        media_file_id: int,
        include_speakers: bool = True,
        redaction_cfg: "EffectiveRedactionConfig | None" = None,
        reveal_categories: set | None = None,
    ) -> str:
        """Generate SRT subtitle content from transcript segments.

        Handles overlapping speech by merging segments with the same overlap_group_id
        into a single subtitle cue with speaker labels on separate lines.
        """
        # Get media file and transcript segments
        media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
        if not media_file:
            raise ValueError(f"Media file with ID {media_file_id} not found")

        # Get all transcript segments ordered by start time
        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file_id)
            .order_by(TranscriptSegment.start_time)
            .all()
        )

        if not segments:
            raise ValueError("No transcript segments found for this media file")

        # Build speaker map for quick lookup
        speaker_map: dict[int, str] = {}
        if include_speakers:
            speaker_ids = {s.speaker_id for s in segments if s.speaker_id}
            speaker_rows = (
                db.query(Speaker.id, Speaker.name, Speaker.display_name)
                .filter(Speaker.id.in_(speaker_ids))
                .all()
            )
            for row in speaker_rows:
                speaker_map[row.id] = str(row.display_name) if row.display_name else str(row.name)

        # Read-time content redaction (mutates text in memory only)
        SubtitleService._redact_segments_inplace(segments, redaction_cfg, reveal_categories)

        # Group overlapping segments
        segment_groups = SubtitleService._group_overlapping_segments(segments)

        srt_content = []
        subtitle_index = 1

        for group in segment_groups:
            if len(group) > 1:
                # Overlapping segments - merge into single cue
                start_time, end_time, text = SubtitleService._format_overlap_for_subtitle(
                    group, speaker_map, "srt"
                )
                start_timestamp = SubtitleService.format_timestamp(start_time)
                end_timestamp = SubtitleService.format_timestamp(end_time)

                srt_entry = f"{subtitle_index}\n{start_timestamp} --> {end_timestamp}\n{text}\n"
                srt_content.append(srt_entry)
                subtitle_index += 1
            else:
                # Single segment - process normally
                segment = group[0]
                speaker_name = speaker_map.get(segment.speaker_id) if segment.speaker_id else None  # type: ignore[call-overload]

                # Split long segments into properly formatted subtitles
                subtitle_parts = SubtitleService.split_long_segment(segment, speaker_name, "srt")

                for start_time, end_time, text in subtitle_parts:
                    # Format SRT entry
                    start_timestamp = SubtitleService.format_timestamp(start_time)
                    end_timestamp = SubtitleService.format_timestamp(end_time)

                    srt_entry = f"{subtitle_index}\n{start_timestamp} --> {end_timestamp}\n{text}\n"
                    srt_content.append(srt_entry)
                    subtitle_index += 1

        return "\n".join(srt_content)

    @staticmethod
    def generate_srt_file(
        db: Session,
        media_file_id: int,
        include_speakers: bool = True,
        output_path: str | None = None,
    ) -> str:
        """Generate SRT file and optionally save to disk."""
        srt_content = SubtitleService.generate_srt_content(db, media_file_id, include_speakers)

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(srt_content)

        return srt_content

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
            .order_by(TranscriptSegment.start_time)
            .all()
        )

        if not segments:
            raise ValueError("No transcript segments found for this media file")

        # Build speaker map for quick lookup
        speaker_map: dict[int, str] = {}
        if include_speakers:
            speaker_ids = {s.speaker_id for s in segments if s.speaker_id}
            speaker_rows = (
                db.query(Speaker.id, Speaker.name, Speaker.display_name)
                .filter(Speaker.id.in_(speaker_ids))
                .all()
            )
            for row in speaker_rows:
                speaker_map[row.id] = str(row.display_name) if row.display_name else str(row.name)

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
    ) -> tuple[bytes, int, int]:
        """Build a ZIP of subtitle files for a batch of media files.

        Args:
            db: Database session.
            file_specs: ``[(media_file_id, base_filename), ...]`` — already
                permission-filtered by the caller.
            subtitle_format: ``srt`` | ``webvtt`` | ``txt``.
            include_speakers: Whether to embed speaker labels.

        Returns:
            ``(zip_bytes, exported, skipped)``. Files whose subtitle generation
            fails or yields empty content are counted as skipped, never aborting
            the batch.
        """
        fmt = subtitle_format.lower()
        ext = "vtt" if fmt == "webvtt" else fmt
        buf = io.BytesIO()
        exported = skipped = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_id, base_name in file_specs:
                try:
                    if fmt == "webvtt":
                        content = SubtitleService.generate_webvtt_content(
                            db, file_id, include_speakers
                        )
                    elif fmt == "txt":
                        content = SubtitleService.generate_txt_content(
                            db, file_id, include_speakers
                        )
                    else:
                        content = SubtitleService.generate_srt_content(
                            db, file_id, include_speakers
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
    def validate_subtitle_timing(db: Session, media_file_id: int) -> list[str]:
        """Validate subtitle timing and return any issues found."""
        issues = []

        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file_id)
            .order_by(TranscriptSegment.start_time)
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
