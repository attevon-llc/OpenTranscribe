"""Real pipeline plans for stage tests (issue #1225).

Every pipeline stage reads what to run from ``context["pipeline_plan"]``, which dispatch
computes with the REAL resolver. Tests build it the same way, through
:func:`resolve_pipeline_plan`, so a stage test can never run against a plan shape the
resolver would not produce.
"""

from __future__ import annotations

from typing import Any

from app.services.pipeline_plan import PipelinePlan
from app.services.pipeline_plan import PlanInputs
from app.services.pipeline_plan import resolve_pipeline_plan

_ALL = frozenset({"min", "max", "num"})

#: Transcription profiles, by what matters to the stages.
TRANSCRIPTION: dict[str, dict[str, Any]] = {
    "local_gpu": {
        "asr_provider": "local",
        "asr_model": "large-v3-turbo",
        "asr_returns_speakers": True,
        "asr_speaker_hints": _ALL,
        "asr_timing": "word",
    },
    "local_cpu": {
        "asr_provider": "local",
        "asr_model": "large-v3-turbo",
        "asr_returns_speakers": True,
        "asr_speaker_hints": _ALL,
        "asr_timing": "word",
        "cpu_whisper_model": "tiny",
    },
    "cloud_speakers": {
        "asr_provider": "deepgram",
        "asr_model": "nova-3",
        "asr_returns_speakers": True,
        "asr_speaker_hints": frozenset(),
        "asr_timing": "word",
    },
    "cloud_text_word": {
        "asr_provider": "deepgram",
        "asr_model": "nova-3",
        "asr_returns_speakers": False,
        "asr_speaker_hints": frozenset(),
        "asr_timing": "word",
    },
    "cloud_text_segment": {
        "asr_provider": "openai",
        "asr_model": "whisper-1",
        "asr_returns_speakers": False,
        "asr_speaker_hints": frozenset(),
        "asr_timing": "segment",
    },
}


def make_plan(
    transcription: str = "local_gpu",
    source: str = "provider",
    *,
    deployment_mode: str = "full",
    has_pyannote_key: bool = True,
    gpu_split: bool = False,
) -> PipelinePlan:
    """The plan the resolver produces for one matrix cell."""
    return resolve_pipeline_plan(
        PlanInputs(
            deployment_mode=deployment_mode,
            asr_config_uuid=None,
            diarization_source=source,
            has_pyannote_key=has_pyannote_key,
            pyannote_model="precision-3" if source == "pyannote" else None,
            gpu_split=gpu_split,
            **TRANSCRIPTION[transcription],
        )
    )


def plan_context(transcription: str = "local_gpu", source: str = "provider", **kwargs) -> dict:
    """``make_plan(...).to_context()`` — what dispatch puts in ``preprocess_context``."""
    return make_plan(transcription, source, **kwargs).to_context()
