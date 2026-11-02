"""
Media download service for downloading and processing media from various platforms.

This service handles all media URL-related operations including URL validation,
video downloading, metadata extraction, and integration with the media processing pipeline.
Supports YouTube, Vimeo, Twitter/X, TikTok, and 1800+ other platforms via yt-dlp.
"""

import io
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.core.exceptions import OperationFailedError
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.media_download_formats import _YOUTUBE_EXTRACTOR_ARGS
from app.services.media_download_formats import _YT_DLP_BASE_OPTS
from app.services.media_download_formats import _build_yt_dlp_format_string
from app.services.media_download_formats import _select_quality_from_available_formats
from app.services.media_download_metadata import MediaMetadataMixin
from app.services.media_download_playlist import PlaylistMixin
from app.services.media_download_thumbnails import _get_thumbnail_with_fallback
from app.services.minio_service import upload_file_tuned
from app.services.protected_media_providers import PROTECTED_MEDIA_PROVIDERS
from app.services.protected_media_providers import ProtectedMediaProvider
from app.utils.url_validation import is_safe_url

logger = logging.getLogger(__name__)

# Generic URL pattern - accepts any HTTP/HTTPS URL
GENERIC_URL_PATTERN = re.compile(r"^https?://.+$")

# YouTube URL validation regex - supports both videos and playlists
YOUTUBE_URL_PATTERN = re.compile(
    r"^https?://(www\.)?(youtube\.com/(watch\?v=|embed/|v/|playlist\?list=)|youtu\.be/)[\w\-_]+.*$"
)

# YouTube playlist URL validation regex
YOUTUBE_PLAYLIST_PATTERN = re.compile(
    r"^https?://(www\.)?youtube\.com/playlist\?list=([\w\-_]+).*$"
)


def _find_downloaded_file(output_path: str, clean_title: str, ext: str) -> str:
    """
    Find the downloaded file in the output directory.

    Args:
        output_path: Directory where file was downloaded
        clean_title: Cleaned title for expected filename
        ext: Expected file extension

    Returns:
        Path to the downloaded file

    Raises:
        FileNotFoundError: If no video file is found
    """
    expected_filename = f"{clean_title}.{ext}"
    downloaded_file = os.path.join(output_path, expected_filename)

    if os.path.exists(downloaded_file):
        return downloaded_file

    # Look for any video file in the directory (yt-dlp might change the name)
    for file in os.listdir(output_path):
        if file.endswith((".mp4", ".webm", ".mkv", ".avi")):
            return os.path.join(output_path, file)

    raise FileNotFoundError("Downloaded file not found")


def _update_media_file_with_download_data(
    media_file: MediaFile,
    media_info: dict[str, Any],
    media_metadata: dict[str, Any],
    technical_metadata: dict[str, Any],
    storage_path: str,
    file_size: int,
    thumbnail_path: str | None,
    original_filename: str,
    source_url: str,
    imohash_value: str | None = None,
) -> None:
    """
    Update MediaFile record with downloaded media and technical metadata.

    Args:
        media_file: MediaFile to update
        media_info: Media video info from yt-dlp
        media_metadata: Prepared media metadata dict
        technical_metadata: Technical metadata from file
        storage_path: Storage path in MinIO
        file_size: File size in bytes
        thumbnail_path: Path to thumbnail
        original_filename: Original filename
        source_url: Original media URL
        imohash_value: Optional imohash fingerprint computed from the
            downloaded file. Enables server-side duplicate detection and
            artifact cache lookups across different source URLs pointing
            at the same content.
    """
    media_file.filename = media_info.get("title", original_filename)[:255]  # type: ignore[assignment]
    media_file.storage_path = storage_path  # type: ignore[assignment]
    media_file.file_size = file_size  # type: ignore[assignment]
    media_file.content_type = technical_metadata.get("content_type", "video/mp4")  # type: ignore[assignment]
    media_file.duration = technical_metadata.get("duration") or media_info.get("duration")  # type: ignore[assignment]
    media_file.status = FileStatus.PENDING  # type: ignore[assignment]
    media_file.thumbnail_path = thumbnail_path  # type: ignore[assignment]
    if imohash_value:
        media_file.imohash = imohash_value  # type: ignore[assignment]

    # Media-specific metadata
    media_file.title = media_info.get("title")  # type: ignore[assignment]
    media_file.author = media_info.get("uploader")  # type: ignore[assignment]
    media_file.description = media_info.get("description")  # type: ignore[assignment]
    media_file.source_url = source_url  # type: ignore[assignment]
    media_file.metadata_raw = media_metadata  # type: ignore[assignment]
    media_file.metadata_important = media_metadata  # type: ignore[assignment]

    # Technical metadata from extraction
    media_file.media_format = technical_metadata.get("format")  # type: ignore[assignment]
    media_file.codec = technical_metadata.get("video_codec")  # type: ignore[assignment]
    media_file.frame_rate = technical_metadata.get("frame_rate")  # type: ignore[assignment]
    media_file.resolution_width = technical_metadata.get("width")  # type: ignore[assignment]
    media_file.resolution_height = technical_metadata.get("height")  # type: ignore[assignment]
    media_file.audio_channels = technical_metadata.get("audio_channels")  # type: ignore[assignment]
    media_file.audio_sample_rate = technical_metadata.get("audio_sample_rate")  # type: ignore[assignment]


