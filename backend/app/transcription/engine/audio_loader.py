"""Audio loading for the engine with optional shared-volume mmap support.

Phase 1a: wraps existing load_audio().
Phase 1b: adds shared-volume WAV write in Stage 1 and mmap-load in Stage 2.
"""

from __future__ import annotations

import logging
import os
import re

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def write_wav_to_shared_volume(
    audio: np.ndarray,
    shared_volume_path: str,
    task_id: str,
) -> str | None:
    """Write normalized WAV to shared volume so Stage 2 can mmap-load it.

    Args:
        audio: 16kHz mono float32 numpy array.
        shared_volume_path: Directory to write the WAV into.
        task_id: Used to form a unique filename.

    Returns:
        Absolute path of the written WAV, or None if write failed.
    """
    if not shared_volume_path:
        return None

    try:
        os.makedirs(shared_volume_path, exist_ok=True)
        safe_id = re.sub(r"[^a-zA-Z0-9\-]", "_", task_id)
        wav_path = os.path.join(shared_volume_path, f"{safe_id}.wav")
        import scipy.io.wavfile as wavfile  # type: ignore[import]

        # Convert float32 [-1, 1] to int16 for standard WAV
        audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        wavfile.write(wav_path, SAMPLE_RATE, audio_int16)
        logger.debug(f"Wrote shared-volume WAV: {wav_path} ({len(audio_int16)} samples)")
        return wav_path
    except PermissionError as e:
        # A volume created before the image reserved these paths lands root-owned, which
        # this non-root worker cannot write — the handoff then silently degrades to a
        # re-decode in Stage 2. Name the repair rather than leaving an EACCES to decode.
        logger.warning(
            "Cannot write the shared-volume WAV for task %s: %s. %s is not writable by "
            "this worker (uid %d) — run scripts/fix-shared-volume-perms.sh to repair the "
            "volume; the pipeline continues without the handoff.",
            task_id,
            e,
            shared_volume_path,
            os.getuid(),
        )
        return None
    except Exception as e:
        logger.warning(f"Failed to write shared-volume WAV for task {task_id}: {e}")
        return None


def load_from_shared_volume(wav_path: str) -> np.ndarray | None:
    """Load audio from shared-volume WAV (sub-second mmap-read).

    Args:
        wav_path: Absolute path written by write_wav_to_shared_volume.

    Returns:
        float32 numpy array at 16kHz, or None if path missing.
    """
    if not wav_path or not os.path.exists(wav_path):
        return None
    try:
        import scipy.io.wavfile as wavfile  # type: ignore[import]

        _, data = wavfile.read(wav_path, mmap=True)
        audio: np.ndarray = data.astype(np.float32) / 32767.0
        logger.debug(f"mmap-loaded shared-volume WAV: {wav_path} ({len(audio)} samples)")
        return audio
    except Exception as e:
        logger.warning(f"Failed to mmap-load shared-volume WAV {wav_path}: {e}")
        return None


def cleanup_shared_volume_wav(wav_path: str | None) -> None:
    """Remove the temporary shared-volume WAV after Stage 3 completes."""
    if wav_path and os.path.exists(wav_path):
        try:
            os.unlink(wav_path)
            logger.debug(f"Removed shared-volume WAV: {wav_path}")
        except Exception as e:
            logger.debug(f"Could not remove shared-volume WAV {wav_path}: {e}")
