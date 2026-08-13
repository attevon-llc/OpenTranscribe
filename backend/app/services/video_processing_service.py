"""
Video processing service for embedding subtitles into video files.

**Session-lifetime note** (``app/tasks/CLAUDE.md``): the ffmpeg/MinIO entry
points here — ``process_video_with_subtitles``, ``embed_subtitles_in_video`` and
``extract_audio`` — used to take a caller's ``Session``, query with it, and then
run a MinIO download plus a full ffmpeg transcode with it still open. A
transcode is minutes; the transaction held ACCESS SHARE the whole time (queueing
any ``ALTER TABLE``, i.e. an Alembic upgrade), pinned the vacuum horizon and
consumed a pool connection. They now take plain arguments and open their **own**
short sessions for the two reads they need (the filename, and the transcript for
the SRT), so no transaction spans the slow work.
"""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import redis.asyncio as redis
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import VIDEO_CHUNK_SIZE
from app.db.session_utils import session_scope
from app.services.minio_service import MinIOService
from app.services.subtitle_service import SubtitleService

logger = logging.getLogger(__name__)


def _parse_range_header(range_header: str, total_length: int | None) -> tuple[int, int | None]:
    """
    Parse HTTP Range header and return start and end bytes.

    Args:
        range_header: The Range header value (e.g., "bytes=0-1023")
        total_length: Total file size in bytes, or None if unknown

    Returns:
        Tuple of (start_byte, end_byte) where end_byte may be None
    """
    if not range_header or not range_header.startswith("bytes="):
        return 0, total_length - 1 if total_length else None

    try:
        range_value = range_header.replace("bytes=", "")
        parts = range_value.split("-")

        # Format: bytes=start-end
        if parts[0] and parts[1]:
            start_byte = int(parts[0])
            end_byte = min(int(parts[1]), total_length - 1) if total_length else int(parts[1])
            return start_byte, end_byte

        # Format: bytes=start-
        if parts[0]:
            start_byte = int(parts[0])
            end_byte_value: int | None = total_length - 1 if total_length else None
            return start_byte, end_byte_value

        # Format: bytes=-end (last N bytes)
        if parts[1]:
            requested_length = int(parts[1])
            if total_length:
                start_byte = max(0, total_length - requested_length)
                return start_byte, total_length - 1
            return 0, None

    except Exception as e:
        logger.error(f"Error parsing range header '{range_header}': {e}")

    # Default fallback
    return 0, total_length - 1 if total_length else None


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


class NoAudioTrackError(Exception):
    """Raised when a media file has no audio stream to extract."""


# Map a source audio codec (from ffprobe) to a container extension + MIME type for
# lossless stream-copy ("original" audio download). Unknown codecs fall back to MP3.
_AUDIO_COPY_CONTAINERS: dict[str, tuple[str, str]] = {
    "aac": ("m4a", "audio/mp4"),
    "alac": ("m4a", "audio/mp4"),
    "mp3": ("mp3", "audio/mpeg"),
    "opus": ("opus", "audio/opus"),
    "vorbis": ("ogg", "audio/ogg"),
    "flac": ("flac", "audio/flac"),
    "ac3": ("ac3", "audio/ac3"),
    "eac3": ("eac3", "audio/eac3"),
    "pcm_s16le": ("wav", "audio/wav"),
    "pcm_s24le": ("wav", "audio/wav"),
}

# Re-encode presets: audio_format -> (extension, mime, ffmpeg output codec args).
_AUDIO_ENCODE_PRESETS: dict[str, tuple[str, str, list[str]]] = {
    "mp3": ("mp3", "audio/mpeg", ["-c:a", "libmp3lame", "-q:a", "2"]),
    "wav": ("wav", "audio/wav", ["-c:a", "pcm_s16le"]),
}

# Reverse map for recovering an "original" download's extension from the cached
# object's stored content type (cache key carries no extension).
_CONTENT_TYPE_TO_EXT: dict[str, str] = {ct: ext for ext, ct in _AUDIO_COPY_CONTAINERS.values()}
_CONTENT_TYPE_TO_EXT.update({"audio/mpeg": "mp3", "audio/wav": "wav", "audio/mp4": "m4a"})


