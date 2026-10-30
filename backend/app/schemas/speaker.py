"""Speaker, speaker-profile and speaker-provenance wire schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator

from app.schemas.base import UUIDBaseSchema


class SpeakerBase(BaseModel):
    name: str
    display_name: str | None = None
    suggested_name: str | None = None
    verified: bool = False


class SpeakerUpdate(BaseModel):
    # Server-side enforcement of the speaker-label length cap (the frontend also
    # validates display_name <= 100 chars; the backend is the system of record).
    name: str | None = Field(default=None, max_length=100)
    display_name: str | None = Field(default=None, max_length=100)
    suggested_name: str | None = Field(default=None, max_length=100)
    verified: bool | None = None
    embedding_vector: list[float] | None = None
    profile_action: str | None = None  # 'update_profile' or 'create_new_profile'


class SpeakerMetadataHint(BaseModel):
    """A speaker name inferred from the file's title/description/author (never auto-applied)."""

    name: str
    role: str | None = None
    confidence: float | None = None
    source: str | None = None


# Keys in ``speaker.attribute_confidence`` that are NOT confidence numbers. Speaker
# identification writes them into the same JSONB bag as the voice-attribute scores;
# each is exposed as its own response field instead.
_ATTRIBUTE_CONFIDENCE_NON_NUMERIC_KEYS = frozenset(
    {"metadata_hints", "alignment", "alignment_hint"}
)


def split_attribute_confidence(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Split the stored ``attribute_confidence`` bag into its response fields.

    Every surface that serves a speaker goes through this, so the speaker list (a
    hand-built dict) and the schema-validated surfaces cannot drift apart (#1026).

    Returns:
        ``attribute_confidence`` (numeric scores only; None when nothing was stored),
        ``metadata_hints`` (list), ``gender_alignment`` and ``gender_alignment_hint``.
    """
    bag = dict(raw or {})
    alignment = bag.get("alignment")
    alignment_hint = bag.get("alignment_hint")
    hints = bag.get("metadata_hints")
    numeric = {
        k: v
        for k, v in bag.items()
        if k not in _ATTRIBUTE_CONFIDENCE_NON_NUMERIC_KEYS
        and isinstance(v, (int, float))
        and not isinstance(v, bool)
    }
    return {
        "attribute_confidence": numeric if raw else None,
        "metadata_hints": [h for h in hints if isinstance(h, dict)]
        if isinstance(hints, list)
        else [],
        "gender_alignment": alignment if isinstance(alignment, str) else None,
        "gender_alignment_hint": alignment_hint if isinstance(alignment_hint, str) else None,
    }


class SpeakerIdentityActor(BaseModel):
    """The person who made or confirmed a speaker's identity."""

    uuid: UUID | None = None  # None for an erased account
    name: str | None = None  # full name, else the account's email; None for an erased account
    erased: bool = False  # the account is being erased: no uuid, name or email is given


class SpeakerIdentity(BaseModel):
    """How the speaker's CURRENT identity came to be (read-only; issue #1280).

    ``by`` is present only for the file's owner, an administrator of the file's organization
    and a platform administrator whose reach covers the file (any administrator in
    single-tenant mode; in multi-tenant mode only under a support-access grant); every other
    viewer gets ``source`` and ``at``. ``null`` for
    the whole object means unknown (written before the record existed, or never named) or
    withheld because a voiceprint made it and the file's voiceprints are off. Only the current
    state is kept; the full change history is tracked in issue #1336.
    """

    source: str  # "human" | "voiceprint" | "voiceprint_link"
    at: datetime | None = None
    by: SpeakerIdentityActor | None = None
    detail: dict[str, Any] | None = None


class SpeakerSuggestion(BaseModel):
    """Where the speaker's CURRENT recommendation in ``suggested_name`` came from (read-only)."""

    source: str | None = None  # "voice_match" | "profile_match" | "llm_analysis" | ...
    confidence: float | None = None
    detail: dict[str, Any] | None = None


class Speaker(SpeakerBase, UUIDBaseSchema):
    """Speaker with UUID as public identifier"""

    user_id: UUID
    media_file_id: UUID
    profile_id: UUID | None = None
    confidence: float | None = None
    # Where ``suggested_name`` came from (``llm_analysis``, ``voice_match``, ``profile_match``
    # ...). Only a voiceprint source can become the speaker's name; the SPA reads this to
    # tell a guess it must offer for verification from a voice match.
    suggestion_source: str | None = None
    created_at: datetime

    # Read-only provenance, attached by ``services.speaker_provenance_view`` after the privacy
    # redaction. None = unknown or withheld.
    identity: SpeakerIdentity | None = None
    suggestion: SpeakerSuggestion | None = None

    # Computed status fields from SpeakerStatusService
    computed_status: str | None = None  # "verified", "suggested", "unverified"
    status_text: str | None = None  # Human-readable status text
    status_color: str | None = None  # CSS color for status display
    resolved_display_name: str | None = None  # Best available display name

    # Linked SpeakerProfile info (saves the frontend a separate profile fetch).
    # None when the speaker is not linked to a profile.
    profile_name: str | None = None  # Name of the linked SpeakerProfile
    profile_status: str | None = None  # "linked" when a profile is attached, else None

    # AI-predicted voice attributes
    predicted_gender: str | None = None
    predicted_age_range: str | None = None
    attribute_confidence: dict[str, float] | None = None  # numeric scores only
    attributes_predicted_at: datetime | None = None

    # Split out of the stored attribute_confidence bag by split_attribute_confidence().
    metadata_hints: list[SpeakerMetadataHint] = Field(default_factory=list)
    gender_alignment: str | None = None  # "match" | "mismatch"
    gender_alignment_hint: str | None = None  # the hint name the alignment was judged against

    @model_validator(mode="before")
    @classmethod
    def prepare_uuid_response(cls, data: Any) -> Any:
        """Run the base ORM->dict mapping, then split ``attribute_confidence``.

        Overrides the base validator by name so it runs AFTER the ORM row has become
        a dict; a separately named subclass validator would run first and see the row.
        Fields already present in a dict input (a re-validated ``model_dump()``) win
        over the split, which would otherwise reset them from the numeric-only map.
        """
        data = cls._orm_row_to_dict(data)
        if isinstance(data, dict) and "attribute_confidence" in data:
            split = split_attribute_confidence(data["attribute_confidence"])
            data = {**split, **data, "attribute_confidence": split["attribute_confidence"]}
        return data


# Speaker Profile schemas
class SpeakerProfileBase(BaseModel):
    name: str
    description: str | None = None


class SpeakerProfile(SpeakerProfileBase, UUIDBaseSchema):
    """Speaker profile with UUID as public identifier"""

    user_id: UUID
    created_at: datetime
    updated_at: datetime

    # AI-predicted attributes (consensus from linked speakers)
    predicted_gender: str | None = None
    predicted_age_range: str | None = None
