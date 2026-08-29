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


class ActionItem(BaseModel):
    """Action item (optional, used by default BLUF prompt)"""

    text: str = Field(..., description="Action item description")
    assigned_to: str | None = Field(None, description="Person assigned")
    due_date: str | None = Field(None, description="Due date in YYYY-MM-DD format")
    priority: Literal["high", "medium", "low"] = Field(..., description="Priority level")
    context: str = Field(..., description="Context about why this action is needed")
    status: Literal["pending", "completed", "cancelled"] | None = Field("pending")


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
    """Response containing flexible summary data.

    ``source`` / ``document_id`` / ``created_at`` / ``updated_at`` used to name
    *which OpenSearch document in ``transcript_summaries`` answered*. That index is
    retired (#67), the summary lives only in ``media_file.summary_data``, and no
    frontend ever read those fields — a ``source`` that can hold exactly one value
    is a field pretending to be a choice.
    """

    file_id: UUID
    filename: str | None = None
    summary_data: dict[str, Any]  # Flexible structure - accepts any JSON


# NOTE: ``SummarySearchRequest`` / ``SummarySearchHit`` / ``SummarySearchResponse``
# and ``SummaryAnalyticsResponse`` were removed here. They shaped
# ``POST /api/files/search`` and the never-mounted ``GET /analytics``, both of which
# queried the retired ``transcript_summaries`` index (#67) and nothing else.


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
