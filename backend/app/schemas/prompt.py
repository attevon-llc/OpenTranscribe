"""
Pydantic schemas for AI summarization prompt management
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

from app.schemas.base import UUIDBaseSchema


class SummaryPromptBase(BaseModel):
    """Base schema for summary prompts"""

    name: str = Field(..., max_length=255, description="User-friendly name for the prompt")
    description: str | None = Field(
        None, description="Optional description of what this prompt is for"
    )
    prompt_text: str = Field(..., description="The actual prompt content")
    content_type: str | None = Field(
        None,
        description="Content type: meeting, interview, podcast, documentary, general, qa_panel",
    )
    is_active: bool = Field(True, description="Whether the prompt is available for use")
    is_shared: bool = Field(False, description="Whether this prompt is shared with all users")
    tags: list[str] = Field(default_factory=list)

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v):
        if v is not None:
            valid_types = {
                "meeting",
                "interview",
                "podcast",
                "documentary",
                "general",
                "speaker_identification",
                # The community Q&A-panel extractor seeded in initial_data (#136).
                "qa_panel",
            }
            if v not in valid_types:
                raise ValueError(f"content_type must be one of: {valid_types}")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v):
        if len(v) > 20:
            raise ValueError("Maximum 20 tags allowed per prompt")
        cleaned = []
        for tag in v:
            tag = tag.strip().lower()
            if not tag or len(tag) > 50:
                continue
            if not all(c.isalnum() or c in "-_ " for c in tag):
                raise ValueError(f"Tag '{tag}' contains invalid characters")
            cleaned.append(tag)
        return list(dict.fromkeys(cleaned))


class SummaryPromptCreate(SummaryPromptBase):
    """Schema for creating a new summary prompt"""


class SummaryPromptUpdate(BaseModel):
    """Schema for updating an existing summary prompt"""

    name: str | None = Field(None, max_length=255)
    description: str | None = None
    prompt_text: str | None = None
    content_type: str | None = None
    is_active: bool | None = None
    tags: list[str] | None = None

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v):
        if v is not None:
            valid_types = {
                "meeting",
                "interview",
                "podcast",
                "documentary",
                "general",
                "speaker_identification",
                # The community Q&A-panel extractor seeded in initial_data (#136).
                "qa_panel",
            }
            if v not in valid_types:
                raise ValueError(f"content_type must be one of: {valid_types}")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v):
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("Maximum 20 tags allowed per prompt")
        cleaned = []
        for tag in v:
            tag = tag.strip().lower()
            if not tag or len(tag) > 50:
                continue
            if not all(c.isalnum() or c in "-_ " for c in tag):
                raise ValueError(f"Tag '{tag}' contains invalid characters")
            cleaned.append(tag)
        return list(dict.fromkeys(cleaned))


class SummaryPrompt(SummaryPromptBase, UUIDBaseSchema):
    """Schema for summary prompt responses with UUID as public identifier"""

    user_id: UUID | None = Field(
        None, description="User ID for custom prompts, null for system prompts"
    )
    is_system_default: bool = Field(False, description="Whether this is a system-provided prompt")
    is_shared: bool = False
    shared_at: datetime | None = None
    shared_by: UUID | None = None
    shared_by_name: str | None = None
    tags: list[str] = []
    usage_count: int = 0
    author_name: str | None = None
    author_role: str | None = None
    is_owner: bool = False
    created_at: datetime
    updated_at: datetime


class LinkedCollection(BaseModel):
    """Minimal collection info for display in prompt listings"""

    uuid: UUID
    name: str


class SummaryPromptWithCollections(SummaryPrompt):
    """Summary prompt with linked collection information"""

    linked_collections: list[LinkedCollection] = []


class SummaryPromptList(BaseModel):
    """Schema for paginated summary prompt lists"""

    prompts: list[SummaryPromptWithCollections]
    total: int
    page: int
    size: int
    has_next: bool
    has_prev: bool


# User Settings Schemas
class UserSettingBase(BaseModel):
    """Base schema for user settings"""

    setting_key: str = Field(..., max_length=100, description="Setting key")
    setting_value: str | None = Field(None, description="Setting value (JSON or simple value)")


class UserSettingCreate(UserSettingBase):
    """Schema for creating a new user setting"""


class UserSettingUpdate(BaseModel):
    """Schema for updating an existing user setting"""

    setting_value: str | None = None


class UserSetting(UserSettingBase, UUIDBaseSchema):
    """Schema for user setting responses with UUID as public identifier"""

    user_id: UUID
    created_at: datetime
    updated_at: datetime


class UserSettingsList(BaseModel):
    """Schema for user settings list"""

    settings: list[UserSetting]


# Summary prompt selection schemas
class ActivePromptSelection(BaseModel):
    """Schema for selecting an active summary prompt"""

    prompt_id: UUID = Field(..., description="UUID of the summary prompt to use")


class ActivePromptResponse(BaseModel):
    """Schema for active prompt response"""

    active_prompt_id: UUID | None = Field(None, description="Currently active prompt UUID")
    active_prompt: SummaryPrompt | None = Field(None, description="Currently active prompt details")


class SummaryPromptShare(BaseModel):
    """Schema for toggling prompt sharing"""

    is_shared: bool


class SharedPromptLibrary(BaseModel):
    """Response for the shared prompt browsing endpoint"""

    prompts: list[SummaryPrompt]
    total: int
    page: int
    size: int
    has_next: bool
    has_prev: bool
    available_tags: list[str] = []


# Content type specific prompt schemas
class ContentTypePromptsResponse(BaseModel):
    """Schema for getting prompts by content type"""

    content_type: str
    system_prompts: list[SummaryPrompt]
    user_prompts: list[SummaryPrompt]
    shared_prompts: list[SummaryPrompt] = []
    active_prompt_id: UUID | None = None
