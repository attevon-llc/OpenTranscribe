"""Lightweight container for diarization output, replacing pandas DataFrame."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class DiarizeResult:
    """Typed struct-of-arrays for diarization segments.

    Replaces the pd.DataFrame previously returned by SpeakerDiarizer.diarize.
    Consumers only need start/end/speaker columns as numpy arrays.
    """

    start: np.ndarray  # float64, shape (N,)
    end: np.ndarray  # float64, shape (N,)
    speaker: np.ndarray  # object dtype, shape (N,) — string labels
    # Per-speaker gender, when the engine worked it out while it had the audio (the sidecar
    # classifies from the same decoded buffer). None on the PyAnnote path, which leaves
    # gender to the enrichment task.
    speaker_gender: dict | None = None

    def __len__(self) -> int:
        return int(self.start.shape[0])

    def to_records(self) -> list[dict]:
        """Serialize to list of dicts for JSON persistence."""
        return [
            {"start": float(s), "end": float(e), "speaker": str(sp)}
            for s, e, sp in zip(self.start, self.end, self.speaker, strict=True)
        ]
