"""Pydantic schemas for speaker attribute detection settings."""

from pydantic import BaseModel
from pydantic import Field


class SpeakerAttributeSettings(BaseModel):
    """User-level speaker attribute settings."""

    detection_enabled: bool = True
    gender_detection_enabled: bool = True
    age_detection_enabled: bool = True
    show_attributes_on_cards: bool = True
    auto_llm_speaker_identification: bool = True
    gender_locked_by: str | None = Field(
        default=None,
        description=(
            "The privacy layer above the user (platform, instance or organization) that "
            "forbids gender inference for the caller's active organization, or null. When "
            "set, gender_detection_enabled is false whatever the user chose. Read-only: "
            "a PUT ignores it."
        ),
    )


class SpeakerAttributeSettingsUpdate(BaseModel):
    """Partial update for speaker attribute settings."""

    detection_enabled: bool | None = None
    gender_detection_enabled: bool | None = None
    age_detection_enabled: bool | None = None
    show_attributes_on_cards: bool | None = None
    auto_llm_speaker_identification: bool | None = None


class SpeakerAttributeSystemDefaults(BaseModel):
    """System-level defaults for speaker attribute settings."""

    detection_enabled: bool
    gender_detection_enabled: bool
    age_detection_enabled: bool
    show_attributes_on_cards: bool
    auto_llm_speaker_identification: bool
