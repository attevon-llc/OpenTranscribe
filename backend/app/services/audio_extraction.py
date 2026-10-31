"""Audio-track extraction and its derived-cache keys (mixin of ``VideoProcessingService``).

Split out of ``video_processing_service``. ``extract_audio`` opens no session of its own beyond
``_media_filename``'s short one, which closes before the MinIO download and the ffmpeg run.
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from app.core.config import settings
from app.services.video_processing_host import VideoProcessingHost
from app.services.video_subtitle_burn import _run_ffmpeg

logger = logging.getLogger(__name__)


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


class AudioExtractionMixin(VideoProcessingHost):
    """Extract and cache a media file's audio track (mixed into ``VideoProcessingService``)."""

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
