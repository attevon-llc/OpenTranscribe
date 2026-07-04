"""
Waveform generation service for media files.

This module provides functionality to generate waveform visualization data
during the media processing pipeline.
"""

import json
import logging
import subprocess
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Standard waveform resolutions for responsive design and device optimization
WAVEFORM_RESOLUTIONS = {
    "small": 500,  # Mobile phones, thumbnails, low-bandwidth
    "medium": 1000,  # Desktop standard, tablets
    "large": 2000,  # High-DPI displays, detailed editing, large screens
}


class WaveformGenerator:
    """Generate waveform visualization data for audio/video files."""

    # Sample rate used for waveform extraction
    WAVEFORM_SAMPLE_RATE = 22050

    def __init__(self):
        """Initialize the waveform generator."""
        self._check_dependencies()

    def _check_dependencies(self):
        """Check that required dependencies (FFmpeg) are available."""
        try:
            # Using hardcoded ffmpeg/ffprobe commands, not user input
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=10)  # noqa: S603 S607 # nosec B603 B607
            subprocess.run(["ffprobe", "-version"], capture_output=True, check=True, timeout=10)  # noqa: S603 S607 # nosec B603 B607
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ) as e:
            logger.error(f"FFmpeg not available: {e}")
            raise RuntimeError(
                "FFmpeg is required for waveform generation but not available"
            ) from e

    def generate_waveform_data(self, file_path: str) -> dict[str, Any] | None:
        """
        Generate waveform data for multiple resolutions optimized for different devices and screen sizes.

        Args:
            file_path: Path to the audio/video file

        Returns:
            Dictionary containing waveform data for all resolutions
        """
        try:
            # Generate waveforms for all standard resolutions
            waveform_cache = {}

            for resolution_name, samples in WAVEFORM_RESOLUTIONS.items():
                waveform_data = self._extract_single_waveform(file_path, samples)
                if waveform_data:
                    cache_key = f"waveform_{samples}"
                    waveform_cache[cache_key] = waveform_data
                    logger.debug(f"Generated {resolution_name} waveform: {samples} samples")

            # If we got at least one waveform, return the cache
            if waveform_cache:
                logger.info(f"Generated waveform data for {len(waveform_cache)} resolutions")
                return waveform_cache
            else:
                logger.warning(f"Failed to generate waveform data for {file_path}")
                return None

        except Exception as e:
            logger.error(f"Error generating waveform data: {e}")
            return None

    def _probe_audio_file(self, file_path: str) -> dict[str, Any] | None:
        """
        Probe audio file to get stream information and duration.

        Args:
            file_path: Path to the audio/video file

        Returns:
            Dictionary with 'duration' and 'sample_rate', or None if no audio stream
        """
        probe_cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]

        # Using hardcoded ffprobe command with validated file path, not user input
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)  # noqa: S603 # nosec B603
        probe_data = json.loads(result.stdout)

        # Find audio stream
        audio_stream = None
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "audio":
                audio_stream = stream
                break

        if not audio_stream:
            logger.warning(f"No audio stream found in {file_path}")
            return None

        # Get duration from format or stream
        duration = 0.0
        if "duration" in probe_data.get("format", {}):
            duration = float(probe_data["format"]["duration"])
        elif "duration" in audio_stream:
            duration = float(audio_stream["duration"])

        # Get sample rate with default fallback
        sample_rate = int(audio_stream.get("sample_rate", 44100))

        return {"duration": duration, "sample_rate": sample_rate}

    def _extract_raw_audio(self, file_path: str, duration: float) -> np.ndarray | None:
        """
        Extract raw audio data from file using FFmpeg.

        Args:
            file_path: Path to the audio/video file
            duration: Expected duration (used to ensure complete extraction)

        Returns:
            Numpy array of audio samples as float32, or None if extraction failed
        """
        ffmpeg_cmd = ["ffmpeg", "-i", file_path]

        # Add duration flag to ensure we get the complete audio stream
        if duration > 0:
            # Add small buffer (0.5s) to ensure we don't cut off the end
            ffmpeg_cmd.extend(["-t", str(duration + 0.5)])

        ffmpeg_cmd.extend(
            [
                "-f",
                "s16le",  # 16-bit little-endian
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(self.WAVEFORM_SAMPLE_RATE),
                "-ac",
                "1",  # Mono
                "-v",
                "quiet",
                "-",  # Output to stdout
            ]
        )

        logger.debug(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")
        # Using hardcoded ffmpeg command with validated file path, not user input
        result = subprocess.run(ffmpeg_cmd, capture_output=True, check=True, timeout=300)  # noqa: S603 # nosec B603

        # Convert bytes to numpy array
        audio_data = np.frombuffer(result.stdout, dtype=np.int16)

        if len(audio_data) == 0:
            logger.warning(f"No audio data extracted from {file_path}")
            return None

        # Convert to float and normalize
        return audio_data.astype(np.float32) / 32768.0

    def _compute_waveform_samples(self, audio_data: np.ndarray, target_samples: int) -> list[float]:
        """
        Compute waveform samples from audio data.

        Args:
            audio_data: Normalized audio data array
            target_samples: Number of waveform samples to generate

        Returns:
            List of waveform values (floats)
        """
        total_samples = len(audio_data)

        if total_samples <= target_samples:
            # Upsample: interpolate to get enough points
            logger.warning(
                f"Audio has fewer samples ({total_samples}) than target ({target_samples})"
            )
            indices = np.linspace(0, total_samples - 1, target_samples)
            interpolated: list[float] = np.interp(
                indices, np.arange(total_samples), np.abs(audio_data)
            ).tolist()
            return interpolated

        # Downsample: group audio samples into chunks
        chunk_size = total_samples / target_samples
        waveform = []

        for i in range(target_samples):
            start_idx = int(i * chunk_size)
            end_idx = min(int((i + 1) * chunk_size), total_samples)

            if start_idx < total_samples and start_idx < end_idx:
                chunk = audio_data[start_idx:end_idx]
                # Take RMS (root mean square) for better representation
                rms_val = np.sqrt(np.mean(chunk**2)) if len(chunk) > 0 else 0.0
                waveform.append(rms_val)
            else:
                waveform.append(0.0)

        return waveform

    def _normalize_waveform(self, waveform: list[float], target_samples: int) -> list[int]:
        """
        Normalize waveform to 0-255 range and ensure correct length.

        Args:
            waveform: List of waveform float values
            target_samples: Expected number of samples

        Returns:
            List of integers in 0-255 range
        """
        # Ensure we have exactly target_samples
        if len(waveform) < target_samples:
            waveform.extend([0.0] * (target_samples - len(waveform)))
        elif len(waveform) > target_samples:
            waveform = waveform[:target_samples]

        # Normalize to 0-255 range for visualization
        if waveform and max(waveform) > 0:
            max_val = max(waveform)
            return [int(val / max_val * 255) for val in waveform]

        return [0] * target_samples

    def generate_from_16khz_wav(self, wav_path: str) -> dict[str, Any] | None:
        """Generate waveform data from a pre-decoded 16kHz WAV (numpy resample, no FFmpeg).

        Used by Phase 1b shared-volume path to avoid re-running FFmpeg on an already-decoded WAV.
        Resamples 16kHz → 22050Hz using scipy.signal.resample_poly (ratio 441:320).

        Args:
            wav_path: Path to a 16kHz mono int16 WAV file.

        Returns:
            Waveform dict (same shape as generate_waveform_data), or None on failure.
        """
        try:
            import scipy.io.wavfile as wavfile  # type: ignore[import]
            from scipy.signal import resample_poly  # type: ignore[import]

            source_rate, data = wavfile.read(wav_path)
            if data.ndim > 1:
                data = data[:, 0]
            audio = data.astype(np.float32) / 32768.0

            if source_rate != self.WAVEFORM_SAMPLE_RATE:
                from math import gcd

                g = gcd(self.WAVEFORM_SAMPLE_RATE, source_rate)
                audio = resample_poly(audio, self.WAVEFORM_SAMPLE_RATE // g, source_rate // g)

            waveform_cache: dict[str, Any] = {}
            for resolution_name, samples in WAVEFORM_RESOLUTIONS.items():
                waveform = self._compute_waveform_samples(audio, samples)
                normalized = self._normalize_waveform(waveform, samples)
                duration_s = len(audio) / self.WAVEFORM_SAMPLE_RATE
                waveform_cache[f"waveform_{samples}"] = {
                    "data": normalized,
                    "samples": len(normalized),
                    "duration": duration_s,
                    "sample_rate": self.WAVEFORM_SAMPLE_RATE,
                }
                logger.debug(
                    "Generated %s waveform from 16kHz WAV: %d samples", resolution_name, samples
                )

            return waveform_cache if waveform_cache else None
        except Exception as e:
            logger.warning(
                "numpy-resample waveform failed (%s), caller should fall back to FFmpeg", e
            )
            return None

    def _extract_single_waveform(
        self, file_path: str, target_samples: int
    ) -> dict[str, Any] | None:
        """
        Extract waveform data for a single resolution.

        Args:
            file_path: Path to the audio/video file
            target_samples: Number of samples to return

        Returns:
            Dictionary containing waveform data and metadata
        """
        try:
            # Probe file for audio information
            probe_info = self._probe_audio_file(file_path)
            if not probe_info:
                return None

            duration = probe_info["duration"]
            sample_rate = probe_info["sample_rate"]

            # Extract raw audio data
            audio_data = self._extract_raw_audio(file_path, duration)
            if audio_data is None:
                return None

            # Calculate durations and log extraction results
            total_samples = len(audio_data)
            extracted_duration = total_samples / self.WAVEFORM_SAMPLE_RATE
            logger.info(
                f"FFmpeg extraction results: "
                f"expected_duration={duration:.2f}s, "
                f"extracted_duration={extracted_duration:.2f}s, "
                f"audio_samples={total_samples}, "
                f"sample_rate={self.WAVEFORM_SAMPLE_RATE}"
            )

            # Validate extraction - warn if significantly different
            if duration > 0 and abs(extracted_duration - duration) > 1.0:
                logger.warning(
                    f"Extracted duration ({extracted_duration:.2f}s) differs from expected ({duration:.2f}s)"
                )

            # Compute waveform samples and normalize
            actual_duration = extracted_duration if extracted_duration > 0 else duration
            waveform_float = self._compute_waveform_samples(audio_data, target_samples)
            waveform = self._normalize_waveform(waveform_float, target_samples)

            return {
                "waveform": waveform,
                "duration": actual_duration,
                "expected_duration": duration,
                "sample_rate": self.WAVEFORM_SAMPLE_RATE,
                "samples": len(waveform),
                "original_sample_rate": sample_rate,
                "extracted_samples": total_samples,
                "seconds_per_point": actual_duration / target_samples if target_samples > 0 else 0,
            }

        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg error processing file {file_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error extracting waveform from {file_path}: {e}")
            return None
