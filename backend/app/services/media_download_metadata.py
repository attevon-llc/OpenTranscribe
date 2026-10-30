"""Technical/basic file metadata extraction for downloaded media."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MediaMetadataMixin:
    """File metadata helpers for MediaDownloadService."""

    def _extract_technical_metadata(self, file_path: str) -> dict[str, Any]:
        """
        Extract technical metadata from downloaded file.

        Args:
            file_path: Path to the downloaded file

        Returns:
            Dictionary with technical metadata
        """
        try:
            # Use the existing metadata extraction service
            from app.tasks.transcription.metadata_extractor import extract_media_metadata
            from app.tasks.transcription.metadata_extractor import get_important_metadata

            raw_metadata = extract_media_metadata(file_path)
            if raw_metadata:
                important_metadata = get_important_metadata(raw_metadata)

                # Convert to format expected by MediaFile model
                return {
                    "content_type": raw_metadata.get("File:MIMEType", "video/mp4"),
                    "format": important_metadata.get("FileType"),
                    "video_codec": important_metadata.get("VideoCodec"),
                    "width": important_metadata.get("VideoWidth"),
                    "height": important_metadata.get("VideoHeight"),
                    "frame_rate": important_metadata.get("VideoFrameRate"),
                    "audio_channels": important_metadata.get("AudioChannels"),
                    "audio_sample_rate": important_metadata.get("AudioSampleRate"),
                    "duration": important_metadata.get("Duration"),
                }
            else:
                logger.warning("No metadata extracted, using fallback")
                return self._extract_basic_metadata(file_path)
        except Exception as e:
            logger.warning(f"Failed to extract technical metadata: {e}")
            return self._extract_basic_metadata(file_path)

    def _safe_frame_rate_eval(self, frame_rate_str: str) -> float | None:
        """
        Safely evaluate frame rate string like '30/1' or '29.97'.

        Args:
            frame_rate_str: Frame rate string from ffprobe

        Returns:
            Frame rate as float or None if invalid
        """
        try:
            if "/" in frame_rate_str:
                numerator, denominator = frame_rate_str.split("/")
                return float(numerator) / float(denominator)
            else:
                return float(frame_rate_str)
        except (ValueError, ZeroDivisionError):
            logger.warning(f"Invalid frame rate format: {frame_rate_str}")
            return None

    def _extract_basic_metadata(self, file_path: str) -> dict[str, Any]:
        """
        Fallback method to extract basic metadata using ffprobe.

        Args:
            file_path: Path to the media file

        Returns:
            Dictionary with basic metadata
        """
        try:
            import ffmpeg  # type: ignore[import-untyped]

            probe = ffmpeg.probe(file_path)
            format_info = probe.get("format", {})
            video_stream = next(
                (stream for stream in probe["streams"] if stream["codec_type"] == "video"),
                None,
            )
            audio_stream = next(
                (stream for stream in probe["streams"] if stream["codec_type"] == "audio"),
                None,
            )

            metadata = {
                "content_type": "video/mp4",  # Default
                "format": format_info.get("format_name"),
                "duration": float(format_info.get("duration", 0)),
            }

            if video_stream:
                metadata.update(
                    {
                        "video_codec": video_stream.get("codec_name"),
                        "width": video_stream.get("width"),
                        "height": video_stream.get("height"),
                        "frame_rate": self._safe_frame_rate_eval(video_stream.get("r_frame_rate"))
                        if video_stream.get("r_frame_rate")
                        else None,
                    }
                )

            if audio_stream:
                metadata.update(
                    {
                        "audio_channels": audio_stream.get("channels"),
                        "audio_sample_rate": audio_stream.get("sample_rate"),
                    }
                )

            return metadata

        except Exception as e:
            logger.warning(f"Failed to extract basic metadata: {e}")
            return {"content_type": "video/mp4"}

    def _prepare_media_metadata(self, url: str, media_info: dict[str, Any]) -> dict[str, Any]:
        """
        Prepare media-specific metadata for storage.

        Args:
            url: Original media URL
            media_info: Information extracted from yt-dlp

        Returns:
            Dictionary with media metadata
        """
        # Detect the source platform dynamically
        source = media_info.get("extractor", "unknown").lower()

        # Build metadata with platform-agnostic keys plus platform-specific ones
        metadata = {
            "source": source,
            "original_url": url,
            "video_id": media_info.get("id"),
            "title": media_info.get("title"),
            "description": media_info.get("description"),
            "uploader": media_info.get("uploader"),
            "upload_date": media_info.get("upload_date"),
            "duration": media_info.get("duration"),
            "view_count": media_info.get("view_count"),
            "like_count": media_info.get("like_count"),
            "thumbnail": media_info.get("thumbnail"),
            "tags": media_info.get("tags", []),
            "categories": media_info.get("categories", []),
        }

        # Add YouTube-specific fields for backward compatibility
        if "youtube" in source:
            metadata.update(
                {
                    "youtube_id": media_info.get("id"),
                    "youtube_title": media_info.get("title"),
                    "youtube_description": media_info.get("description"),
                    "youtube_uploader": media_info.get("uploader"),
                    "youtube_upload_date": media_info.get("upload_date"),
                    "youtube_duration": media_info.get("duration"),
                    "youtube_view_count": media_info.get("view_count"),
                    "youtube_like_count": media_info.get("like_count"),
                    "youtube_thumbnail": media_info.get("thumbnail"),
                    "youtube_tags": media_info.get("tags", []),
                    "youtube_categories": media_info.get("categories", []),
                }
            )

        return metadata