class MediaDownloadService(PlaylistMixin, MediaMetadataMixin):
    """Service for processing media from various platforms.

    Uses yt-dlp for public platforms and a pluggable registry of
    ProtectedMediaProvider implementations for authenticated sites
    (for example, internal corporate media portals).
    """

    def __init__(self):
        pass

    def _get_protected_provider(
        self, url: str, user_id: int | None = None
    ) -> ProtectedMediaProvider | None:
        """Return a protected media provider that can handle this URL, if any."""
        for provider in PROTECTED_MEDIA_PROVIDERS:
            try:
                if provider.can_handle(url, user_id=user_id):
                    return provider
            except Exception as e:
                logger.warning(
                    f"Protected media provider {provider.__class__.__name__} "
                    f"failed in can_handle for {url}: {e}"
                )
        return None

    def is_valid_media_url(self, url: str) -> bool:
        """
        Validate if URL is a valid and safe media URL.

        Checks both URL format (HTTP/HTTPS) and SSRF safety
        (blocks private IPs, localhost, cloud metadata endpoints).

        Args:
            url: URL to validate

        Returns:
            True if valid and safe media URL, False otherwise
        """
        if not GENERIC_URL_PATTERN.match(url.strip()):
            return False
        safe, reason = is_safe_url(url)
        if not safe:
            logger.warning("SSRF blocked: %s for URL: %s", reason, url[:200])
            return False
        return True

    def is_youtube_url(self, url: str) -> bool:
        """
        Check if URL is a YouTube URL (for backward compatibility and special handling).

        Args:
            url: URL to check

        Returns:
            True if YouTube URL, False otherwise
        """
        return bool(YOUTUBE_URL_PATTERN.match(url.strip()))

    def is_playlist_url(self, url: str) -> bool:
        """
        Check if URL is a YouTube playlist URL.

        Args:
            url: URL to validate

        Returns:
            True if URL is a playlist, False if it's a single video
        """
        return bool(YOUTUBE_PLAYLIST_PATTERN.match(url.strip()))

    def extract_video_info(
        self,
        url: str,
        media_username: str | None = None,
        media_password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Extract video metadata without downloading.

        For generic public platforms this uses yt-dlp. For URLs handled by
        a ProtectedMediaProvider, it delegates to that provider's custom API-based logic.

        Args:
            url: Media URL
            media_username: Optional username for protected media sources
            media_password: Optional password for protected media sources
            user_id: Optional user ID for per-user credential resolution

        Returns:
            Dictionary with video information

        Raises:
            InvalidInputError: If unable to extract video information
        """
        # Try protected media providers first (authenticated corporate sites, etc.)
        provider = self._get_protected_provider(url, user_id=user_id)
        if provider is not None:
            return provider.extract_info(
                url, username=media_username, password=media_password, user_id=user_id
            )

        ydl_opts = {
            **_YT_DLP_BASE_OPTS,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "extractor_args": _YOUTUBE_EXTRACTOR_ARGS,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info  # type: ignore[no-any-return]
        except yt_dlp.DownloadError as e:
            # create_user_friendly_error is NOT a sanitizer despite the name — it
            # only strips four literal yt-dlp prefixes on the non-auth-error
            # branch, so the raw yt-dlp message (which can carry cookie/config
            # paths or the URL itself) used to reach the response verbatim
            # (#859). Log the real error with a traceback; return a fixed literal.
            logger.exception(f"Error extracting video info from {url}")
            raise InvalidInputError("Could not read information for that media URL.") from e
        except Exception as e:
            logger.exception(f"Error extracting video info from {url}")
            raise InvalidInputError("Could not read information for that media URL.") from e

    def download_video(
        self,
        url: str,
        output_path: str,
        progress_callback: Callable[[int, str], None] | None = None,
        media_username: str | None = None,
        media_password: str | None = None,
        video_quality: str = "best",
        audio_only: bool = False,
        audio_quality: str = "best",
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Download video from media URL.

        For public platforms uses yt-dlp; for URLs recognized by a
        ProtectedMediaProvider it delegates to that provider.

        Args:
            url: Media URL
            output_path: Directory to save downloaded file
            progress_callback: Optional callback for progress updates
            user_id: Optional user ID for per-user credential resolution

        Returns:
            Dictionary with file path, filename, and video info

        Raises:
            InvalidInputError: If the URL is refused or the download fails
            OperationFailedError: On an unexpected download failure
        """
        # Re-validate at FETCH time, not just at submission (issue #284 A0.2).
        # `is_valid_media_url` runs in the request handler, but the download executes
        # later on a Celery worker — a submitter controlling the domain can repoint DNS
        # in between and have the worker fetch an internal address. Re-checking here
        # closes that queue-delay window, which in practice is the whole attack.
        #
        # It does NOT fully close TOCTOU: yt-dlp resolves DNS itself at connect time and
        # follows redirects internally, so a sub-second rebind or a redirect to a private
        # host still gets through. Closing that needs egress restriction on the download
        # worker (deny RFC1918 + link-local at the network layer) — infrastructure, not
        # something this process can enforce.
        if not self.is_valid_media_url(url):
            raise InvalidInputError("Invalid URL. Please enter a valid HTTP or HTTPS URL.")

        # First, try pluggable protected-media providers
        provider = self._get_protected_provider(url, user_id=user_id)
        if provider is not None:
            return provider.download(
                url,
                output_path,
                progress_callback=progress_callback,
                username=media_username,
                password=media_password,
                user_id=user_id,
            )

        # Create progress hook function
        def progress_hook(d):
            if progress_callback and d.get("status") == "downloading":
                # Calculate progress percentage from downloaded_bytes and total_bytes
                total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded_bytes = d.get("downloaded_bytes", 0)

                if total_bytes and total_bytes > 0:
                    progress_percent = min(
                        int((downloaded_bytes / total_bytes) * 40) + 20, 60
                    )  # Map to 20-60% range
                    progress_callback(progress_percent, "Downloading video...")

        # Configure yt-dlp options for highest quality with web-compatible output
        ydl_opts = {
            **_YT_DLP_BASE_OPTS,
            # Initial format string — refined after extract_info reveals real availability
            "format": _build_yt_dlp_format_string(video_quality, audio_only, audio_quality),
            "outtmpl": os.path.join(output_path, "%(title)s.%(ext)s"),
            "restrictfilenames": True,  # Safe filenames (no special chars)
            "no_warnings": True,
            "ignoreerrors": False,
            "no_playlist": True,  # Single-video downloads only
            "max_filesize": 15 * 1024 * 1024 * 1024,  # 15 GB — matches upload limit
            # Always merge/remux to MP4 for broadest browser compatibility
            "merge_output_format": "mp4",
            # Cache and temp dirs
            "cachedir": str(settings.TEMP_DIR / "yt-dlp-cache"),
            "paths": {"temp": output_path},
            # 2026 YouTube client rotation — avoids SABR blocking.
            # 'android' and 'android_sdkless' are deprecated; ios_downgraded
            # and android_vr bypass server-side restrictions on many networks.
            "extractor_args": _YOUTUBE_EXTRACTOR_ARGS,
            # Browser impersonation headers (Chrome 131 — current stable Jan 2026)
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Sec-Fetch-Mode": "navigate",
            },
            # FFmpegVideoRemuxer: fast, lossless container remux to MP4.
            # Our format string already selects H.264+AAC streams so no
            # re-encoding is needed; FFmpegMetadata embeds title/uploader info.
            "postprocessors": [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "mp4",
                },
                {
                    "key": "FFmpegMetadata",
                    "add_metadata": True,
                },
            ],
        }

        # Audio-only mode adjustments
        if audio_only:
            ydl_opts["merge_output_format"] = "m4a"
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                }
            ]

        # Add progress hook if callback is provided
        if progress_callback:
            ydl_opts["progress_hooks"] = [progress_hook]

        # Cookie authentication for sign-in required videos
        if settings.YOUTUBE_COOKIE_FILE and os.path.exists(settings.YOUTUBE_COOKIE_FILE):
            # Explicit cookie file (for headless servers)
            ydl_opts["cookiefile"] = settings.YOUTUBE_COOKIE_FILE
            logger.info(f"Using cookie file: {settings.YOUTUBE_COOKIE_FILE}")
        elif settings.YOUTUBE_COOKIE_BROWSER:
            # Browser cookie extraction (for servers with browser installed)
            ydl_opts["cookiesfrombrowser"] = (settings.YOUTUBE_COOKIE_BROWSER, None, None, None)
            logger.info(f"Using cookies from browser: {settings.YOUTUBE_COOKIE_BROWSER}")

        try:
            # First context: extract info only (before download)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            # Refine format selection based on formats actually available for this video.
            # This ensures we cascade from the user's preferred quality down to the best
            # real match rather than relying solely on the yt-dlp selector string.
            format_spec, quality_description = _select_quality_from_available_formats(
                info.get("formats", []),
                video_quality,
                audio_only,
                audio_quality,
            )
            logger.info("Quality selection for %s: %s", url, quality_description)
            ydl_opts["format"] = format_spec

            # Check duration - for very long videos (>4hr), switch to audio-only
            duration = info.get("duration")
            if duration and duration > 14400:  # 4 hours limit
                logger.warning(
                    f"Video duration {duration:.0f}s exceeds 4hr limit, "
                    f"downloading audio-only for {url}"
                )
                ydl_opts["format"] = "bestaudio[ext=m4a]/bestaudio"
                ydl_opts["merge_output_format"] = "m4a"
                ydl_opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "m4a",
                    }
                ]
                quality_description = "audio only (video exceeds 4-hour limit)"

            # Second context: download with refined format selection
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # Find the downloaded file
            title = info.get("title", "video")
            ext = info.get("ext", "mp4")
            clean_title = re.sub(r"[^\w\-_\.]", "_", title)[:100]
            downloaded_file = _find_downloaded_file(output_path, clean_title, ext)

            # Detect actual file extension and content type
            from pathlib import Path

            actual_ext = Path(downloaded_file).suffix.lstrip(".")
            content_type_by_ext = {  # no-dot ext; unknown -> application/<ext>, unlike other tables
                "mp4": "video/mp4",
                "webm": "video/webm",
                "mkv": "video/x-matroska",
                "m4a": "audio/mp4",
                "mp3": "audio/mpeg",
                "ogg": "audio/ogg",
                "wav": "audio/wav",
                "flac": "audio/flac",
            }
            actual_content_type = content_type_by_ext.get(actual_ext, f"application/{actual_ext}")

            return {
                "file_path": downloaded_file,
                "filename": os.path.basename(downloaded_file),
                "content_type": actual_content_type,
                "info": info,
                "quality_description": quality_description,
            }

        except yt_dlp.DownloadError as e:
            # Same "create_user_friendly_error is not a sanitizer" reasoning as
            # extract_video_info above — never echo the raw yt-dlp message (#859).
            logger.exception(f"yt-dlp download error for {url}")
            raise InvalidInputError("The media could not be downloaded from that URL.") from e
        except Exception as e:
            logger.exception(f"Unexpected error downloading {url}")
            raise OperationFailedError("The media download failed.") from e

    def process_media_url_sync(
        self,
        url: str,
        db: Session,
        user: User,
        media_file: MediaFile,
        progress_callback: Callable[[int, str], None] | None = None,
        media_username: str | None = None,
        media_password: str | None = None,
        video_quality: str = "best",
        audio_only: bool = False,
        audio_quality: str = "best",
        benchmark_task_id: str | None = None,
    ) -> MediaFile:
        """
        Process a media URL by downloading the video and updating the MediaFile record (synchronous).

        Args:
            url: Media URL to process
            db: Database session
            user: User requesting the processing
            media_file: Pre-created MediaFile to update
            progress_callback: Optional callback for progress updates
            benchmark_task_id: Optional application-level task_id minted upstream
                (e.g., in ``process_youtube_url_task``) so per-stage wall-clock
                markers join the same ``benchmark:{task_id}`` Redis hash as the
                downstream transcription pipeline. No-op when falsy or when
                ``ENABLE_BENCHMARK_TIMING`` is off.

        Returns:
            Updated MediaFile object

        Raises:
            InvalidInputError: If the URL is refused
        """
        from app.utils import benchmark_timing

        if not self.is_valid_media_url(url):
            raise InvalidInputError("Invalid media URL")

        # Phase 1 — read the two identifiers the download phase needs, then END
        # the caller's transaction. A yt-dlp download plus a multi-gigabyte MinIO
        # upload used to run with it open, holding ACCESS SHARE on media_file for
        # the whole ingest (``app/tasks/CLAUDE.md``). Committing here is safe:
        # this method already commits below, so no caller relies on it leaving
        # work pending.
        resolve_user_id = int(user.id) if user else None
        file_id = int(media_file.id)
        db.commit()

        # Phase 2 — download + upload. NO transaction is held.
        staged = self._download_and_stage_media(
            url=url,
            user_id=resolve_user_id,
            file_id=file_id,
            progress_callback=progress_callback,
            media_username=media_username,
            media_password=media_password,
            video_quality=video_quality,
            audio_only=audio_only,
            audio_quality=audio_quality,
            benchmark_task_id=benchmark_task_id,
        )

        if progress_callback:
            progress_callback(95, "Finalizing and updating database...")

        # Phase 3 — write (DB only).
        _update_media_file_with_download_data(
            media_file=media_file,
            media_info=staged["media_info"],
            media_metadata=staged["media_metadata"],
            technical_metadata=staged["technical_metadata"],
            storage_path=staged["storage_path"],
            file_size=staged["file_size"],
            thumbnail_path=staged["thumbnail_path"],
            original_filename=staged["original_filename"],
            source_url=url,
            imohash_value=staged["imohash_value"],
        )
        with benchmark_timing.stage(benchmark_task_id, "db_commit"):
            db.commit()
            db.refresh(media_file)

        # Surface per-file context (size + MIME) so the analysis layer can
        # slice timing by file characteristics later.
        benchmark_timing.set_context(
            benchmark_task_id,
            {
                "file_size_bytes": int(staged["file_size"]) if staged["file_size"] else 0,
                "content_type": staged["technical_metadata"].get("content_type") or "",
            },
        )

        logger.info(f"Updated MediaFile record {media_file.id} for media video")

        return media_file

    def _download_and_stage_media(
        self,
        *,
        url: str,
        user_id: int | None,
        file_id: int,
        progress_callback: Callable[[int, str], None] | None = None,
        media_username: str | None = None,
        media_password: str | None = None,
        video_quality: str = "best",
        audio_only: bool = False,
        audio_quality: str = "best",
        benchmark_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Download a media URL and stage it in object storage.

        **Takes no DB session and opens none.** This is the slow half of
        :meth:`process_media_url_sync` — yt-dlp extraction, the download itself,
        ffprobe metadata, the imohash sample, the MinIO upload and the thumbnail
        — and it must run with the caller's transaction closed.

        Args:
            url: Media URL to download.
            user_id: Owner id; the storage key is namespaced by it.
            file_id: Internal media file id, used for thumbnail naming.
            progress_callback: Optional ``(percent, message)`` callback.
            media_username: Optional site credential.
            media_password: Optional site credential.
            video_quality: yt-dlp video quality selector.
            audio_only: Download audio only.
            audio_quality: yt-dlp audio quality selector.
            benchmark_task_id: Optional task id for per-stage timing markers.

        Returns:
            Plain data for the caller's write phase: ``media_info``,
            ``media_metadata``, ``technical_metadata``, ``storage_path``,
            ``file_size``, ``thumbnail_path``, ``original_filename`` and
            ``imohash_value``.

        Raises:
            InvalidInputError: If the video id cannot be extracted.
        """
        from app.utils import benchmark_timing

        resolve_user_id = user_id

        # Extract video info first to get video ID
        logger.debug(f"Extracting video information for URL: {url}")
        video_info = self.extract_video_info(
            url,
            media_username=media_username,
            media_password=media_password,
            user_id=resolve_user_id,
        )
        video_id = video_info.get("id")

        if not video_id:
            raise InvalidInputError("Could not extract video ID from URL")

        # Create temporary directory for download in configured TEMP_DIR
        # This ensures the non-root container user has write permissions
        temp_dir = tempfile.mkdtemp(prefix="media_download_", dir=str(settings.TEMP_DIR))

        try:
            if progress_callback:
                progress_callback(15, "Preparing for download...")

            # Download the video using the already extracted info (this will use 20-60% progress range)
            logger.info(f"Starting media download for URL: {url}")
            with benchmark_timing.stage(benchmark_task_id, "url_download"):
                download_result = self.download_video(
                    url,
                    temp_dir,
                    progress_callback=progress_callback,
                    media_username=media_username,
                    media_password=media_password,
                    video_quality=video_quality,
                    audio_only=audio_only,
                    audio_quality=audio_quality,
                    user_id=resolve_user_id,
                )

            if progress_callback:
                progress_callback(65, "Video downloaded, processing metadata...")

            downloaded_file = download_result["file_path"]
            original_filename = download_result["filename"]
            media_info = video_info  # Use the info we already extracted

            # Get file stats
            file_stats = os.stat(downloaded_file)
            file_size = file_stats.st_size

            # Extract technical metadata from downloaded file first
            technical_metadata = self._extract_technical_metadata(downloaded_file)

            if progress_callback:
                progress_callback(75, "Uploading to storage...")

            # Generate unique storage path
            file_uuid = str(uuid.uuid4())
            file_extension = Path(downloaded_file).suffix
            storage_path = f"media/{resolve_user_id}/{file_uuid}{file_extension}"

            # Compute imohash against the local downloaded file BEFORE the
            # upload clears it from disk. imohash samples three small byte
            # ranges (≤384 KiB total) — constant-time regardless of file
            # size, and cheap enough to run on every URL ingest. Used for
            # server-side duplicate detection + artifact cache keys
            # (Phase 2 PR #9 — URL ingress parity with direct uploads).
            imohash_value: str | None = None
            try:
                from app.services.imohash_service import compute_from_stream

                with (
                    benchmark_timing.stage(benchmark_task_id, "imohash"),
                    open(downloaded_file, "rb") as imo_fp,
                ):
                    imohash_value = compute_from_stream(imo_fp, file_size)
            except Exception as imo_err:
                logger.debug(f"imohash compute failed for {url} (non-fatal): {imo_err}")

            # Upload to MinIO. Use the tuned uploader so large yt-dlp files
            # use 64 MiB multipart parts (~12× fewer parts than the minio-py
            # 5 MB default) — Phase 2 PR #6.
            logger.info(f"Uploading downloaded video to MinIO: {storage_path}")
            with (
                benchmark_timing.stage(benchmark_task_id, "minio_put"),
                open(downloaded_file, "rb") as f,
            ):
                file_content = io.BytesIO(f.read())
                upload_file_tuned(
                    file_content=file_content,
                    file_size=file_size,
                    object_name=storage_path,
                    content_type=technical_metadata.get("content_type", "video/mp4"),
                )

            if progress_callback:
                progress_callback(85, "Processing thumbnails...")

            # Thumbnail derivation. If the source already provides a thumbnail
            # URL (YouTube, Vimeo, etc.) we fetch it inline — a single ~60 KB
            # HTTP GET is cheap and keeps the row usable immediately. Only the
            # expensive FFmpeg-frame-extraction fallback is deferred to an
            # async task (Phase 2 PR #9, item 3).
            thumbnail_path: str | None = None
            with benchmark_timing.stage(benchmark_task_id, "thumbnail"):
                thumbnail_path = _get_thumbnail_with_fallback(
                    media_info,
                    int(resolve_user_id) if resolve_user_id is not None else 0,
                    file_id,
                    downloaded_file,
                    defer_fallback_generation=True,
                    storage_path=storage_path,
                )

            # Prepare media metadata for the caller's write phase.
            media_metadata = self._prepare_media_metadata(url, media_info)

            return {
                "media_info": media_info,
                "media_metadata": media_metadata,
                "technical_metadata": technical_metadata,
                "storage_path": storage_path,
                "file_size": file_size,
                "thumbnail_path": thumbnail_path,
                "original_filename": original_filename,
                "imohash_value": imohash_value,
            }

        finally:
            # Clean up temporary files
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.debug(f"Cleaned up temporary directory: {temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory {temp_dir}: {e}")

    # Backward compatibility aliases
    def is_valid_youtube_url(self, url: str) -> bool:
        """Alias for is_valid_media_url for backward compatibility."""
        return self.is_valid_media_url(url)

    def process_youtube_url_sync(
        self,
        url: str,
        db: Session,
        user: User,
        media_file: MediaFile,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> MediaFile:
        """Alias for process_media_url_sync for backward compatibility."""
        return self.process_media_url_sync(url, db, user, media_file, progress_callback)

    def _prepare_youtube_metadata(self, url: str, youtube_info: dict[str, Any]) -> dict[str, Any]:
        """Alias for _prepare_media_metadata for backward compatibility."""
        return self._prepare_media_metadata(url, youtube_info)


# Backward compatibility alias
YouTubeService = MediaDownloadService
