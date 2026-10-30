"""Request bodies that carry processing options: reprocess and prepare-upload.

Both validate the same speaker-count and Whisper-model options; ``VALID_LOCAL_WHISPER_MODELS``
is the catalog the model check gates on.
"""

from typing import Any
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.core.local_whisper_catalog import LOCAL_WHISPER_MODEL_IDS

# The catalog is the one list of local Whisper models (#1246).
VALID_LOCAL_WHISPER_MODELS = LOCAL_WHISPER_MODEL_IDS


class ReprocessRequest(BaseModel):
    """Request schema for reprocessing a file with optional speaker diarization settings.

    Attributes:
        stages: Pipeline stages to re-run. Empty list = full reprocess (backward compatible).
        min_speakers: Optional minimum number of speakers for diarization
        max_speakers: Optional maximum number of speakers for diarization
        num_speakers: Optional fixed number of speakers for diarization (overrides min/max)
    """

    stages: list[
        Literal[
            "transcription",
            "rediarize",
            "search_indexing",
            "analytics",
            "speaker_llm",
            "summarization",
            "topic_extraction",
        ]
    ] = Field(
        default_factory=list,
        description="Pipeline stages to re-run. Empty list = full reprocess (backward compatible)",
    )

    min_speakers: int | None = Field(
        None, description="Minimum number of speakers for diarization (positive integer)"
    )
    max_speakers: int | None = Field(
        None, description="Maximum number of speakers for diarization (positive integer)"
    )
    num_speakers: int | None = Field(
        None, description="Fixed number of speakers for diarization (overrides min/max when set)"
    )
    disable_diarization: bool | None = Field(
        None,
        description=(
            "Skip speaker diarization for this run. Only meaningful when the transcription "
            "stage runs (a full reprocess, or stages containing 'transcription'); any other "
            "stage selection is rejected with 422."
        ),
    )
    whisper_model: str | None = Field(
        None,
        description="Whisper model to use for reprocessing. "
        "None = use admin-configured default. "
        "Only applies to local ASR provider. Accepts the deployment's model or a "
        "lightweight CPU model (tiny, base); anything else is rejected with 422.",
        examples=["tiny", "base", "large-v3-turbo"],
    )

    @field_validator("min_speakers", "max_speakers", "num_speakers")
    @classmethod
    def validate_speaker_count_positive(cls, v: int | None) -> int | None:
        """Validate that speaker counts are positive integers (>= 1) if provided."""
        if v is not None and v < 1:
            raise ValueError("Speaker count must be at least 1")
        return v

    @field_validator("whisper_model")
    @classmethod
    def validate_whisper_model(cls, v: str | None) -> str | None:
        """Validate that whisper_model is a known local model name."""
        if v is None:
            return v
        v = v.strip()
        if v and v not in VALID_LOCAL_WHISPER_MODELS:
            raise ValueError(
                f"Unknown Whisper model '{v}'. Valid models: {sorted(VALID_LOCAL_WHISPER_MODELS)}"
            )
        return v or None

    @model_validator(mode="after")
    def validate_disable_diarization_stage(self) -> "ReprocessRequest":
        """``disable_diarization`` only changes a run that transcribes."""
        if self.disable_diarization and self.stages and "transcription" not in self.stages:
            raise ValueError(
                "disable_diarization only applies when the 'transcription' stage is "
                "reprocessed; re-diarizing or re-running downstream stages cannot skip "
                "diarization"
            )
        return self

    @model_validator(mode="after")
    def validate_min_max_speakers(self) -> "ReprocessRequest":
        """Validate that min_speakers <= max_speakers if both are provided."""
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError(
                f"min_speakers ({self.min_speakers}) must be less than or equal to "
                f"max_speakers ({self.max_speakers})"
            )
        return self


