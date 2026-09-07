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


# Chunk size (in samples) for the scaled read below. 4_000_000 int16 samples is 8 MB per
# mmap page-in and produces a 16 MB float32 chunk — small enough to bound the working set,
# large enough that the per-chunk Python/numpy overhead is negligible against a 3h file.
_SCALED_READ_CHUNK_SAMPLES = 4_000_000


def load_from_shared_volume(wav_path: str) -> np.ndarray | None:
    """Load audio from shared-volume WAV via a lazy, chunked scaled read.

    ``wavfile.read(..., mmap=True)`` opens the file without paging it in, but the
    int16 -> float32 ``/32767`` normalization below still has to touch every sample —
    the mmap by itself only saves a *disk* read, not the memory this function must
    still produce. What it *does* avoid, by converting in fixed-size chunks straight
    into a preallocated float32 output array (with the divide applied ``in place`` on
    each chunk), is ever holding a second full-length float32 temporary at once: the
    naive ``data.astype(np.float32) / 32767.0`` allocates one array for the cast and a
    *second* for the divide, doubling peak memory over one 3h/16kHz mono file's ~691 MB
    float32 footprint. Peak here is the one output array plus one chunk's worth of
    scratch (issue #661 E4).

    ⚠️ Scaling to [-1, 1] is not optional and must not be skipped or reordered: faster_whisper's
    feature extractor only checks ``dtype != float32`` and casts without dividing, so handing
    it a raw (unscaled) int16-derived array yields amplitudes ~32767x too large with no
    exception — a silently garbage transcript. Do not "simplify" this into a dtype-only view.

    Args:
        wav_path: Absolute path written by write_wav_to_shared_volume.

    Returns:
        float32 numpy array at 16kHz, normalized to [-1, 1], or None if path missing.
    """
    if not wav_path or not os.path.exists(wav_path):
        return None
    try:
        import scipy.io.wavfile as wavfile  # type: ignore[import]

        _, data = wavfile.read(wav_path, mmap=True)
        n_samples = data.shape[0]
        audio = np.empty(data.shape, dtype=np.float32)
        for start in range(0, n_samples, _SCALED_READ_CHUNK_SAMPLES):
            end = min(start + _SCALED_READ_CHUNK_SAMPLES, n_samples)
            # Assignment casts int16 -> float32 for this chunk only; the in-place
            # divide then scales it without allocating a second chunk-sized array.
            audio[start:end] = data[start:end]
            audio[start:end] /= 32767.0
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
