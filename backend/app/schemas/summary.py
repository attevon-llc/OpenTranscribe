"""
Pydantic schemas for AI summarization functionality

Updated to support flexible summary structures from custom AI prompts.
No hard-coded field requirements - accepts any valid JSON structure.
"""

from datetime import datetime
from typing import Any
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator


# Legacy schemas kept for backward compatibility with default BLUF prompt
class SpeakerInfo(BaseModel):
    """Speaker information (optional, used by default BLUF prompt)"""

    name: str = Field(..., description="Speaker name or label")
    talk_time_seconds: int | float = Field(..., description="Total talk time in seconds")
    percentage: float = Field(..., description="Percentage of total talk time")
    key_points: list[str] = Field(..., description="Key points from this speaker")

    @field_validator("talk_time_seconds")
    @classmethod
    def convert_talk_time_to_int(cls, v):
        if isinstance(v, float):
            return int(round(v))
        return v


class ContentSection(BaseModel):
    """Content section (optional, used by some prompts)"""

    time_range: str = Field(..., description="Time range for this section")
    topic: str = Field(..., description="Topic or title")
    key_points: list[str] = Field(..., description="Key discussion points")


class ActionItem(BaseModel):
    """Action item (optional, used by default BLUF prompt)"""

    text: str = Field(..., description="Action item description")
    assigned_to: str | None = Field(None, description="Person assigned")
    due_date: str | None = Field(None, description="Due date in YYYY-MM-DD format")
    priority: Literal["high", "medium", "low"] = Field(..., description="Priority level")
    context: str = Field(..., description="Context about why this action is needed")
    status: Literal["pending", "completed", "cancelled"] | None = Field("pending")


class SummaryMetadata(BaseModel):
    """Metadata about summary generation (always present)"""

    provider: str = Field(..., description="LLM provider used")
    model: str = Field(..., description="Model name")
    usage_tokens: int | None = None
    transcript_length: int
    processing_time_ms: int | None = None
    confidence_score: float | None = None
    language: str | None = None
    error: str | None = None


class MajorTopic(BaseModel):
    """Major topic (optional, used by default BLUF prompt)"""

    topic: str
    importance: Literal["high", "medium", "low"]
    key_points: list[str]
    participants: list[str]


class SummaryData(BaseModel):
    """
    Flexible summary data structure that accepts ANY valid JSON structure.

    This schema is designed to accommodate custom AI prompts with different
    output formats. Fields from the default BLUF prompt are optional for
    backward compatibility, but any additional fields are allowed.

    Examples:
    - Default BLUF format: {bluf, brief_summary, major_topics, ...}
    - Custom format: {executive_summary, risks, recommendations, ...}
    - Any other valid JSON structure from custom prompts
    """

    model_config = ConfigDict(extra="allow")  # Allow additional fields

    # Optional fields for backward compatibility with default BLUF prompt
    bluf: str | None = None
    brief_summary: str | None = None
    major_topics: list[Any] | None = None
    action_items: list[Any] | None = None
    key_decisions: list[Any] | None = None
    follow_up_items: list[Any] | None = None
    metadata: dict[str, Any] | None = None


class SummaryResponse(BaseModel):
    """Response containing flexible summary data"""

    file_id: UUID
    filename: str | None = None
    summary_data: dict[str, Any]  # Flexible structure - accepts any JSON
    source: Literal["opensearch", "postgresql"]
    document_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SummarySearchRequest(BaseModel):
    query: str | None = None
    speakers: list[str] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    has_pending_actions: bool | None = None
    size: int = 20
    offset: int = 0


class SummarySearchHit(BaseModel):
    document_id: str
    score: float
    file_id: UUID  # Changed from int to UUID
    bluf: str
    brief_summary: str
    created_at: str
    provider: str
    model: str
    highlights: dict[str, list[str]] | None = None


class SummarySearchResponse(BaseModel):
    hits: list[SummarySearchHit]
    total: int
    max_score: float | None = None
    query: str | None = None
    filters: dict[str, Any]


class SummaryAnalyticsResponse(BaseModel):
    total_summaries: int
    speaker_stats: list[dict[str, Any]]
    action_items_trend: list[dict[str, Any]]
    common_topics: list[dict[str, Any]]
    summary_statistics: dict[str, Any]
    provider_usage: list[dict[str, Any]]


class SpeakerIdentificationResponse(BaseModel):
    message: str
    task_id: str
    file_id: UUID  # Changed from int to UUID
    speaker_count: int


class SummaryTaskRequest(BaseModel):
    force_regenerate: bool = False
    prompt_uuid: str | None = None


class SummaryTaskStatus(BaseModel):
    task_id: str
    status: Literal["pending", "in_progress", "completed", "failed"]
    progress: float | None = None
    error_message: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
