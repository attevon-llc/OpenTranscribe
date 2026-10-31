"""Burned-in subtitle rendering (mixin of ``VideoProcessingService``).

Split out of ``video_processing_service``. Same session-lifetime contract as there: the two
reads (the reader's export policy, the transcript for the SRT) each open their **own** short
session, so no transaction spans the MinIO download or the ffmpeg transcode.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from app.core.config import settings
from app.db.session_utils import session_scope
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import resolve_effective_config
from app.services.redaction.export_policy import ExportRedactionNotReadyError
from app.services.redaction.export_policy import export_masking_is_pending
from app.services.redaction.export_policy import export_policy_fingerprint
from app.services.subtitle_service import SubtitleService
from app.services.video_processing_host import VideoProcessingHost

logger = logging.getLogger(__name__)


def _get_video_codecs(output_format: str) -> tuple[str, str, str]:
    """
    Get video and subtitle codecs for the given output format.

    Args:
        output_format: Output video format (mp4, mkv, etc.)

    Returns:
        Tuple of (video_codec, subtitle_codec, normalized_format)
    """
    format_lower = output_format.lower()

    if format_lower == "mp4":
        return "copy", "mov_text", "mp4"
    if format_lower == "mkv":
        return "copy", "srt", "mkv"

    # Default to mp4
    return "copy", "mov_text", "mp4"


def _validate_ffmpeg_paths(original_video_path, subtitle_path) -> str:
    """
    Validate paths and return ffmpeg executable path.

    Args:
        original_video_path: Path to the original video file
        subtitle_path: Path to the subtitle file

    Returns:
        Full path to ffmpeg executable

    Raises:
        Exception: If ffmpeg not found or paths are invalid
    """
    import shutil

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise Exception("ffmpeg not found in system PATH")

    original_path_obj = Path(original_video_path)
    if not original_path_obj.exists():
        raise Exception(f"Input video file not found: {original_video_path}")
    if not subtitle_path.exists():
        raise Exception(f"Subtitle file not found: {subtitle_path}")

    return ffmpeg_path


def _build_ffmpeg_command(
    ffmpeg_path: str,
    video_path: str,
    subtitle_path: str,
    output_path: str,
    video_codec: str,
    subtitle_codec: str,
) -> list[str]:
    """Build the ffmpeg command for embedding subtitles."""
    return [
        ffmpeg_path,
        "-i",
        str(video_path),
        "-i",
        str(subtitle_path),
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-map",
        "1:0",
        "-c:v",
        video_codec,
        "-c:a",
        "copy",
        "-c:s",
        subtitle_codec,
        "-disposition:s:0",
        "default",
        "-metadata:s:s:0",
        "language=eng",
        "-metadata:s:s:0",
        "title=English (Auto-generated)",
        "-y",
        str(output_path),
    ]


def _run_ffmpeg(ffmpeg_cmd: list[str], file_id: int) -> None:
    """
    Run ffmpeg command and handle errors.

    Args:
        ffmpeg_cmd: The ffmpeg command to run
        file_id: Media file ID for logging

    Raises:
        Exception: If ffmpeg fails or times out
    """
    logger.info(f"Running ffmpeg command: {' '.join(ffmpeg_cmd)}")

    # Using validated ffmpeg executable path with internally generated file paths
    # All paths are validated/sanitized by _validate_ffmpeg_paths() - not user input
    result = subprocess.run(  # noqa: S603 - validated paths, not user input
        ffmpeg_cmd,  # nosec B603
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    if result.returncode != 0:
        logger.error(f"ffmpeg failed with return code {result.returncode}")
        logger.error(f"ffmpeg stderr: {result.stderr}")
        logger.error(f"ffmpeg stdout: {result.stdout}")
        raise Exception(f"Video processing failed: {result.stderr}")

    logger.info(f"ffmpeg completed successfully for file {file_id}")


class VideoSubtitleBurnMixin(VideoProcessingHost):
    """Burn a transcript into a video (mixed into ``VideoProcessingService``)."""

    def _notify_progress(
        self,
        user_id: int | None,
        file_id: int,
        status: str,
        progress: int | None = None,
        error: str | None = None,
    ):
        """Send progress notification if user_id is provided."""
        if user_id:
            self._send_download_progress_sync(user_id, file_id, status, progress, error)

    @staticmethod
    def _resolve_export_policy(file_id: int, redaction_user_id: int) -> EffectiveRedactionConfig:
        """The reader's effective policy for this render, in a short session of its own.

        Resolved at RUN time, not captured at dispatch: an admin who tightens the
        redaction floor while an ffmpeg job is queued should be obeyed by the video
        that comes out. See ``services/redaction/export_policy.py`` for why the
        requesting user rather than the file owner.

        The tenant floor (issue #982/#987) consulted here is the FILE's own
        ``organization_id`` — this Celery task carries no request context to read
        the requester's own active org from (#988), and the file's tenant is what
        the floor governs regardless of which member is reading it.

        Raises:
            ExportRedactionNotReadyError: The policy masks this file but its detection
                scan has produced no spans yet.
        """
        from app.models.media import MediaFile

        with session_scope() as db:
            row = (
                db.query(MediaFile.redaction_status, MediaFile.organization_id)
                .filter(MediaFile.id == file_id)
                .first()
            )
            status = row.redaction_status if row else None
            organization_id = row.organization_id if row else None
            cfg = resolve_effective_config(db, redaction_user_id, organization_id=organization_id)
        if export_masking_is_pending(cfg, status):
            raise ExportRedactionNotReadyError(
                "Content redaction has not finished for this file, so its subtitles "
                "could not be masked. Try again once detection completes."
            )
        return cfg

    def _generate_subtitle_file(
        self,
        file_id: int,
        subtitle_path: Path,
        include_speakers: bool,
        redaction_cfg: EffectiveRedactionConfig,
    ) -> None:
        """Generate subtitle file from transcript segments.

        The transcript read gets its **own** short session, and the file write
        happens after it closes — this runs between a MinIO download and an
        ffmpeg transcode, so nothing here may leave a transaction open.

        ``redaction_cfg`` is required (issue #85). This SRT is about to be burned
        into pixels, so it is the least recoverable export surface in the product;
        it used to be rendered with no policy at all. Nothing is ever revealed
        here — the burn-in has no ``?redact=false`` counterpart.
        """
        with session_scope() as db:
            subtitle_content = SubtitleService.generate_srt_content(
                db, file_id, include_speakers, redaction_cfg
            )
        subtitle_path.write_text(subtitle_content, encoding="utf-8")

    def _process_video_in_temp_dir(
        self,
        file_id: int,
        original_video_path,
        user_id: int | None,
        include_speakers: bool,
        output_format: str,
        cache_key: str,
        redaction_cfg: EffectiveRedactionConfig,
    ) -> str:
        """Process video with subtitles in temporary directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            subtitle_path = temp_dir_path / "subtitles.srt"

            # Generate subtitle file
            self._notify_progress(user_id, file_id, "processing", 20)
            self._generate_subtitle_file(file_id, subtitle_path, include_speakers, redaction_cfg)
            self._notify_progress(user_id, file_id, "processing", 30)

            # Get codecs and normalize format
            video_codec, subtitle_codec, normalized_format = _get_video_codecs(output_format)
            output_path = temp_dir_path / f"output.{normalized_format}"

            # Validate paths and get ffmpeg
            ffmpeg_path = _validate_ffmpeg_paths(original_video_path, subtitle_path)

            # Build and run ffmpeg command
            ffmpeg_cmd = _build_ffmpeg_command(
                ffmpeg_path,
                original_video_path,
                str(subtitle_path),
                str(output_path),
                video_codec,
                subtitle_codec,
            )

            self._notify_progress(user_id, file_id, "processing", 50)
            _run_ffmpeg(ffmpeg_cmd, file_id)
            logger.info(f"Output file size: {os.path.getsize(output_path)} bytes")

            # Upload to cache
            self._notify_progress(user_id, file_id, "processing", 80)
            self._upload_to_cache(output_path, cache_key, normalized_format)
            self._notify_progress(user_id, file_id, "completed", 100)

            return cache_key

    def embed_subtitles_in_video(
        self,
        file_id: int,
        original_video_path,
        user_id: int | None = None,
        include_speakers: bool = True,
        output_format: str = "mp4",
        original_filename: str | None = None,
        *,
        redaction_cfg: EffectiveRedactionConfig,
    ) -> str:
        """
        Embed subtitles into a video file using ffmpeg.

        Takes no ``Session``: the ffmpeg run below is minutes long and must not
        share a transaction with anything. The two reads it needs open their own
        short sessions (see :meth:`_media_filename` / :meth:`_generate_subtitle_file`).

        Args:
            file_id: Media file ID
            original_video_path: Path to the original video file
            user_id: Recipient of progress notifications, if any
            include_speakers: Whether to include speaker labels
            output_format: Output video format (mp4, mkv, etc.)
            original_filename: Pre-read filename, so a caller that already has it
                does not pay for a second lookup.
            redaction_cfg: The reader's effective policy. Keyword-only and required —
                the burned-in subtitles are masked with it, and its fingerprint keys
                the cache so one reader's render is never served to another whose
                policy differs (issue #85).

        Returns:
            Path to the processed video file with embedded subtitles
        """
        filename = original_filename or self._media_filename(file_id)
        cache_key = self.generate_cache_key(
            file_id,
            filename,
            include_speakers,
            redaction_fingerprint=export_policy_fingerprint(redaction_cfg),
        )

        # Return cached version if available
        if self.is_video_cached(cache_key):
            logger.info(f"Using cached video for file {file_id}")
            self._notify_progress(user_id, file_id, "completed")
            return cache_key

        self._notify_progress(user_id, file_id, "processing", 10)

        try:
            return self._process_video_in_temp_dir(
                file_id,
                original_video_path,
                user_id,
                include_speakers,
                output_format,
                cache_key,
                redaction_cfg,
            )
        except subprocess.TimeoutExpired as e:
            logger.error(f"ffmpeg timeout for file {file_id}")
            self._notify_progress(user_id, file_id, "error", error="Video processing timeout")
            raise Exception("Video processing timeout") from e
        except Exception as e:
            logger.error(f"Video processing error for file {file_id}: {e}")
            self._notify_progress(user_id, file_id, "error", error=str(e))
            raise

    def process_video_with_subtitles(
        self,
        file_id: int,
        original_object_name: str,
        user_id: int | None = None,
        include_speakers: bool = True,
        output_format: str = "mp4",
        *,
        redaction_user_id: int,
    ) -> str:
        """
        Complete workflow to process a video with embedded subtitles.

        Takes no ``Session``. The filename lookup below happens in its own short
        session which closes **before** the MinIO download and the ffmpeg run.

        Args:
            file_id: Media file ID
            original_object_name: MinIO object name for the original video
            user_id: Recipient of progress notifications, if any
            include_speakers: Whether to include speaker labels
            output_format: Output video format
            redaction_user_id: Whose redaction policy masks the burned-in subtitles.
                Keyword-only and required: this render used to apply no policy at all,
                which is unrecoverable once the text is pixels (issue #85). Distinct
                from ``user_id``, which only addresses progress notifications and is
                deliberately ``None`` on the SSE path.

        Returns:
            Presigned URL to download the processed video

        Raises:
            ExportRedactionNotReadyError: The reader's policy masks this file and its
                detection scan has not produced spans yet.
        """
        # Resolved BEFORE the cache probe: the fingerprint is part of the key, and a
        # file whose scan is still running must be refused rather than served.
        redaction_cfg = self._resolve_export_policy(file_id, redaction_user_id)
        filename = self._media_filename(file_id)
        cache_key = self.generate_cache_key(
            file_id,
            filename,
            include_speakers,
            redaction_fingerprint=export_policy_fingerprint(redaction_cfg),
        )

        # Check cache first
        if self.is_video_cached(cache_key):
            return cache_key

        # Download original video to temporary location
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            original_path = temp_dir_path / "original_video"

            try:
                # Download original video from MinIO
                self.minio_service.download_file(
                    object_name=original_object_name,
                    file_path=str(original_path),
                    bucket_name=settings.MEDIA_BUCKET_NAME,
                )

                # Process video with subtitles
                return self.embed_subtitles_in_video(
                    file_id=file_id,
                    original_video_path=original_path,
                    user_id=user_id,
                    include_speakers=include_speakers,
                    output_format=output_format,
                    original_filename=filename,
                    redaction_cfg=redaction_cfg,
                )

            except Exception as e:
                logger.error(f"Failed to process video {file_id}: {e}")
                raise
