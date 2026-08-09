"""Audio loading for the transcription pipeline.

Uses faster_whisper.decode_audio() which is the same function WhisperX
calls internally via whisperx.load_audio().

This is the ONLY audio-loading path in the app for a reason: torchcodec (a
transitive dependency pulled in by pyannote.audio's audio I/O layer) cannot
load its native library in this environment — its GPU decoder needs CUDA
13's libnvrtc.so.13, but torch here is built against cu128 (CUDA 12.8).
torchaudio.load() defaults to the torchcodec backend as of the torchaudio
version pinned here, so it raises RuntimeError on any call. decode_audio()
never touches torchcodec (it shells out via PyAV/ffmpeg), and everywhere
this app calls pyannote's Pipeline it passes an already-loaded
`{"waveform": tensor, "sample_rate": ...}` dict (see diarizer.py) rather
than a file path — pyannote's own file-path-based `Audio().get_duration()`
telemetry path is the other place that reaches for torchcodec, and dict
inputs skip it entirely. Verified empirically: faster-whisper transcription,
pyannote diarization (via a dict input, matching diarizer.py exactly), and
GLiNER redaction NER all run correctly end-to-end on GPU in this
environment. Do not add a torchaudio.load() call or pass a raw file path
into a pyannote Pipeline — either will hit the broken torchcodec backend.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def load_audio(file_path: str) -> np.ndarray:
    """Load audio as 16kHz mono float32 numpy array.

    Args:
        file_path: Path to the audio file.

    Returns:
        Audio waveform as numpy array, shape (samples,).

    Raises:
        ValueError: If audio is empty, too short, or cannot be loaded.
    """
    from faster_whisper.audio import decode_audio

    logger.info(f"Loading audio: {file_path}")
    try:
        audio = decode_audio(file_path, sampling_rate=SAMPLE_RATE)
    except Exception as e:
        raise ValueError(
            f"Unable to load audio content. The file may be corrupted, "
            f"in an unsupported format, or contain no audio data: {e}"
        ) from e

    if audio is None or len(audio) == 0:
        raise ValueError("Audio file appears to be empty or corrupted")

    duration = len(audio) / SAMPLE_RATE
    if duration < 0.1:
        raise ValueError("Audio file is too short to contain meaningful content")

    logger.info(f"Audio loaded: {duration:.1f}s ({len(audio)} samples)")
    return audio  # type: ignore[no-any-return]
