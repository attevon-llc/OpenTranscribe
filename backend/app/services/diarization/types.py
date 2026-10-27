"""Shared data types for diarization providers."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field


@dataclass
class DiarizeConfig:
    """Configuration passed to each diarization provider."""

    min_speakers: int = 1
    max_speakers: int = 20
    num_speakers: int | None = None
    api_key: str | None = None
    model_name: str | None = None
    #: A WAV the diar-native sidecar can open directly (the pipeline scratch copy), so this
    #: server's engine need not re-encode the audio. Only the local provider reads it.
    sidecar_wav_path: str | None = None
    #: The privacy policy's answers for this file (``services/privacy/policy``). Only the
    #: local provider reads them: ``False`` means no per-speaker centroids come back and the
    #: sidecar is not asked for gender. They default to allowed because a cloud detector has
    #: neither to give; the post stage sets them from the file's own policy.
    return_centroids: bool = True
    request_gender: bool = True


@dataclass
class DiarizeSegment:
    """A speaker segment from diarization output."""

    start: float
    end: float
    speaker: str  # Normalized SPEAKER_XX format


@dataclass
class DiarizeResult:
    """Full result from a diarization provider."""

    segments: list[DiarizeSegment]
    num_speakers: int
    provider_name: str
    model_name: str = ""
    metadata: dict = field(default_factory=dict)
