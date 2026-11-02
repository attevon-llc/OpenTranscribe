"""Audio loading and RTTM export."""

from __future__ import annotations

from pyannote_direct.config import logger


def load_waveform(audio_path: str):
    """Load audio into a torch Tensor. Tries pyannote.audio.Audio (torchcodec)
    first, then falls back to scipy.io.wavfile for WAV inputs.

    Returns
    -------
    waveform : torch.Tensor
        Shape (channels, samples), float32 in [-1, 1].
    sample_rate : int
    """
    import numpy as np
    import torch

    # Prefer pyannote's loader — respects torchcodec when available.
    try:
        from pyannote.audio import Audio

        loader = Audio(mono='downmix')
        waveform, sample_rate = loader({'audio': audio_path})
        return waveform, sample_rate
    except Exception as e:
        logger.debug('pyannote.Audio loader failed (%s); falling back to scipy', e)

    # Fallback: scipy.io.wavfile — WAV-only, but zero extra deps.
    from scipy.io import wavfile

    sample_rate, data = wavfile.read(audio_path)
    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        data = data.astype(np.float32) / 2147483648.0
    elif data.dtype == np.uint8:
        data = (data.astype(np.float32) - 128.0) / 128.0
    elif data.dtype == np.float64:
        data = data.astype(np.float32)
    if data.ndim == 1:
        data = data[np.newaxis, :]
    else:
        data = data.T  # (samples, channels) -> (channels, samples)
        # Downmix to mono to match pyannote's mono="downmix" default
        data = data.mean(axis=0, keepdims=True)
    waveform = torch.from_numpy(np.ascontiguousarray(data))
    return waveform, int(sample_rate)


# ---------------------------------------------------------------------------
# RTTM export
# ---------------------------------------------------------------------------


def annotation_to_rttm(annotation, uri: str) -> str:
    """Serialize an Annotation to RTTM format.

    Uses pyannote's built-in RTTM serializer when available; falls back to a
    manual emitter if the API is different across versions.
    """
    import io

    try:
        buf = io.StringIO()
        annotation.write_rttm(buf)
        content = buf.getvalue()
        if content:
            return content
    except Exception:
        pass

    # Manual fallback
    lines = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        # SPEAKER <uri> 1 <start> <dur> <NA> <NA> <spk> <NA> <NA>
        dur = turn.end - turn.start
        lines.append(f'SPEAKER {uri} 1 {turn.start:.3f} {dur:.3f} <NA> <NA> {speaker} <NA> <NA>')
    return '\n'.join(lines) + '\n'
