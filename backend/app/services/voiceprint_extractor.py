"""The voiceprint stage's one interface: audio + speaker turns in, one vector per speaker out.

Voiceprints are always computed on THIS server, whichever detector produced the speaker
turns (local engine, a transcription vendor, pyannote.ai; issue #1225, plan C6). Every
combination that needs vectors computed after the fact goes through
:func:`get_voiceprint_extractor`; the result is matched against profiles and clusters by
code that never knows which implementation produced it.

The contract is deliberately language-neutral so the v0.8.0 implementation (a Rust
extractor in the diar-native family, #1231) can replace :class:`WeSpeakerVoiceprintExtractor`
behind :func:`get_voiceprint_extractor` with no change to callers, storage or clustering:

* input: a path to a 16 kHz mono WAV and a sequence of :class:`SpeakerTurn`
  ``(speaker label, start s, end s)``;
* output: :class:`Voiceprints`, one L2-normalised vector per speaker label that had usable
  audio, in the deployment's speaker-embedding space (the 256-d WeSpeaker
  ``pyannote/wespeaker-voxceleb-resnet34-LM`` space on v4; a v3 install that has not run
  the embedding migration is still 512-d, and ``dims`` says which), plus the model name.

Nothing about a database, a speaker id, or Python objects crosses this boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeakerTurn:
    """One stretch of audio attributed to one speaker label."""

    speaker: str
    start: float
    end: float


@dataclass(frozen=True)
class Voiceprints:
    """Per-speaker vectors (label -> L2-normalised floats), their width and model."""

    vectors: dict[str, list[float]]
    dims: int
    model: str


class VoiceprintExtractor(Protocol):
    """Anything that turns audio plus speaker turns into per-speaker vectors."""

    def extract(self, audio_path: str, turns: Sequence[SpeakerTurn]) -> Voiceprints:
        """Vectors for every speaker in ``turns`` that had usable audio."""
        ...


class WeSpeakerVoiceprintExtractor:
    """Today's implementation: ``SpeakerEmbeddingService`` (diar-native ``/embed_window`` or
    the in-process WeSpeaker fallback, see ``services/CLAUDE.md``).

    Per speaker: adjacent turns are merged, the five longest stretches embedded, and the
    mean L2-normalised. The model is released afterwards so the caller's next phase does
    not hold it.
    """

    def extract(self, audio_path: str, turns: Sequence[SpeakerTurn]) -> Voiceprints:
        from app.services.speaker_embedding_service import SpeakerEmbeddingService

        labels = sorted({turn.speaker for turn in turns})
        index = {label: i for i, label in enumerate(labels)}
        segments = [{"start": t.start, "end": t.end, "speaker": t.speaker} for t in turns]

        service = SpeakerEmbeddingService()
        try:
            raw = service.extract_embeddings_for_segments(audio_path, segments, index)
            vectors = {
                labels[i]: service.aggregate_embeddings(embeddings).astype(float).tolist()
                for i, embeddings in raw.items()
                if embeddings
            }
            return Voiceprints(
                vectors=vectors, dims=service.get_embedding_dimension(), model=service.model_name
            )
        finally:
            service.cleanup()


def get_voiceprint_extractor() -> VoiceprintExtractor:
    """The voiceprint extractor this deployment uses (the one seam the v0.8.0 swap edits)."""
    return WeSpeakerVoiceprintExtractor()
