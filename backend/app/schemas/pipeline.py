"""Wire contract of ``GET /user-settings/pipeline`` (issue #1224).

A read-only view of how the caller's next recording would be processed, built from the
one resolver (``services/pipeline_plan.resolve_pipeline_plan``). Every id here is stable
and doubles as an i18n key suffix; display strings are pre-formatted by the backend.
"""

from typing import Literal

from pydantic import BaseModel
from pydantic import Field

PipelineStepName = Literal["transcription", "diarization", "speaker_id", "llm"]


class PipelineIssueOut(BaseModel):
    code: str = Field(description="Stable code; i18n key settings.pipeline.issue.<code>")
    severity: Literal["error", "warning", "info"]
    step: PipelineStepName
    message: str = Field(description="The fixed English sentence for the code")


class PipelineStepOut(BaseModel):
    step: PipelineStepName
    enabled: bool
    where: Literal["server", "cloud"] | None = Field(
        description="Where the step runs; null when it does not run"
    )
    engine: str = Field(
        description=(
            "Stable id: local_gpu|local_cpu|cloud (transcription), "
            "asr|local|pyannote_ai|asr_config|off (diarization), "
            "inline_native|embedding_stage|none (speaker_id), llm|off (llm)"
        )
    )
    execution: str | None = Field(
        default=None, description="How the step is carried out (diarization only)"
    )
    provider: str | None
    provider_display: str | None
    model: str | None
    returns_speakers: bool | None = Field(
        default=None, description="Transcription step: the model returns speaker labels"
    )
    timing: Literal["word", "segment", "none"] | None = Field(
        default=None, description="Transcription step: timestamp granularity of the text"
    )
    adapter_billing: bool = Field(
        default=False,
        description="Diarization by another transcription provider, billed as a transcription",
    )
    speaker_hints: list[Literal["min", "max", "num"]] = Field(default_factory=list)
    audio_leaves_server: bool
    settings_section: str = Field(description="Settings section id that changes this step")


class PipelineView(BaseModel):
    steps: list[PipelineStepOut] = Field(description="Always four, in processing order")
    issues: list[PipelineIssueOut]
    deployment_mode: Literal["full", "lite"]
    diarization_source_locked: bool


class ASRSwitchResult(BaseModel):
    """Response of the ASR-side switches (``set-active``, ``clear-active``), issue #1225.

    ``pipeline_issues`` is the caller's pipeline AFTER the switch: warnings, and errors the
    switch did not introduce (a switch that would introduce a diarization-step error is
    refused with 409 ``{code, message}`` instead).
    """

    message: str
    config_uuid: str | None = None
    pipeline_issues: list[PipelineIssueOut]
