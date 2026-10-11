"""Per-file processing provenance: the stored document and its wire shape (issue #1229).

``media_file.processing_provenance`` holds ``ProcessingProvenance``; it is validated on
write (``services/file_provenance.record_stage``) and again on read, so a malformed row
degrades to "not recorded" instead of reaching the client. ``ProcessingStepOut`` is what
the file detail and file status responses carry: one display-ready row per step.

The stored entry is a closed set of short facts. It can never hold a URL, a key or a
configuration id (``extra="forbid"`` plus the ``://`` refusal below), because the file
responses reach every viewer the file is shared with.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

StepName = Literal[
    "transcription", "diarization", "voiceprints", "summary", "topics", "speaker_names"
]
Where = Literal["server", "cloud"]
Timing = Literal["word", "segment", "none"]

#: Processing order, which is also the order the UI lists the steps in.
STEP_ORDER: tuple[StepName, ...] = (
    "transcription",
    "diarization",
    "voiceprints",
    "summary",
    "topics",
    "speaker_names",
)


class ProvenanceEntry(BaseModel):
    """What one completed step recorded. Every field is optional: unknown stays unknown."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)
    where: Where | None = None
    engine: str | None = Field(default=None, max_length=32)
    execution: str | None = Field(default=None, max_length=32)
    timing: Timing | None = None
    dims: int | None = Field(default=None, ge=1, le=8192)
    completed_at: datetime | None = None

    @field_validator("provider", "model", "engine", "execution")
    @classmethod
    def _no_urls(cls, value: str | None) -> str | None:
        if value is not None and "://" in value:
            raise ValueError("provenance facts never hold a URL")
        return value


class ProcessingProvenance(BaseModel):
    """The whole stored document."""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = 1
    steps: dict[StepName, ProvenanceEntry] = Field(default_factory=dict)


class ProcessingStepOut(BaseModel):
    """One step of how a file was processed, ready to render."""

    step: StepName
    status: Literal["done", "off", "not_run", "not_recorded"] = Field(
        description=(
            "done: the step ran (see provider/model/where); off: skipped by choice; "
            "not_run: recorded run, this step has not completed; not_recorded: the file "
            "was processed before this was tracked"
        )
    )
    recorded: Literal["full", "legacy", "none"] = Field(
        description="full: recorded by the step; legacy: read from older columns; none"
    )
    provider: str | None = Field(default=None, description="Stable provider id")
    provider_display: str | None = Field(default=None, description="Display name")
    model: str | None = None
    where: Where | None = Field(
        default=None, description="Where the step ran; null when not known or not run"
    )
    timing: Timing | None = Field(
        default=None, description="Transcription only: timestamp granularity of the text"
    )
