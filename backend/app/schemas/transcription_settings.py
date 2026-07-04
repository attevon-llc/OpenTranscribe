"""
Pydantic schemas for user transcription settings.

These schemas define the request/response models for user-level transcription
preferences including speaker detection, prompt behavior, and garbage cleanup settings.
"""

from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from app.core.constants import DEFAULT_DIARIZATION_SOURCE
from app.core.constants import DEFAULT_GARBAGE_CLEANUP_ENABLED
from app.core.constants import DEFAULT_GARBAGE_CLEANUP_THRESHOLD
from app.core.constants import DEFAULT_LLM_OUTPUT_LANGUAGE
from app.core.constants import DEFAULT_SOURCE_LANGUAGE
from app.core.constants import DEFAULT_SPEAKER_PROMPT_BEHAVIOR
from app.core.constants import DEFAULT_TRANSCRIPTION_MAX_SPEAKERS
from app.core.constants import DEFAULT_TRANSCRIPTION_MIN_SPEAKERS
from app.core.constants import DEFAULT_TRANSLATE_TO_ENGLISH
from app.core.constants import DEFAULT_VAD_MIN_SILENCE_MS
from app.core.constants import DEFAULT_VAD_MIN_SPEECH_MS
from app.core.constants import DEFAULT_VAD_SPEECH_PAD_MS
from app.core.constants import DEFAULT_VAD_THRESHOLD
from app.core.constants import VALID_DIARIZATION_SOURCES
from app.core.constants import VALID_SPEAKER_PROMPT_BEHAVIORS

# Type alias for speaker prompt behavior
SpeakerPromptBehavior = Literal["always_prompt", "use_defaults", "use_custom"]


class TranscriptionSettings(BaseModel):
    """
    Response schema for user transcription settings.

    Contains all transcription-related user preferences with their current values.
    """

    min_speakers: int = Field(
        default=DEFAULT_TRANSCRIPTION_MIN_SPEAKERS,
        ge=1,
        le=100,
        description="Minimum number of speakers to detect during diarization",
    )
    max_speakers: int = Field(
        default=DEFAULT_TRANSCRIPTION_MAX_SPEAKERS,
        ge=1,
        le=100,
        description="Maximum number of speakers to detect during diarization",
    )
    speaker_prompt_behavior: SpeakerPromptBehavior = Field(
        default=DEFAULT_SPEAKER_PROMPT_BEHAVIOR,  # type: ignore[arg-type]
        description="How to handle speaker count prompts: always_prompt, use_defaults, or use_custom",
    )
    garbage_cleanup_enabled: bool = Field(
        default=DEFAULT_GARBAGE_CLEANUP_ENABLED,
        description="Whether automatic garbage segment cleanup is enabled",
    )
    garbage_cleanup_threshold: int = Field(
        default=DEFAULT_GARBAGE_CLEANUP_THRESHOLD,
        ge=0,
        le=100,
        description="Confidence threshold (0-100) below which segments are flagged as garbage",
    )
    source_language: str = Field(
        default=DEFAULT_SOURCE_LANGUAGE,
        description="Source language hint for transcription (ISO 639-1 code or 'auto')",
    )
    translate_to_english: bool = Field(
        default=DEFAULT_TRANSLATE_TO_ENGLISH,
        description="Whether to translate non-English audio to English",
    )
    llm_output_language: str = Field(
        default=DEFAULT_LLM_OUTPUT_LANGUAGE,
        description="Language for LLM-generated summaries and analysis (ISO 639-1 code)",
    )
    # VAD settings
    vad_threshold: float = Field(
        default=DEFAULT_VAD_THRESHOLD,
        ge=0.1,
        le=0.95,
        description="Speech detection sensitivity (lower=more speech detected)",
    )
    vad_min_silence_ms: int = Field(
        default=DEFAULT_VAD_MIN_SILENCE_MS,
        ge=100,
        le=5000,
        description="Minimum silence duration (ms) to split segments",
    )
    vad_min_speech_ms: int = Field(
        default=DEFAULT_VAD_MIN_SPEECH_MS,
        ge=50,
        le=5000,
        description="Minimum speech duration (ms) to keep a segment",
    )
    vad_speech_pad_ms: int = Field(
        default=DEFAULT_VAD_SPEECH_PAD_MS,
        ge=0,
        le=2000,
        description="Padding (ms) around detected speech segments",
    )
    # Accuracy settings
    hallucination_silence_threshold: float | None = Field(
        default=None,
        description="Skip hallucinated text during silence gaps >= N seconds (null=disabled)",
    )
    repetition_penalty: float = Field(
        default=1.0,
        ge=1.0,
        le=2.0,
        description="Penalize repetitive output (1.0=off, 1.1-1.3=recommended)",
    )
    # Diarization control
    diarization_source: str = Field(
        default=DEFAULT_DIARIZATION_SOURCE,
        description=(
            "Diarization source: 'provider' (use ASR provider's diarization), "
            "'local' (always use local PyAnnote on GPU), "
            "'pyannote' (pyannote.ai cloud diarization), "
            "'off' (no diarization)"
        ),
    )

    class Config:
        json_schema_extra = {
            "example": {
                "min_speakers": 1,
                "max_speakers": 20,
                "speaker_prompt_behavior": "always_prompt",
                "garbage_cleanup_enabled": True,
                "garbage_cleanup_threshold": 50,
                "source_language": "auto",
                "translate_to_english": False,
                "llm_output_language": "en",
                "vad_threshold": 0.5,
                "vad_min_silence_ms": 2000,
                "vad_min_speech_ms": 250,
                "vad_speech_pad_ms": 400,
                "hallucination_silence_threshold": None,
                "repetition_penalty": 1.0,
                "diarization_source": "provider",
            }
        }


