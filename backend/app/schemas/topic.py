"""
Pydantic schemas for AI tag and collection suggestions

Simplified schemas for LLM-powered tag and collection suggestions (Issue #79).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field

from app.schemas.base import UUIDBaseSchema

# ============================================================================
# Suggestion Item Schemas
# ============================================================================


class SuggestedTag(BaseModel):
    """
    AI-suggested tag for media file

    Attributes:
        name: Suggested tag name
        confidence: LLM confidence score (0.0-1.0)
        rationale: Brief reasoning for this suggestion (optional)
    """

    name: str = Field(..., description="Tag name")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score (0.0-1.0)")
    rationale: str | None = Field(None, description="Reasoning for suggestion")


class SuggestedCollection(BaseModel):
    """
    AI-suggested collection for organizing media

    Attributes:
        name: Suggested collection name
        confidence: LLM confidence score (0.0-1.0)
        rationale: Brief reasoning for this suggestion (optional)
    """

    name: str = Field(..., description="Suggested collection name")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score (0.0-1.0)")
    rationale: str | None = Field(None, description="Reasoning for suggestion")


# ============================================================================
# AI Suggestion Schemas (Main API models)
# ============================================================================


class TopicSuggestionBase(BaseModel):
    """Base schema for AI suggestions"""


class TopicSuggestionCreate(TopicSuggestionBase):
    """Schema for creating AI suggestions (internal use)"""

    media_file_id: int
    user_id: int
    suggested_tags: list[dict] = Field(default_factory=list)
    suggested_collections: list[dict] = Field(default_factory=list)
    status: str = "pending"


class TopicSuggestionResponse(UUIDBaseSchema):
    """
    AI suggestions response for tags and collections

    Attributes:
        uuid: Public identifier
        media_file_id: Reference to media file (UUID)
        user_id: Reference to user (UUID)
        suggested_collections: AI-suggested collections
        suggested_tags: AI-suggested tags
        status: Suggestion status (pending, reviewed, accepted, rejected)
        created_at: When suggestion was created
    """

    media_file_id: UUID = Field(..., description="Media file UUID")
    user_id: UUID = Field(..., description="User UUID")

    # AI suggestions
    suggested_collections: list[SuggestedCollection] = Field(
        default_factory=list, description="Suggested collections"
    )
    suggested_tags: list[SuggestedTag] = Field(default_factory=list, description="Suggested tags")

    # Metadata
    status: str = Field(..., description="Suggestion status")

    # Auto-apply tracking
    auto_applied_tags: list[str] = Field(default_factory=list, description="Auto-applied tag names")
    auto_applied_collections: list[str] = Field(
        default_factory=list, description="Auto-applied collection names"
    )
    auto_apply_completed_at: datetime | None = Field(None, description="When auto-apply completed")

    # Timestamps
    created_at: datetime = Field(..., description="Creation timestamp")

    model_config = {"from_attributes": True}


# ============================================================================
# Request Schemas (User Actions)
# ============================================================================


class ApplyTopicSuggestionsRequest(BaseModel):
    """
    Request to apply approved tag/collection suggestions

    Attributes:
        accepted_collections: Collection names to create/add to
        accepted_tags: Tag names to apply
    """

    accepted_collections: list[str] = Field(
        default_factory=list, description="Collection names to create/add to"
    )
    accepted_tags: list[str] = Field(default_factory=list, description="Tag names to apply")


class ExtractTopicsRequest(BaseModel):
    """
    Request to extract AI suggestions from transcript

    Attributes:
        force_regenerate: Force re-extraction even if suggestions exist
    """

    force_regenerate: bool = Field(False, description="Force re-extraction")


# ============================================================================
# LLM Response Schemas (Internal parsing)
# ============================================================================


class LLMSuggestionResponse(BaseModel):
    """
    Expected response structure from LLM suggestion extraction

    This matches the JSON structure defined in the extraction prompt.
    Used for parsing and validation of LLM responses.

    Attributes:
        suggested_collections: Suggested collections
        suggested_tags: Suggested tags
    """

    suggested_collections: list[SuggestedCollection]
    suggested_tags: list[SuggestedTag]


class AutoLabelSettingsSchema(BaseModel):
    """User auto-label settings."""

    enabled: bool = Field(True, description="Master enable/disable")
    confidence_threshold: float = Field(
        0.75, ge=0.5, le=1.0, description="Confidence threshold for auto-apply"
    )
    tags_enabled: bool = Field(True, description="Auto-apply tags")
    collections_enabled: bool = Field(True, description="Auto-apply collections")
    bulk_grouping_enabled: bool = Field(True, description="Group bulk imports by topic")


class RetroactiveAutoLabelRequest(BaseModel):
    """Request to retroactively apply auto-labeling."""

    file_uuids: list[str] | None = Field(
        None, max_length=500, description="Specific file UUIDs to process (all if omitted)"
    )
