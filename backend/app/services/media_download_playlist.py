"""Playlist ingestion: placeholder records, dedup lookup, and the playlist mixin."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

import yt_dlp
from sqlalchemy.orm import Session

from app.core.exceptions import DomainError
from app.core.exceptions import InvalidInputError
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.media_download_formats import _YOUTUBE_EXTRACTOR_ARGS
from app.services.media_download_formats import _YT_DLP_BASE_OPTS

logger = logging.getLogger(__name__)


def _check_existing_youtube_video(
    db: Session, user_id: int, video_id: str, organization_id: int | None = None
) -> MediaFile | None:
    """
    Check if a YouTube video already exists in the user's library in this tenant.

    Args:
        db: Database session
        user_id: User ID
        video_id: YouTube video ID
        organization_id: Tenant the playlist is imported into (None = personal).
            The same user's copy in another tenant is not a duplicate.

    Returns:
        Existing MediaFile if found, None otherwise
    """
    from sqlalchemy import text

    from app.utils.db_helpers import org_stamp_is

    result = (
        db.query(MediaFile)
        .filter(
            MediaFile.user_id == user_id,
            org_stamp_is(MediaFile.organization_id, organization_id),
            text("metadata_raw->>'youtube_id' = :youtube_id"),
        )
        .params(youtube_id=video_id)
        .first()
    )
    return result  # type: ignore[no-any-return]


def _process_playlist_videos(
    db: Session,
    user_id: int,
    videos: list[dict[str, Any]],
    playlist_info: dict[str, Any],
    playlist_url: str,
    video_count: int,
    progress_callback: Callable[[int, str, dict], None] | None = None,
    organization_id: int | None = None,
) -> tuple[list[MediaFile], list[dict[str, Any]]]:
    """
    Process playlist videos and create placeholders.

    Args:
        db: Database session
        user_id: User ID
        videos: List of video entries from playlist
        playlist_info: Playlist metadata
        playlist_url: Original playlist URL
        video_count: Total video count for progress
        progress_callback: Optional progress callback
        organization_id: Originating request's tenant (threaded through the
            task kwargs — issue #262c); stamped on every placeholder

    Returns:
        Tuple of (created_media_files, skipped_videos)
    """
    created_media_files = []
    skipped_videos = []

    for idx, video_entry in enumerate(videos):
        video_id = video_entry.get("video_id")
        video_title = video_entry.get("title", "Unknown")

        # Report progress
        if progress_callback:
            progress = int(20 + (idx / video_count) * 70)
            progress_callback(
                progress,
                f"Processing video {idx + 1} of {video_count}: {video_title[:50]}...",
                {"current_video": idx + 1, "total_videos": video_count, "video_title": video_title},
            )

        # Check for existing video - ensure video_id is a string
        if video_id:
            existing_video = _check_existing_youtube_video(
                db, user_id, str(video_id), organization_id
            )
        else:
            existing_video = None

        if existing_video:
            logger.info(f"Video already exists in library: {video_title} (YouTube ID: {video_id})")
            skipped_videos.append(
                {
                    "video_id": video_id,
                    "title": video_title,
                    "reason": "duplicate",
                    "existing_file_id": existing_video.id,
                }
            )
            continue

        # Create placeholder
        try:
            video_entry["playlist_index"] = video_entry.get("playlist_index", idx + 1)
            media_file = _create_playlist_video_placeholder(
                db,
                user_id,
                video_entry,
                playlist_info,
                playlist_url,
                organization_id=organization_id,
            )
            created_media_files.append(media_file)
            logger.info(
                f"Created placeholder MediaFile {media_file.id} for playlist video: {video_title}"
            )
        except Exception as e:
            logger.error(f"Error creating placeholder for video {video_title}: {e}")
            skipped_videos.append(
                {
                    "video_id": video_id,
                    "title": video_title,
                    "reason": f"error: {str(e)}",
                }
            )

    return created_media_files, skipped_videos


def _create_playlist_video_placeholder(
    db: Session,
    user_id: int,
    video_entry: dict[str, Any],
    playlist_info: dict[str, Any],
    playlist_url: str,
    organization_id: int | None = None,
) -> MediaFile:
    """
    Create a placeholder MediaFile for a playlist video.

    Args:
        db: Database session
        user_id: User ID
        video_entry: Video entry from playlist
        playlist_info: Playlist metadata
        playlist_url: Original playlist URL
        organization_id: Tenant of the ORIGINATING request (issue #262c) —
            never resolved from the owner's memberships here. None = personal.

    Returns:
        Created MediaFile
    """
    video_id = video_entry.get("video_id")
    video_url = video_entry.get("url")
    video_title = video_entry.get("title", "Unknown")
    playlist_index = video_entry.get("playlist_index", 1)

    placeholder_metadata = {
        "youtube_id": video_id,
        "youtube_url": video_url,
        "title": video_title,
        "processing": True,
        "from_playlist": True,
        "playlist_id": playlist_info.get("playlist_id"),
        "playlist_title": playlist_info.get("playlist_title"),
        "playlist_url": playlist_url,
        "playlist_index": playlist_index,
    }

    media_file = MediaFile(
        user_id=user_id,
        organization_id=organization_id,
        filename=video_title[:255],
        storage_path="",
        file_size=0,
        content_type="video/mp4",
        duration=video_entry.get("duration"),
        status=FileStatus.QUEUED,  # Playlist placeholder, waiting for download
        title=video_title,
        author=video_entry.get("uploader"),
        source_url=video_url,
        metadata_raw=placeholder_metadata,
        metadata_important=placeholder_metadata,
    )

    db.add(media_file)
    db.flush()

    return media_file


class PlaylistMixin:
    """Playlist enumeration and processing for MediaDownloadService."""

    if TYPE_CHECKING:
        # Provided by MediaDownloadService; declared so mypy sees the host contract.
        def is_playlist_url(self, url: str) -> bool: ...

    def extract_playlist_info(self, url: str) -> dict[str, Any]:
        """
        Extract playlist metadata and video list without downloading.

        Args:
            url: YouTube playlist URL

        Returns:
            Dictionary with playlist information including:
            - playlist_id: Playlist ID
            - playlist_title: Playlist title
            - playlist_uploader: Playlist creator
            - video_count: Number of videos
            - videos: List of video entries with URLs and basic info

        Raises:
            InvalidInputError: If unable to extract playlist information
        """
        ydl_opts = {
            **_YT_DLP_BASE_OPTS,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",  # Extract video info without downloading
            "skip_download": True,
            "extractor_args": _YOUTUBE_EXTRACTOR_ARGS,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                if not info:
                    raise ValueError("No playlist information found")

                # Extract video entries
                entries = info.get("entries", [])
                videos = []

                for idx, entry in enumerate(entries):
                    if entry:  # Some entries might be None (unavailable videos)
                        video_id = entry.get("id")
                        if video_id:
                            videos.append(
                                {
                                    "video_id": video_id,
                                    "url": f"https://www.youtube.com/watch?v={video_id}",
                                    "title": entry.get("title", "Unknown"),
                                    "duration": entry.get("duration"),
                                    "uploader": entry.get("uploader"),
                                    "playlist_index": idx + 1,
                                }
                            )

                return {
                    "playlist_id": info.get("id"),
                    "playlist_title": info.get("title", "Unknown Playlist"),
                    "playlist_uploader": info.get("uploader") or info.get("channel"),
                    "playlist_description": info.get("description"),
                    "video_count": len(videos),
                    "videos": videos,
                }

        except Exception as e:
            # Same "create_user_friendly_error is not a sanitizer" reasoning as
            # extract_video_info above — never echo the raw yt-dlp message (#859).
            logger.exception(f"Error extracting playlist info from {url}")
            raise InvalidInputError("Could not read that playlist.") from e

    def process_youtube_playlist_sync(
        self,
        url: str,
        db: Session,
        user: User,
        progress_callback: Callable[[int, str, dict], None] | None = None,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Process a YouTube playlist by extracting video list and creating placeholder MediaFile records.

        This method extracts the playlist information and creates MediaFile records for each video.
        Individual video downloads are handled by separate Celery tasks for parallel processing.

        Args:
            url: YouTube playlist URL
            db: Database session
            user: User requesting the processing
            progress_callback: Optional callback for progress updates (progress, message, data)

        Returns:
            Dictionary containing:
            - playlist_info: Playlist metadata
            - media_files: List of created MediaFile records
            - skipped_videos: List of videos that were skipped (duplicates or errors)

        Raises:
            InvalidInputError: If playlist extraction fails
        """
        if not self.is_playlist_url(url):
            raise InvalidInputError("URL is not a valid YouTube playlist")

        # Extract playlist information
        logger.info(f"Extracting playlist information from: {url}")
        if progress_callback:
            progress_callback(10, "Extracting playlist information...", {})

        try:
            playlist_info = self.extract_playlist_info(url)
        except DomainError:
            raise
        except Exception as e:
            logger.exception("Error extracting playlist info")
            raise InvalidInputError("Could not read that playlist.") from e

        video_count = playlist_info.get("video_count", 0)
        if video_count == 0:
            raise InvalidInputError("Playlist is empty or contains no accessible videos")

        logger.info(
            f"Found {video_count} videos in playlist: {playlist_info.get('playlist_title')}"
        )
        if progress_callback:
            progress_callback(
                20,
                f"Found {video_count} videos in playlist...",
                {"video_count": video_count, "playlist_title": playlist_info.get("playlist_title")},
            )

        # Create placeholder MediaFile records for each video
        videos = playlist_info.get("videos", [])
        created_media_files, skipped_videos = _process_playlist_videos(
            db,
            int(user.id),
            videos,
            playlist_info,
            url,
            video_count,
            progress_callback,
            organization_id=organization_id,
        )

        # Commit and refresh all placeholder records
        db.commit()
        for media_file in created_media_files:
            db.refresh(media_file)

        if progress_callback:
            progress_callback(
                100,
                f"Playlist processing complete: {len(created_media_files)} videos queued",
                {"created_count": len(created_media_files), "skipped_count": len(skipped_videos)},
            )

        logger.info(
            f"Playlist processing complete: {len(created_media_files)} videos created, "
            f"{len(skipped_videos)} skipped"
        )

        return {
            "playlist_info": playlist_info,
            "media_files": created_media_files,
            "skipped_videos": skipped_videos,
            "created_count": len(created_media_files),
            "skipped_count": len(skipped_videos),
            "total_videos": video_count,
        }
