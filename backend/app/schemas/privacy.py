"""Wire contract of the privacy controls (issues #1280, #1279).

Voiceprint matching and inferred gender are governed by one restrict-only policy
(``app.services.privacy.policy``): platform, instance, organization and user are ANDed and any
layer that is off turns the feature off. These schemas are the request and response shapes of

* ``GET /privacy/effective`` and ``GET|PUT /privacy/me`` (any signed-in user),
* ``GET|PUT /admin/privacy-policy`` (super_admin, the instance layer),
* ``GET|PATCH /org-admin/policy`` (an organization administrator, capability ``org.defaults``),
* the three purge endpoints, which delete data a switch only hides.

``locked_by`` names the highest layer that turned a feature off, so a UI can say who to ask. The
layer names are the policy module's (``Layer``); ``tests/unit/test_privacy_schemas.py`` pins the
two vocabularies together.

Error bodies that carry a ``code`` (HTTP 409 and 403) have ``detail`` shaped
``{"code": "<code>", ...}``:

* ``policy_locked`` (409, ``PUT /privacy/me``): ``locked_by`` names the layer above the user.
* ``policy_still_enabled`` (409, any purge): ``targets`` lists what is still switched on at the
  purging layer. A purge is refused until the switch is off, or the data would grow straight back.
* ``purge_scope_not_allowed`` (403, ``POST /admin/privacy-policy/purge`` with ``scope="all"``):
  refused in a multi-tenant deployment, where a platform administrator must not destroy tenant data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.core import constants

#: Every layer a decision can be locked by. ``consent`` only ever appears for voiceprint creation.
PrivacyLayer = Literal["platform", "instance", "organization", "user", "consent"]
#: The layers ABOVE a user's own switch, i.e. the ones that can lock it.
UserLockLayer = Literal["platform", "instance", "organization"]
#: What a purge deletes. ``voiceprints`` is every stored embedding and what is derived from it;
#: ``inferred_gender`` is gender no human confirmed. Named profiles, avatars and speaker
#: assignments a person made are kept by both.
PurgeTarget = Literal["voiceprints", "inferred_gender"]

#: The redaction categories an organization floor may force. The vocabulary of the instance floor
#: (``redaction.force_pii`` / ``force_toxicity`` / ``force_profanity``); custom words have their own
#: field and imply the ``custom`` category when the floor is resolved.
ORG_FLOOR_CATEGORIES: tuple[str, ...] = ("pii", "toxicity", "profanity")
ORG_FLOOR_MAX_CUSTOM_WORDS = 200
ORG_FLOOR_MAX_WORD_LENGTH = 100


class PrivacyDecision(BaseModel):
    """Whether one feature is allowed for a scope, and the highest layer that forbade it."""

    enabled: bool
    locked_by: PrivacyLayer | None = Field(
        default=None, description="None exactly when enabled is true."
    )


class EffectivePrivacy(BaseModel):
    """``GET /privacy/effective``: the policy as it applies to one file or to the caller."""

    organization_uuid: UUID | None = Field(
        default=None, description="The tenant the answer is for; None for personal scope."
    )
    voiceprint_matching: PrivacyDecision
    gender_inference: PrivacyDecision


class InstancePrivacyPolicy(BaseModel):
    """The instance layer: ``SystemSettings`` ``privacy.*``. Both default on."""

    voiceprint_matching_enabled: bool
    gender_inference_enabled: bool


class InstancePrivacyPolicyUpdate(BaseModel):
    """``PUT /admin/privacy-policy``: partial. At least one field must be given."""

    voiceprint_matching_enabled: bool | None = None
    gender_inference_enabled: bool | None = None

    @field_validator("voiceprint_matching_enabled", "gender_inference_enabled", mode="before")
    @classmethod
    def _no_coercion(cls, value: object) -> object:
        # "no" or 0 must not silently read as False on a switch that hides biometric data.
        if value is not None and not isinstance(value, bool):
            raise ValueError("must be a boolean")
        return value

    @model_validator(mode="after")
    def _something_to_change(self) -> InstancePrivacyPolicyUpdate:
        if not self.model_fields_set:
            raise ValueError("send at least one field")
        return self


class UserPrivacyPolicy(BaseModel):
    """``GET|PUT /privacy/me``: the caller's own voiceprint switch."""

    voiceprint_matching_enabled: bool = Field(
        description="The caller's own choice (default true), independent of the layers above."
    )
    locked_by: UserLockLayer | None = Field(
        default=None,
        description=(
            "The layer above the caller that has voiceprint matching off for the active scope, "
            "if any. While set the switch cannot be changed (PUT answers 409 policy_locked)."
        ),
    )


