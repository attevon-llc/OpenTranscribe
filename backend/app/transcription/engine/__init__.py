"""OpenTranscribe Engine — combined transcription + diarization pipeline.

Public API:
    Engine          — main entry point
    EngineConfig    — configuration (wraps TranscriptionConfig)
    JobSpec         — input spec for a single job
    JobResult       — final output
    PreprocessResult  — Stage 1 → Stage 2 handoff
    RawInferenceResult — Stage 2 → Stage 3 handoff
"""

from app.transcription.engine.config import EngineConfig
from app.transcription.engine.engine import Engine
from app.transcription.engine.job import JobResult
from app.transcription.engine.job import JobSpec
from app.transcription.engine.job import PreprocessResult
from app.transcription.engine.job import RawInferenceResult

__all__ = [
    "Engine",
    "EngineConfig",
    "JobSpec",
    "JobResult",
    "PreprocessResult",
    "RawInferenceResult",
]