def _probe_audio_codec(media_path: str) -> str | None:
    """Return the codec name of the first audio stream, or None if there is none."""
    import shutil

    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return None

    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(media_path),
    ]
    result = subprocess.run(  # noqa: S603 - validated path, internal input
        cmd,  # nosec B603
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(f"ffprobe failed for {media_path}: {result.stderr}")
        return None

    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return None
    if not streams:
        return None
    codec_name = streams[0].get("codec_name")
    return str(codec_name) if codec_name else None


def _build_audio_extract_command(
    ffmpeg_path: str, input_path: str, output_path: str, codec_args: list[str]
) -> list[str]:
    """Build an ffmpeg command that strips video and writes an audio-only file."""
    return [
        ffmpeg_path,
        "-i",
        str(input_path),
        "-vn",
        *codec_args,
        "-y",
        str(output_path),
    ]


class VideoProcessingService:
    """Service for processing video files, including subtitle embedding."""

    # Regenerable derived assets (subtitle-embedded videos + extracted audio) live under
    # this prefix so one MinIO lifecycle rule can auto-expire them. Originals are the
    # source of truth; these are duplicates re-created on demand.
    DERIVED_CACHE_PREFIX = "derived/"

    def __init__(self, minio_service: MinIOService):
        self.minio_service = minio_service
        self.cache_bucket = "processed-videos"
        self._ensure_cache_bucket_exists()

    async def _send_download_progress(
        self,
        user_id: int,
        file_id: int,
        status: str,
        progress: int | None = None,
        error: str | None = None,
    ):
        """Send download progress update via WebSocket."""
        try:
            redis_client = redis.from_url(settings.REDIS_URL)
            notification_data = {
                "user_id": user_id,
                "type": "download_progress",
                "data": {
                    "file_id": str(file_id),
                    "status": status,
                    "progress": progress,
                    "error": error,
                },
            }
            await redis_client.publish("websocket_notifications", json.dumps(notification_data))
            await redis_client.close()
            logger.info(
                f"Sent download progress update: user={user_id}, file={file_id}, status={status}"
            )
        except Exception as e:
            logger.error(f"Failed to send download progress update: {e}")

    def _send_download_progress_sync(
        self,
        user_id: int,
        file_id: int,
        status: str,
        progress: int | None = None,
        error: str | None = None,
    ):
        """Synchronous wrapper for sending download progress updates."""
        try:
            # Create new event loop for this thread if needed
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # Run the async function
            loop.run_until_complete(
                self._send_download_progress(user_id, file_id, status, progress, error)
            )
        except Exception as e:
            logger.error(f"Failed to send download progress update (sync): {e}")

    def _ensure_cache_bucket_exists(self):
        """Ensure the cache bucket exists."""
        try:
            if not self.minio_service.bucket_exists(self.cache_bucket):
                logger.info(f"Creating cache bucket: {self.cache_bucket}")
                self.minio_service.make_bucket(self.cache_bucket)
                logger.info(f"Cache bucket created successfully: {self.cache_bucket}")
            else:
                logger.info(f"Cache bucket already exists: {self.cache_bucket}")
        except Exception as e:
            logger.error(f"Failed to create cache bucket: {e}")
            raise

        # Auto-expire ephemeral bulk-export ZIPs (presigned URLs are short-lived, so a
        # fresh job_id is generated per request — the objects don't need to persist).
        # Idempotent + best-effort; does not affect long-lived video/audio cache objects.
        self.minio_service.ensure_prefix_expiry(
            self.cache_bucket, prefix="bulk/", days=1, rule_id="expire-bulk-exports"
        )
        # Bootstrap the derived-cache lifecycle rule from the env baseline ONLY if absent.
        # The authoritative value is the admin/DB setting, re-applied on startup and on
        # change via apply_derived_retention(); per-instance construction must never
        # clobber it (this object is created per task/request).
        if settings.DERIVED_CACHE_RETENTION_DAYS and settings.DERIVED_CACHE_RETENTION_DAYS > 0:
            self.minio_service.ensure_prefix_expiry(
                self.cache_bucket,
                prefix=self.DERIVED_CACHE_PREFIX,
                days=int(settings.DERIVED_CACHE_RETENTION_DAYS),
                rule_id="expire-derived-cache",
                update_if_exists=False,
            )

    def apply_derived_retention(self, days: int) -> None:
        """Authoritatively (re)apply the lifecycle rule that expires the derived cache.

        ``days <= 0`` removes the rule (keep derived assets forever). Best-effort.
        """
        if days and days > 0:
            self.minio_service.ensure_prefix_expiry(
                self.cache_bucket,
                prefix=self.DERIVED_CACHE_PREFIX,
                days=int(days),
                rule_id="expire-derived-cache",
                update_if_exists=True,
            )
        else:
            self.minio_service.remove_lifecycle_rule(self.cache_bucket, "expire-derived-cache")

    def generate_cache_key(
        self, file_id: int, original_filename: str, include_speakers: bool = True
    ) -> str:
        """Generate a cache key for processed video using original filename.

        Lives under the ``derived/`` prefix so a single MinIO lifecycle rule can
        auto-expire the regenerable derived cache (see ``DERIVED_CACHE_PREFIX``).
        """
        # Get base filename without extension
        base_name = (
            original_filename.rsplit(".", 1)[0] if "." in original_filename else original_filename
        )
        speaker_suffix = "_with_speakers" if include_speakers else "_no_speakers"
        return f"{self.DERIVED_CACHE_PREFIX}{base_name}{speaker_suffix}.mp4"

    def is_video_cached(self, cache_key: str) -> bool:
        """Check if a cached video exists."""
        try:
            # Check if cached video exists
            self.minio_service.stat_object(self.cache_bucket, cache_key)
            return True
        except Exception:
            return False

    def get_cached_video_stream(self, cache_key: str):
        """Get streaming response for a cached video."""
        try:
            # Use our custom cache bucket streaming function
            return self._get_cache_file_stream(cache_key)
        except Exception as e:
            logger.error(f"Error getting cached video stream: {e}")
            raise

    def _get_object_size(self, object_name: str) -> int | None:
        """Get the size of a cached object, or None if unavailable."""
        try:
            stats = self.minio_service.stat_object(self.cache_bucket, object_name)
            logger.info(f"Cached file size for {object_name}: {stats.size} bytes")
            return int(stats.size)  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Error getting cached object stats: {e}")
            return None

    def _create_chunk_generator(self, response, max_bytes: int | None):
        """Create a generator that yields chunks from the MinIO response."""
        chunk_size = VIDEO_CHUNK_SIZE

        def generate_chunks():
            try:
                bytes_read = 0
                while True:
                    # Adjust final chunk size if we're at the end of requested range
                    if max_bytes is not None and bytes_read + chunk_size > max_bytes:
                        final_chunk_size = max_bytes - bytes_read
                        if final_chunk_size <= 0:
                            break
                        chunk = response.read(final_chunk_size)
                    else:
                        chunk = response.read(chunk_size)

                    if not chunk:
                        break

                    bytes_read += len(chunk)
                    yield chunk
            finally:
                try:
                    response.close()
                    response.release_conn()
                except Exception as e:
                    logger.error(f"Error closing MinIO response: {e}")

        return generate_chunks()

    def _get_cache_file_stream(self, object_name: str, range_header: str | None = None):
        """Get a file stream from the cache bucket."""
        try:
            total_length = self._get_object_size(object_name)
            start_byte, end_byte = _parse_range_header(range_header or "", total_length)

            # Build MinIO request kwargs
            kwargs: dict[str, str | int] = {
                "bucket_name": self.cache_bucket,
                "object_name": object_name,
            }
            if range_header and range_header.startswith("bytes="):
                kwargs["offset"] = start_byte
                if end_byte is not None:
                    kwargs["length"] = end_byte - start_byte + 1

                logger.info(
                    f"Streaming cached video with range: start={start_byte}, "
                    f"end={end_byte if end_byte is not None else 'EOF'}, total={total_length}"
                )

            response = self.minio_service.client.get_object(**kwargs)  # type: ignore[arg-type]
            length_value = kwargs.get("length")
            chunks = self._create_chunk_generator(
                response, int(length_value) if length_value is not None else None
            )

            return chunks, start_byte, end_byte, total_length

        except Exception as e:
            logger.error(f"Error setting up cached file stream for {object_name}: {e}")
            raise Exception(f"Error streaming cached file: {e}") from e

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
    def _media_filename(file_id: int) -> str:
        """Read the file's original name in a short session of its own.

        Raises:
            Exception: If the media file does not exist.
        """
        from app.models.media import MediaFile

        with session_scope() as db:
            row = db.query(MediaFile.filename).filter(MediaFile.id == file_id).first()
            if not row:
                raise Exception(f"Media file {file_id} not found")
            return str(row[0])

    def _generate_subtitle_file(
        self, file_id: int, subtitle_path: Path, include_speakers: bool
    ) -> None:
        """Generate subtitle file from transcript segments.

        The transcript read gets its **own** short session, and the file write
        happens after it closes — this runs between a MinIO download and an
        ffmpeg transcode, so nothing here may leave a transaction open.
        """
        with session_scope() as db:
            subtitle_content = SubtitleService.generate_srt_content(db, file_id, include_speakers)
        subtitle_path.write_text(subtitle_content, encoding="utf-8")

    def _upload_to_cache(
        self,
        output_path: Path,
        cache_key: str,
        output_format: str,
        content_type: str | None = None,
    ) -> None:
        """Upload a processed file to the cache bucket.

        Args:
            output_path: Local path of the processed file.
            cache_key: Object name to store it under.
            output_format: Container/format used to derive a default content type.
            content_type: Explicit MIME type; falls back to ``video/{output_format}``.
        """
        logger.info(f"Uploading processed file to cache bucket: {self.cache_bucket}/{cache_key}")
        self.minio_service.upload_file(
            file_path=str(output_path),
            bucket_name=self.cache_bucket,
            object_name=cache_key,
            content_type=content_type or f"video/{output_format}",
        )
        logger.info("Upload complete, processing finished")

    def _process_video_in_temp_dir(
        self,
        file_id: int,
        original_video_path,
        user_id: int | None,
        include_speakers: bool,
        output_format: str,
        cache_key: str,
    ) -> str:
        """Process video with subtitles in temporary directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            subtitle_path = temp_dir_path / "subtitles.srt"

            # Generate subtitle file
            self._notify_progress(user_id, file_id, "processing", 20)
            self._generate_subtitle_file(file_id, subtitle_path, include_speakers)
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

        Returns:
            Path to the processed video file with embedded subtitles
        """
        filename = original_filename or self._media_filename(file_id)
        cache_key = self.generate_cache_key(file_id, filename, include_speakers)

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

        Returns:
            Presigned URL to download the processed video
        """
        filename = self._media_filename(file_id)
        cache_key = self.generate_cache_key(file_id, filename, include_speakers)

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
                )

            except Exception as e:
                logger.error(f"Failed to process video {file_id}: {e}")
                raise

    @staticmethod
    def _audio_base(original_filename: str) -> str:
        return (
            original_filename.rsplit(".", 1)[0] if "." in original_filename else original_filename
        )

    def audio_cache_key(self, original_filename: str, audio_format: str) -> str:
        """Deterministic cache key for an extracted audio file.

        ``mp3``/``wav`` carry their extension; ``original`` uses an extension-less key
        because the real extension depends on the (later-probed) source codec — it is
        recovered from the cached object's stored content type.
        """
        base = self._audio_base(original_filename)
        if audio_format == "original":
            return f"{self.DERIVED_CACHE_PREFIX}{base}_audio_original"
        ext = "mp3" if audio_format == "mp3" else "wav"
        return f"{self.DERIVED_CACHE_PREFIX}{base}_audio_{audio_format}.{ext}"

    def peek_cached_audio(
        self, original_filename: str, audio_format: str
    ) -> tuple[str, str, str] | None:
        """Return ``(cache_key, ext, content_type)`` if already cached, else None.

        Pure metadata lookup — never runs ffmpeg — so the API can hand back a
        presigned URL instantly on a cache hit.
        """
        normalized = audio_format.lower()
        if normalized not in ("mp3", "wav", "original"):
            normalized = "mp3"
        cache_key = self.audio_cache_key(original_filename, normalized)
        if not self.is_video_cached(cache_key):
            return None
        if normalized in _AUDIO_ENCODE_PRESETS:
            ext, content_type, _ = _AUDIO_ENCODE_PRESETS[normalized]
            return cache_key, ext, content_type
        # original: recover ext/content_type from the stored object metadata.
        try:
            stat = self.minio_service.stat_object(self.cache_bucket, cache_key)
            content_type = getattr(stat, "content_type", None) or "audio/mpeg"
        except Exception:
            content_type = "audio/mpeg"
        return cache_key, _CONTENT_TYPE_TO_EXT.get(content_type, "m4a"), content_type

    def presigned_download_url(
        self, cache_key: str, download_filename: str, content_type: str
    ) -> str:
        """Browser-reachable presigned GET URL for a cached object (forces download)."""
        from app.services.minio_service import get_presigned_download_url

        return get_presigned_download_url(
            cache_key,
            bucket_name=self.cache_bucket,
            download_filename=download_filename,
            content_type=content_type,
        )

    def extract_audio(
        self,
        file_id: int,
        original_object_name: str,
        audio_format: str = "mp3",
    ) -> tuple[str, str, str]:
        """Extract the audio track of a media file and cache it.

        Takes no ``Session``: the filename lookup gets its own short session,
        which closes before the MinIO download and the ffmpeg transcode below.

        Args:
            file_id: Media file ID.
            original_object_name: MinIO object name for the original media.
            audio_format: One of ``mp3``, ``wav`` or ``original`` (lossless stream copy).

        Returns:
            Tuple of ``(cache_key, extension, content_type)``.

        Raises:
            NoAudioTrackError: If the source has no audio stream.
            Exception: On ffmpeg/streaming failures.
        """
        original_filename = self._media_filename(file_id)

        normalized = audio_format.lower()
        if normalized not in ("mp3", "wav", "original"):
            normalized = "mp3"

        # Fast path: already cached (no download, no ffmpeg).
        cached = self.peek_cached_audio(original_filename, normalized)
        if cached:
            logger.info(f"Using cached audio for file {file_id} ({cached[0]})")
            return cached

        cache_key = self.audio_cache_key(original_filename, normalized)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            original_path = temp_dir_path / "original_media"

            self.minio_service.download_file(
                object_name=original_object_name,
                file_path=str(original_path),
                bucket_name=settings.MEDIA_BUCKET_NAME,
            )

            # Resolve the output codec/extension. "original" stream-copies the source
            # codec into a matching container; unknown codecs fall back to MP3.
            if normalized == "original":
                source_codec = _probe_audio_codec(str(original_path))
                if source_codec is None:
                    raise NoAudioTrackError("This file has no audio track to download.")
                ext, content_type = _AUDIO_COPY_CONTAINERS.get(source_codec, ("mp3", "audio/mpeg"))
                codec_args = (
                    ["-c:a", "copy"]
                    if source_codec in _AUDIO_COPY_CONTAINERS
                    else ["-c:a", "libmp3lame", "-q:a", "2"]
                )
            else:
                ext, content_type, codec_args = _AUDIO_ENCODE_PRESETS[normalized]

            import shutil

            ffmpeg_path = shutil.which("ffmpeg")
            if not ffmpeg_path:
                raise Exception("ffmpeg not found in system PATH")

            output_path = temp_dir_path / f"audio.{ext}"
            ffmpeg_cmd = _build_audio_extract_command(
                ffmpeg_path, str(original_path), str(output_path), codec_args
            )

            try:
                _run_ffmpeg(ffmpeg_cmd, file_id)
            except Exception as e:
                # ffmpeg reports a missing audio stream via its output-stream check.
                message = str(e).lower()
                if "does not contain any stream" in message or "no audio" in message:
                    raise NoAudioTrackError("This file has no audio track to download.") from e
                raise

            self._upload_to_cache(output_path, cache_key, ext, content_type=content_type)
            return cache_key, ext, content_type

    def derived_cache_keys(self, file_id: int, filename: str) -> list[str]:
        """Every derived-cache key a media file can own. Pure — no I/O, no DB.

        Both subtitle-embedded video variants and all three audio-extract
        variants. Shared by the delete paths so the list cannot drift between
        them.

        Args:
            file_id: Internal media file id.
            filename: The file's ORIGINAL filename, which the keys derive from.

        Returns:
            Cache keys under ``DERIVED_CACHE_PREFIX``.
        """
        return [
            self.generate_cache_key(file_id, filename, include_speakers=True),
            self.generate_cache_key(file_id, filename, include_speakers=False),
            self.audio_cache_key(filename, "mp3"),
            self.audio_cache_key(filename, "wav"),
            self.audio_cache_key(filename, "original"),
        ]

    def clear_derived_cache(self, file_id: int, filename: str) -> None:
        """Delete a file's derived-cache objects. **Takes no DB session.**

        Five MinIO round trips, so callers must not be holding a transaction:
        take the filename in the read phase and call this afterwards. Absent
        objects are not an error — these deletes are idempotent.

        Args:
            file_id: Internal media file id.
            filename: The file's original filename.
        """
        for cache_key in self.derived_cache_keys(file_id, filename):
            try:
                self.minio_service.delete_object(self.cache_bucket, cache_key)
                logger.info(f"Cleared cache for {cache_key}")
            except Exception as cache_error:
                # Cache file might not exist, which is fine, but we should log for debugging
                logger.debug(
                    f"Cache file {cache_key} not found or could not be deleted: {cache_error}"
                )

    def clear_cache_for_media_file(self, db: Session, file_id: int):
        """Clear cached processed videos for a media file, resolving the name via ``db``.

        ⚠️ **Known session-lifetime leak — do not call this from new code.** The
        filename is looked up on the CALLER's session, so a caller that is
        mid-transaction holds it across the five MinIO deletes below. Its
        remaining callers are the two request handlers in
        ``api/endpoints/speakers.py``, whose signature this change does not own;
        it is catalogued in ``scripts/session-lifetime-allowlist.txt`` under its
        own key until they move. Every other path resolves the filename in its
        own short read phase and calls :meth:`clear_derived_cache` afterwards.

        The delete loop is **deliberately inline rather than delegated** to
        :meth:`clear_derived_cache`: delegating would hide the still-open leak
        from ``scripts/audit-session-lifetime.py``, whose interprocedural rule
        does not recurse. A gate that reports zero on a live defect is worse
        than the six duplicated lines. Delete this method — not the duplication —
        once ``speakers.py`` stops passing a session.
        """
        try:
            # Get the MediaFile to access original filename
            from app.models.media import MediaFile

            db_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
            if not db_file:
                logger.warning(f"Media file {file_id} not found for cache clearing")
                return

            for cache_key in self.derived_cache_keys(file_id, str(db_file.filename)):
                try:
                    self.minio_service.delete_object(self.cache_bucket, cache_key)
                    logger.info(f"Cleared cache for {cache_key}")
                except Exception as cache_error:
                    # Cache file might not exist, which is fine, but we should log for debugging
                    logger.debug(
                        f"Cache file {cache_key} not found or could not be deleted: {cache_error}"
                    )
        except Exception as e:
            logger.error(f"Failed to clear cache for file {file_id}: {e}")

    def check_ffmpeg_availability(self) -> bool:
        """Check if ffmpeg is available on the system."""
        import shutil

        try:
            # Use shutil.which to find the full path to ffmpeg
            ffmpeg_path = shutil.which("ffmpeg")
            if not ffmpeg_path:
                logger.warning("ffmpeg not found in system PATH")
                return False

            # Using validated ffmpeg executable path from shutil.which() with hardcoded -version flag
            # No user input involved - only checking if ffmpeg is available on the system
            result = subprocess.run(  # noqa: S603 - hardcoded, no user input
                [ffmpeg_path, "-version"],  # nosec B603
                capture_output=True,
                text=True,
                timeout=10,
                check=False,  # Don't raise exception on non-zero return code
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Failed to check ffmpeg availability: {e}")
            return False
