"""
Utility module for generating thumbnails from video files.

Supports WebP format with aspect ratio preservation for optimal quality and performance.
"""

import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

import ffmpeg

from app.core.constants import THUMBNAIL_FORMAT
from app.core.constants import THUMBNAIL_MAX_DIMENSION
from app.core.constants import THUMBNAIL_QUALITY_JPEG
from app.core.constants import THUMBNAIL_QUALITY_WEBP
from app.services.minio_service import upload_file

logger = logging.getLogger(__name__)


def generate_thumbnail(
    video_path: str | Path,
    output_path: str | Path | None = None,
    timestamp: float = 1.0,
    max_dimension: int = THUMBNAIL_MAX_DIMENSION,
    output_format: Literal["webp", "jpeg"] = THUMBNAIL_FORMAT,  # type: ignore[assignment]
    quality: int | None = None,
) -> str | bytes | None:
    """
    Generate a thumbnail from a video file at the specified timestamp.

    Preserves aspect ratio by scaling the longest edge to max_dimension.
    Does not upscale videos smaller than max_dimension.

    Args:
        video_path: Path to the video file
        output_path: Path to save the thumbnail (optional)
        timestamp: Time in seconds to extract the thumbnail (default: 1s)
        max_dimension: Maximum dimension for longest edge (default: 1280)
        format: Output format - "webp" or "jpeg" (default: webp)
        quality: Output quality (default: 75 for webp, 70 for jpeg)

    Returns:
        If output_path is specified: The path to the saved thumbnail
        If output_path is None: The thumbnail as bytes
    """
    try:
        # Convert to string if Path object
        if isinstance(video_path, Path):
            video_path = str(video_path)

        # Set default quality based on format
        if quality is None:
            quality = THUMBNAIL_QUALITY_WEBP if output_format == "webp" else THUMBNAIL_QUALITY_JPEG

        # Determine file extension and ffmpeg format
        ext = "webp" if output_format == "webp" else "jpg"
        ffmpeg_format = "webp" if output_format == "webp" else "mjpeg"

        # Track if we created a temp file
        temp_file_created = False

        # Create a temporary file if no output path specified
        if output_path is None:
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                output_path = tmp.name
                temp_file_created = True

        # Build output options based on format
        output_opts = {"vframes": 1, "format": ffmpeg_format}
        if output_format == "webp":
            output_opts["q:v"] = quality
        else:
            output_opts["qscale:v"] = int((100 - quality) / 100 * 31)  # Convert to ffmpeg scale

        # Generate thumbnail using ffmpeg
        (
            ffmpeg.input(video_path, ss=timestamp)
            .filter("scale", f"min({max_dimension},iw)", -1)
            .output(str(output_path), **output_opts)
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )

        # Return the path or read as bytes if we created a temp file
        if temp_file_created:
            with open(output_path, "rb") as f:
                result = f.read()
            # Clean up the temporary file
            os.unlink(output_path)
            return result

        return str(output_path)

    except ffmpeg.Error as e:
        logger.error(
            f"Error generating thumbnail with ffmpeg: {e.stderr.decode() if e.stderr else str(e)}"
        )
        return None
    except Exception as e:
        logger.error(f"Error generating thumbnail: {str(e)}")
        return None


def generate_thumbnail_from_url(
    presigned_url: str,
    timestamp: float = 1.0,
    max_dimension: int = THUMBNAIL_MAX_DIMENSION,
    quality: int = THUMBNAIL_QUALITY_WEBP,
) -> bytes | None:
    """
    Generate a WebP thumbnail from a remote video using FFmpeg URL streaming.

    FFmpeg reads directly from the URL via presigned URL.
    Uses input seeking (-ss before -i) for fast seeking.
    Only reads first few seconds, not the entire video.
    Preserves original aspect ratio.

    Args:
        presigned_url: Presigned URL to the video file
        timestamp: Time in seconds to extract the thumbnail (default: 1s)
        max_dimension: Maximum dimension for longest edge (default: 1280)
        quality: WebP quality 0-100 (default: 75)

    Returns:
        Thumbnail as bytes, or None if generation failed
    """
    try:
        logger.info("Generating thumbnail from URL with ffmpeg")
        # Use ffmpeg-python library to stream from URL
        # -ss before -i = input seeking (fast)
        # scale filter preserves aspect ratio, no upscaling
        out, err = (
            ffmpeg.input(presigned_url, ss=timestamp)
            .filter("scale", f"min({max_dimension},iw)", -1)
            .output("pipe:", format="webp", vframes=1, **{"q:v": quality})
            .run(capture_stdout=True, capture_stderr=True)
        )

        if not out:
            logger.error(
                f"FFmpeg produced no output. stderr: {err.decode() if err else 'no stderr'}"
            )
            return None

        logger.info(f"FFmpeg generated {len(out)} bytes")
        # out is bytes from ffmpeg stdout
        return bytes(out)

    except ffmpeg.Error as e:
        # Extract actual error message from stderr
        stderr_msg = ""
        if e.stderr:
            try:
                stderr_msg = e.stderr.decode()
                # Get just the error line, not the full ffmpeg version info
                lines = stderr_msg.split("\n")
                for line in lines:
                    if "error" in line.lower() or "invalid" in line.lower():
                        stderr_msg = line
                        break
            except Exception:
                stderr_msg = str(e.stderr)

        logger.error(f"FFmpeg error: {stderr_msg or str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error generating thumbnail from URL: {str(e)}", exc_info=True)
        return None


def generate_and_upload_thumbnail(
    user_id: int,
    media_file_id: int,
    video_path: str | Path,
    timestamp: float = 1.0,
    output_format: Literal["webp", "jpeg"] = THUMBNAIL_FORMAT,  # type: ignore[assignment]
) -> str | None:
    """
    Generate a thumbnail from a video file and upload it to storage.

    ffmpeg extraction and the object-storage PUT are both blocking, so this is a
    plain function: an ``async def`` twin used to exist alongside it with a
    byte-identical body and no ``await``, which meant awaiting it never yielded
    and the whole ffmpeg run blocked the event loop (issue #320).

    Args:
        user_id: User ID for storage path
        media_file_id: Media file ID for storage path
        video_path: Path to the video file
        timestamp: Time in seconds to extract the thumbnail
        output_format: Output format - "webp" or "jpeg" (default: webp)

    Returns:
        The storage path of the uploaded thumbnail, or None if generation failed
    """
    try:
        # Generate thumbnail
        thumbnail_bytes = generate_thumbnail(
            video_path, timestamp=timestamp, output_format=output_format
        )

        if not thumbnail_bytes or isinstance(thumbnail_bytes, str):
            logger.error(f"Failed to generate thumbnail for file {media_file_id}")
            return None

        # Determine extension and content type based on format
        ext = "webp" if output_format == "webp" else "jpg"
        content_type = "image/webp" if output_format == "webp" else "image/jpeg"

        # Generate storage path for thumbnail
        filename = (
            Path(video_path).stem
            if isinstance(video_path, (str, Path))
            else f"file_{media_file_id}"
        )
        storage_path = f"user_{user_id}/file_{media_file_id}/thumbnail_{filename}.{ext}"

        # Upload thumbnail to storage
        if os.environ.get("SKIP_S3", "False").lower() != "true":
            upload_file(
                file_content=io.BytesIO(thumbnail_bytes),
                file_size=len(thumbnail_bytes),
                object_name=storage_path,
                content_type=content_type,
            )
        else:
            logger.info("Skipping S3 upload for thumbnail in test environment")

        return storage_path

    except Exception as e:
        logger.error(f"Error generating and uploading thumbnail: {str(e)}")
        return None