class PrepareUploadRequest(BaseModel):
    """Request schema for preparing a file upload.

    This schema is used to create a file record before the actual upload starts.

    Attributes:
        filename: Name of the file to be uploaded
        file_size: Size of the file in bytes
        content_type: MIME type of the file
        file_hash: Content fingerprint of the source, for duplicate detection
        extracted_from_video: Optional metadata from original video file (if audio was extracted client-side)
        min_speakers: Optional minimum number of speakers for diarization
        max_speakers: Optional maximum number of speakers for diarization
        num_speakers: Optional fixed number of speakers for diarization (overrides min/max)
        collection_ids: Optional collection UUIDs to add the file to after creation
        tag_names: Optional tag names to apply to the file after creation
    """

    filename: str = Field(..., description="Name of the file to be uploaded")
    file_size: int = Field(..., description="Size of the file in bytes")
    content_type: str = Field(..., description="MIME type of the file")
    file_hash: str | None = Field(
        None,
        description=(
            "Client-computed content fingerprint used for pre-upload duplicate "
            "detection. The browser sends an imohash (32-char hex) — the same "
            "constant-time fingerprint the server computes into MediaFile.imohash, "
            "so a 15 GB file costs the same to fingerprint as a 1 MB one. For "
            "client-extracted audio this is the fingerprint of the SOURCE VIDEO. "
            "A legacy SHA-256 from an older client still matches historical rows."
        ),
    )
    extracted_from_video: dict[str, Any] | None = Field(
        None, description="Metadata from original video file if audio was extracted client-side"
    )
    min_speakers: int | None = Field(
        None, description="Minimum number of speakers for diarization (positive integer)"
    )
    max_speakers: int | None = Field(
        None, description="Maximum number of speakers for diarization (positive integer)"
    )
    num_speakers: int | None = Field(
        None, description="Fixed number of speakers for diarization (overrides min/max when set)"
    )
    disable_diarization: bool | None = Field(
        None,
        description=(
            "Skip speaker diarization entirely; all segments assigned to a single speaker"
        ),
    )
    collection_ids: list[UUID] | None = Field(
        None, description="Collection UUIDs to add the file to after creation"
    )
    tag_names: list[str] | None = Field(
        None, description="Tag names to apply to the file after creation"
    )
    upload_batch_id: UUID | None = Field(
        None,
        description="Client-generated UUID to group files uploaded together into a batch. "
        "All files sharing the same upload_batch_id will be linked to the same UploadBatch record.",
    )
    whisper_model: str | None = Field(
        None,
        description="Whisper model to use for this file. "
        "None = use admin-configured default. "
        "Only applies to local ASR provider. Accepts the deployment's model or a "
        "lightweight CPU model (tiny, base); anything else is rejected with 422.",
        examples=["tiny", "base", "large-v3-turbo"],
    )
    use_presigned: bool | None = Field(
        False,
        description=(
            "When true, the prepare response includes a presigned PUT URL so "
            "the browser can upload bytes directly to MinIO, bypassing the "
            "API container. Requires a follow-up call to /files/complete. "
            "Defaults to the legacy multipart-form upload flow."
        ),
    )

    @field_validator("min_speakers", "max_speakers", "num_speakers")
    @classmethod
    def validate_speaker_count_positive(cls, v: int | None) -> int | None:
        """Validate that speaker counts are positive integers (>= 1) if provided."""
        if v is not None and v < 1:
            raise ValueError("Speaker count must be at least 1")
        return v

    @field_validator("whisper_model")
    @classmethod
    def validate_whisper_model(cls, v: str | None) -> str | None:
        """Validate that whisper_model is a known local model name."""
        if v is None:
            return v
        v = v.strip()
        if v and v not in VALID_LOCAL_WHISPER_MODELS:
            raise ValueError(
                f"Unknown Whisper model '{v}'. Valid models: {sorted(VALID_LOCAL_WHISPER_MODELS)}"
            )
        return v or None

    @model_validator(mode="after")
    def validate_min_max_speakers(self) -> "PrepareUploadRequest":
        """Validate that min_speakers <= max_speakers if both are provided."""
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError(
                f"min_speakers ({self.min_speakers}) must be less than or equal to "
                f"max_speakers ({self.max_speakers})"
            )
        return self
