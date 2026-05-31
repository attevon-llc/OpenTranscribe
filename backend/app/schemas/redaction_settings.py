"""Pydantic schemas for content-redaction settings (per-user) + admin policy."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field

from app.core import constants as C  # noqa: N812


class RedactionSettings(BaseModel):
    """A user's effective redaction preferences (response)."""

    enabled: bool = C.DEFAULT_REDACTION_ENABLED
    detectors: list[str] = Field(default_factory=lambda: list(C.DEFAULT_REDACTION_DETECTORS))
    categories: list[str] = Field(default_factory=lambda: list(C.DEFAULT_REDACTION_CATEGORIES))
    pii_entities: list[str] = Field(default_factory=lambda: list(C.DEFAULT_REDACTION_PII_ENTITIES))
    style: str = C.DEFAULT_REDACTION_STYLE
    custom_words: list[str] = Field(default_factory=list)
    allowlist: list[str] = Field(default_factory=list)
    toxicity_threshold: float = Field(
        default=C.DEFAULT_REDACTION_TOXICITY_THRESHOLD, ge=0.0, le=1.0
    )
    redact_before_llm: bool = C.DEFAULT_REDACTION_REDACT_BEFORE_LLM
    default_export_redacted: bool = C.DEFAULT_REDACTION_DEFAULT_EXPORT_REDACTED


class RedactionSettingsUpdate(BaseModel):
    """Partial update of a user's redaction preferences (all optional)."""

    enabled: bool | None = None
    detectors: list[str] | None = None
    categories: list[str] | None = None
    pii_entities: list[str] | None = None
    style: str | None = None
    custom_words: list[str] | None = None
    allowlist: list[str] | None = None
    toxicity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    redact_before_llm: bool | None = None
    default_export_redacted: bool | None = None


class RedactionSystemDefaults(BaseModel):
    """Metadata for the settings UI: option lists + the admin-forced/locked set."""

    available_detectors: list[str] = Field(default_factory=lambda: list(C.REDACTION_DETECTORS))
    available_categories: list[str] = Field(default_factory=lambda: list(C.REDACTION_CATEGORIES))
    available_pii_entities: list[str] = Field(
        default_factory=lambda: list(C.REDACTION_PII_ENTITIES)
    )
    available_styles: list[str] = Field(default_factory=lambda: list(C.REDACTION_STYLES))
    # Admin-forced categories the user cannot disable (rendered locked in the UI).
    locked_categories: list[str] = Field(default_factory=list)
    export_locked: bool = False
    redact_before_llm_locked: bool = False
    # Per-detector language coverage (so the UI can warn about unsupported languages).
    profanity_languages: list[str] = Field(
        default_factory=lambda: list(C.REDACTION_PROFANITY_LANGUAGES)
    )
    pii_languages: list[str] = Field(default_factory=lambda: list(C.REDACTION_PII_LANGUAGES))
    toxicity_languages: list[str] = Field(
        default_factory=lambda: list(C.REDACTION_TOXICITY_LANGUAGES)
    )


class RedactionPolicy(BaseModel):
    """Admin governance policy — the enforcement floor for all users (response)."""

    force_pii: bool = False
    force_pii_entities: list[str] = Field(default_factory=list)
    force_toxicity: bool = False
    force_toxicity_threshold: float = Field(
        default=C.DEFAULT_REDACTION_TOXICITY_THRESHOLD, ge=0.0, le=1.0
    )
    force_profanity: bool = False
    force_custom_words: list[str] = Field(default_factory=list)
    force_export_redacted: bool = False
    force_redact_before_llm: bool = False
    # Enhanced name detection (GLiNER) — higher accuracy, much slower. Off = fast spaCy NER.
    pii_use_gliner: bool = False


class RedactionPolicyUpdate(BaseModel):
    """Partial update of the admin governance policy (all optional)."""

    force_pii: bool | None = None
    force_pii_entities: list[str] | None = None
    force_toxicity: bool | None = None
    force_toxicity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    force_profanity: bool | None = None
    force_custom_words: list[str] | None = None
    force_export_redacted: bool | None = None
    force_redact_before_llm: bool | None = None
    pii_use_gliner: bool | None = None