class TranscriptionSettingsUpdate(BaseModel):
    """
    Request schema for updating user transcription settings.

    All fields are optional - only provided fields will be updated.
    """

    min_speakers: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Minimum number of speakers to detect during diarization",
    )
    max_speakers: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Maximum number of speakers to detect during diarization",
    )
    speaker_prompt_behavior: SpeakerPromptBehavior | None = Field(
        default=None,
        description="How to handle speaker count prompts: always_prompt, use_defaults, or use_custom",
    )
    garbage_cleanup_enabled: bool | None = Field(
        default=None,
        description="Whether automatic garbage segment cleanup is enabled",
    )
    garbage_cleanup_threshold: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Confidence threshold (0-100) below which segments are flagged as garbage",
    )
    source_language: str | None = Field(
        default=None,
        description="Source language hint for transcription (ISO 639-1 code or 'auto')",
    )
    translate_to_english: bool | None = Field(
        default=None,
        description="Whether to translate non-English audio to English",
    )
    llm_output_language: str | None = Field(
        default=None,
        description="Language for LLM-generated summaries and analysis (ISO 639-1 code)",
    )
    # VAD settings
    vad_threshold: float | None = Field(
        default=None, ge=0.1, le=0.95, description="Speech detection sensitivity"
    )
    vad_min_silence_ms: int | None = Field(
        default=None, ge=100, le=5000, description="Min silence (ms) to split segments"
    )
    vad_min_speech_ms: int | None = Field(
        default=None, ge=50, le=5000, description="Min speech (ms) to keep a segment"
    )
    vad_speech_pad_ms: int | None = Field(
        default=None, ge=0, le=2000, description="Padding (ms) around speech"
    )
    # Accuracy settings
    hallucination_silence_threshold: float | None = Field(
        default=None, ge=0.5, le=10.0, description="Skip hallucinated text during silence >= Ns"
    )
    repetition_penalty: float | None = Field(
        default=None, ge=1.0, le=2.0, description="Penalize repetitive output"
    )
    # Diarization control
    diarization_source: str | None = Field(
        default=None,
        description="Diarization source: 'provider', 'local', 'pyannote', or 'off'",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "min_speakers": 2,
                "max_speakers": 10,
                "speaker_prompt_behavior": "use_custom",
                "garbage_cleanup_enabled": True,
                "garbage_cleanup_threshold": 40,
                "source_language": "es",
                "translate_to_english": False,
                "llm_output_language": "es",
                "vad_threshold": 0.3,
                "repetition_penalty": 1.2,
                "diarization_source": "provider",
            }
        }


class TranscriptionSystemDefaults(BaseModel):
    """
    Response schema for system-level transcription defaults.

    Contains the system-wide default values from environment configuration.
    """

    min_speakers: int = Field(
        description="System default minimum speakers (from MIN_SPEAKERS env var)"
    )
    max_speakers: int = Field(
        description="System default maximum speakers (from MAX_SPEAKERS env var)"
    )
    garbage_cleanup_enabled: bool = Field(
        default=DEFAULT_GARBAGE_CLEANUP_ENABLED,
        description="System default for garbage cleanup enabled",
    )
    garbage_cleanup_threshold: int = Field(
        default=DEFAULT_GARBAGE_CLEANUP_THRESHOLD,
        description="System default garbage cleanup threshold",
    )
    valid_speaker_prompt_behaviors: list[str] = Field(
        default=list(VALID_SPEAKER_PROMPT_BEHAVIORS),
        description="List of valid speaker prompt behavior options",
    )
    available_source_languages: dict[str, str] = Field(
        description="Available source languages for transcription (code -> name)",
    )
    available_llm_output_languages: dict[str, str] = Field(
        description="Available languages for LLM output (code -> name)",
    )
    common_languages: list[str] = Field(
        description="List of common language codes for UI grouping",
    )
    # VAD defaults
    vad_threshold: float = Field(description="System default VAD threshold")
    vad_min_silence_ms: int = Field(description="System default min silence (ms)")
    vad_min_speech_ms: int = Field(description="System default min speech (ms)")
    vad_speech_pad_ms: int = Field(description="System default speech padding (ms)")
    # Accuracy defaults
    hallucination_silence_threshold: float | None = Field(
        description="System default hallucination threshold (null=disabled)"
    )
    repetition_penalty: float = Field(description="System default repetition penalty")
    # Diarization default
    diarization_source_default: str = Field(
        default=DEFAULT_DIARIZATION_SOURCE,
        description="System default diarization source",
    )
    valid_diarization_sources: list[str] = Field(
        default=list(VALID_DIARIZATION_SOURCES),
        description="Valid diarization source options",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "min_speakers": 1,
                "max_speakers": 20,
                "garbage_cleanup_enabled": True,
                "garbage_cleanup_threshold": 50,
                "valid_speaker_prompt_behaviors": [
                    "always_prompt",
                    "use_defaults",
                    "use_custom",
                ],
                "available_source_languages": {"auto": "Auto-detect", "en": "English"},
                "available_llm_output_languages": {"en": "English", "es": "Spanish"},
                "common_languages": ["auto", "en", "es", "fr", "de"],
            }
        }