class UserPrivacyPolicyUpdate(BaseModel):
    """``PUT /privacy/me``."""

    voiceprint_matching_enabled: bool

    @field_validator("voiceprint_matching_enabled", mode="before")
    @classmethod
    def _no_coercion(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("must be a boolean")
        return value


class RedactionFloorOut(BaseModel):
    """An organization's redaction floor. It can only ADD to the instance floor."""

    forced_categories: list[str] = Field(default_factory=list)
    forced_pii_entities: list[str] = Field(default_factory=list)
    forced_custom_words: list[str] = Field(default_factory=list)
    force_toxicity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    force_export_redacted: bool = False
    force_redact_before_llm: bool = False


class RedactionFloorIn(BaseModel):
    """A partial change to the organization's floor: only the fields sent are changed.

    ``force_toxicity_threshold`` is the one nullable field; an explicit ``null`` clears it.
    Every other field rejects ``null``. Values are validated against the same vocabularies the
    instance floor uses, and ``forced_custom_words`` is capped (200 words of up to 100
    characters). A word is never copied into an audit event.
    """

    forced_categories: list[str] | None = None
    forced_pii_entities: list[str] | None = None
    forced_custom_words: list[str] | None = None
    force_toxicity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    force_export_redacted: bool | None = None
    force_redact_before_llm: bool | None = None

    @field_validator("forced_categories")
    @classmethod
    def _categories(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - set(ORG_FLOOR_CATEGORIES))
        if unknown:
            raise ValueError(f"unknown categories {unknown}; allowed: {list(ORG_FLOOR_CATEGORIES)}")
        return list(dict.fromkeys(value))

    @field_validator("forced_pii_entities")
    @classmethod
    def _entities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - set(constants.REDACTION_PII_ENTITIES))
        if unknown:
            raise ValueError(f"unknown PII entities {unknown}")
        return list(dict.fromkeys(value))

    @field_validator("forced_custom_words")
    @classmethod
    def _words(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        words: dict[str, str] = {}
        for raw in value:
            word = raw.strip()
            if not word:
                raise ValueError("custom words must not be blank")
            if len(word) > ORG_FLOOR_MAX_WORD_LENGTH:
                raise ValueError(f"a custom word is longer than {ORG_FLOOR_MAX_WORD_LENGTH}")
            words.setdefault(word.casefold(), word)
        if len(words) > ORG_FLOOR_MAX_CUSTOM_WORDS:
            raise ValueError(f"at most {ORG_FLOOR_MAX_CUSTOM_WORDS} custom words")
        return list(words.values())

    @field_validator(
        "forced_categories",
        "forced_pii_entities",
        "forced_custom_words",
        "force_export_redacted",
        "force_redact_before_llm",
        mode="before",
    )
    @classmethod
    def _present_means_not_null(cls, value: object) -> object:
        # Absent fields are skipped by pydantic; an explicit null here would be ambiguous
        # (clear? ignore?), so only the threshold may be nulled.
        if value is None:
            raise ValueError("must not be null")
        return value


class OrgEffectivePrivacy(BaseModel):
    """The organization-scope answer: platform, instance and organization layers, no user."""

    voiceprint_matching: PrivacyDecision
    gender_inference: PrivacyDecision


class OrgPolicy(BaseModel):
    """``GET|PATCH /org-admin/policy``: the caller's own organization, never another."""

    voiceprint_matching_enabled: bool = Field(description="The organization's own switch.")
    gender_inference_enabled: bool = Field(description="The organization's own switch.")
    instance: InstancePrivacyPolicy = Field(
        description="The deployment's switches. A switch the operator turned off cannot be turned on here."
    )
    effective: OrgEffectivePrivacy = Field(
        description=(
            "What applies to the organization once the layers above it are counted. "
            "locked_by names the platform or instance layer when that is what is off."
        )
    )
    redaction_floor: RedactionFloorOut | None = Field(
        default=None,
        description="Null when the redaction.policy capability is off for this request.",
    )
    updated_at: datetime | None = Field(
        default=None, description="Null until an administrator has saved a policy."
    )


class OrgPolicyUpdate(BaseModel):
    """``PATCH /org-admin/policy``: partial. At least one field must be given.

    ``redaction_floor`` answers 404 while the ``redaction.policy`` capability is off.
    """

    voiceprint_matching_enabled: bool | None = None
    gender_inference_enabled: bool | None = None
    redaction_floor: RedactionFloorIn | None = None

    @field_validator("voiceprint_matching_enabled", "gender_inference_enabled", mode="before")
    @classmethod
    def _no_coercion(cls, value: object) -> object:
        if value is not None and not isinstance(value, bool):
            raise ValueError("must be a boolean")
        return value

    @model_validator(mode="after")
    def _something_to_change(self) -> OrgPolicyUpdate:
        floor = self.redaction_floor
        if not self.model_fields_set - {"redaction_floor"} and (
            floor is None or not floor.model_fields_set
        ):
            raise ValueError("send at least one field")
        return self


class PurgeRequest(BaseModel):
    """Delete what a switch only hides. Irreversible.

    ``confirm`` must be the literal ``PURGE``: the UI makes the person type it, and the API
    refuses anything else (422), so a stray request cannot destroy data.
    """

    targets: list[PurgeTarget] = Field(min_length=1)
    confirm: Literal["PURGE"]

    @field_validator("targets")
    @classmethod
    def _unique(cls, value: list[PurgeTarget]) -> list[PurgeTarget]:
        return list(dict.fromkeys(value))


class AdminPurgeRequest(PurgeRequest):
    """``POST /admin/privacy-policy/purge``.

    ``personal`` deletes the personal-workspace data of every user; ``all`` deletes
    everything, tenants included, and is refused (403) in a multi-tenant deployment.
    """

    scope: Literal["personal", "all"]


class PurgeAccepted(BaseModel):
    """202 body of every purge endpoint. Poll ``GET /privacy/purge/{task_id}``."""

    task_id: str


class PurgeStatus(BaseModel):
    """``GET /privacy/purge/{task_id}``: readable only by the person who started the purge."""

    task_id: str
    status: Literal["pending", "in_progress", "completed", "failed", "skipped"]
    counts: dict[str, int] | None = Field(
        default=None,
        description=(
            "What was deleted, once the purge has finished. Counts only, never a name. A purge "
            "runs twice (the second pass about a minute later, to catch a write that raced the "
            "switch), so status stays in_progress until the second pass ends."
        ),
    )
