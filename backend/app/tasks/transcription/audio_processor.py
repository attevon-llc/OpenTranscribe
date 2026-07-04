import logging
import os
from collections.abc import Callable
from pathlib import Path

import ffmpeg

logger = logging.getLogger(__name__)

# Type alias for progress callback: (progress: float 0-1, message: str) -> None
ProgressCallback = Callable[[float, str], None] | None


def _ffmpeg_threads() -> int:
    """Resolve the ffmpeg -threads value (opt-in CPU-contention control).

    The CPU worker runs up to CPU_WORKER_CONCURRENCY preprocess tasks at once, each
    spawning an ffmpeg process. ffmpeg's default (-threads 0 = use all cores) can
    oversubscribe cores on low-core / busy machines where OpenTranscribe co-tenants
    with other services. Setting FFMPEG_THREADS lets such deployments cap it.

    Default is unbounded (legacy behavior) — benchmarking on a 48-core server showed
    capping yields no throughput or peak-CPU benefit there (preprocess is ~1-2% of
    per-file work; the GPU is the bottleneck). The win is expected only on small
    machines, so it is opt-in rather than a default behavior change.

    FFMPEG_THREADS env:
      unset / "0"     -> 0 = ffmpeg auto / unbounded (default, no change)
      "auto"          -> cores // CPU_WORKER_CONCURRENCY (>=1): no oversubscription
      <positive int>  -> explicit per-process cap (e.g. 2-4 on a laptop)
    """
    raw = os.environ.get("FFMPEG_THREADS", "0").strip().lower()
    if raw in ("", "0"):
        return 0
    if raw.isdigit():
        return int(raw)
    if raw == "auto":
        cores = os.cpu_count() or 4
        try:
            concurrency = max(1, int(os.environ.get("CPU_WORKER_CONCURRENCY", "8")))
        except ValueError:
            concurrency = 8
        return max(1, cores // concurrency)
    return 0  # unrecognized -> safe default (unbounded)


def get_audio_file_extension(content_type: str, filename: str) -> str:
    """
    Determine the appropriate file extension based on content type and filename.

    Args:
        content_type: MIME type of the file
        filename: Original filename

    Returns:
        File extension string
    """
    file_ext = os.path.splitext(filename)[1]

    if not file_ext and content_type:
        mime_to_ext = {
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "audio/wave": ".wav",
            "audio/x-wav": ".wav",
            "audio/webm": ".webm",
            "audio/ogg": ".ogg",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/ogg": ".ogg",
        }
        file_ext = mime_to_ext.get(content_type, ".mp4")

    return file_ext


def extract_audio_from_video(  # noqa: C901
    video_path: str,
    output_path: str,
    progress_callback: ProgressCallback = None,
) -> None:
    """
    Extract audio from a video file using ffmpeg.

    Args:
        video_path: Path to the input video file
        output_path: Path for the output audio file
        progress_callback: Optional callback(progress: float 0-1, message: str)
    """
    logger.info(f"Extracting audio from video file {video_path}")

    if progress_callback:
        progress_callback(0.0, "Starting audio extraction from video")

    try:
        if progress_callback:
            progress_callback(0.2, "Processing video file...")

        ffmpeg.input(video_path).output(
            output_path,
            acodec="pcm_s16le",
            ar="16000",
            ac=1,
            vn=None,
            threads=_ffmpeg_threads(),
        ).run(quiet=True, overwrite_output=True)

        if progress_callback:
            progress_callback(0.8, "Verifying extracted audio...")

        # Verify output file was created and has content
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise ValueError("Video contains no audio track or audio extraction failed")

        if progress_callback:
            progress_callback(1.0, "Audio extraction complete")

    except ffmpeg.Error as e:
        logger.error(f"FFmpeg video extraction failed for {video_path}: {e}")
        # Get stderr output for better error messages
        stderr_output = e.stderr.decode("utf-8") if e.stderr else ""

        # Check for common video-specific error patterns
        if (
            "Invalid data found when processing input" in stderr_output
            or "does not contain any stream" in stderr_output
        ):
            raise ValueError(
                "This file appears to be corrupted or is not a valid video file. Please check the file and try uploading again."
            ) from e
        elif "No audio streams found" in stderr_output or "does not contain audio" in stderr_output:
            raise ValueError(
                "This video file does not contain any audio tracks. Please upload a video with audio or an audio file directly."
            ) from e
        elif "Unknown format" in stderr_output or "not supported" in stderr_output:
            raise ValueError(
                "This video format is not supported. Please convert to a common format like MP4, AVI, or MOV and try again."
            ) from e
        else:
            # Generic fallback for other video processing errors
            raise ValueError(
                "Unable to extract audio from this video file. The file may be corrupted, password-protected, or in an unsupported format."
            ) from e
    except Exception as e:
        logger.error(f"Unexpected error during video audio extraction: {e}")
        raise ValueError(f"Video audio extraction failed: {str(e)}") from e


def convert_audio_format(  # noqa: C901
    input_path: str,
    output_path: str,
    progress_callback: ProgressCallback = None,
) -> None:
    """
    Convert audio file to WAV format using ffmpeg.

    Args:
        input_path: Path to the input audio file
        output_path: Path for the output WAV file
        progress_callback: Optional callback(progress: float 0-1, message: str)
    """
    logger.info("Converting audio to WAV format")

    if progress_callback:
        progress_callback(0.0, "Starting audio format conversion")

    try:
        if progress_callback:
            progress_callback(0.3, "Converting audio format...")

        ffmpeg.input(input_path).output(
            output_path, acodec="pcm_s16le", ar="16000", ac=1, threads=_ffmpeg_threads()
        ).run(quiet=True, overwrite_output=True)

        if progress_callback:
            progress_callback(0.8, "Verifying converted audio...")

        # Verify output file was created and has content
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise ValueError(
                "Audio conversion produced no output - file may be corrupted or not contain valid audio"
            )

        if progress_callback:
            progress_callback(1.0, "Audio conversion complete")

    except ffmpeg.Error as e:
        logger.error(f"FFmpeg conversion failed for {input_path}: {e}")
        # Get stderr output for better error messages
        stderr_output = e.stderr.decode("utf-8") if e.stderr else ""

        # Check for common error patterns and provide user-friendly messages
        if (
            "Invalid data found when processing input" in stderr_output
            or "does not contain any stream" in stderr_output
        ):
            raise ValueError(
                "This file appears to be corrupted or is not a valid audio/video file. Please check the file and try uploading again."
            ) from e
        elif "Unknown format" in stderr_output or "not supported" in stderr_output:
            raise ValueError(
                "This file format is not supported. Please convert to a common format like MP3, WAV, or MP4 and try again."
            ) from e
        elif any(
            keyword in stderr_output.lower()
            for keyword in ["permission denied", "no such file", "directory"]
        ):
            raise ValueError(
                "Unable to access the uploaded file. Please try uploading again."
            ) from e
        else:
            # Generic fallback for other ffmpeg errors
            raise ValueError(
                "Unable to process this file as audio/video content. The file may be corrupted, password-protected, or in an unsupported format."
            ) from e
    except Exception as e:
        logger.error(f"Unexpected error during audio conversion: {e}")
        raise ValueError(f"Audio processing failed: {str(e)}") from e


def prepare_audio_for_transcription(
    temp_file_path: str,
    content_type: str,
    temp_dir: str,
    progress_callback: ProgressCallback = None,
) -> str:
    """
    Prepare audio file for transcription by extracting from video or converting format.

    Args:
        temp_file_path: Path to the temporary input file
        content_type: MIME type of the input file
        temp_dir: Temporary directory for processing
        progress_callback: Optional callback(progress: float 0-1, message: str)

    Returns:
        Path to the prepared audio file
    """
    temp_audio_path = os.path.join(temp_dir, "audio.wav")

    if content_type.startswith("video/"):
        extract_audio_from_video(temp_file_path, temp_audio_path, progress_callback)
        return temp_audio_path
    else:
        # For audio files, convert to WAV format if needed
        file_ext = Path(temp_file_path).suffix.lower()
        if file_ext != ".wav":
            convert_audio_format(temp_file_path, temp_audio_path, progress_callback)
            return temp_audio_path
        else:
            # No conversion needed - signal completion if callback provided
            if progress_callback:
                progress_callback(1.0, "Audio file ready for transcription")
            return temp_file_path
