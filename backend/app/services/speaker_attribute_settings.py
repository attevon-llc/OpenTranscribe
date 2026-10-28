"""Effective speaker-attribute toggles for one user (issues #1200, #1148).

One resolver for every reader, so "attribute detection", "gender detection" and the
sidecar gender write cannot disagree about what the user chose.

Resolution per flag: user ``UserSetting`` row > system ``speaker_attribute.*`` setting >
default. Detection's default additionally honours ``SPEAKER_ATTRIBUTE_DETECTION_ENABLED``.
The ``speaker_attribute.*`` system keys are DEFAULTS a user may override, never a ceiling.

The one exception is gender (issue #1280): a ceiling set by a higher layer of the privacy
policy (platform, instance, organization; ``services/privacy/policy.py``) is ANDed into
``gender_detection_enabled`` after the user > system > default resolution, so a user who
switched gender on still gets no inference when their organization forbids it. The policy is
scoped by the organization of the FILE being processed, never by caller state; use
:func:`resolve_speaker_attribute_flags_for_file` wherever a file is in hand.

The native diarizer's own ``DIAR_NATIVE_GENDER`` env switch is a separate, additional hard
off applied where the sidecar runs (``transcription/diarizer_native.py``); it is not
consulted here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from dataclasses import replace

from sqlalchemy.orm import Session

from app.services.privacy.policy import Decision
from app.services.privacy.policy import gender_decision
from app.services.privacy.policy import gender_decision_for_file

logger = logging.getLogger(__name__)

_TRUTHY = ("true", "1", "yes", "on")


@dataclass(frozen=True)
class SpeakerAttributeFlags:
    """What the owner of a file has chosen for speaker attributes."""

    detection_enabled: bool
    gender_detection_enabled: bool
    show_on_cards: bool
    #: The layer above the user that forbids gender inference (``platform``, ``instance`` or
    #: ``organization``), or None. A user's own switch is not a lock: they can undo it.
    gender_locked_by: str | None = None

    @property
    def gender_prediction_allowed(self) -> bool:
        """True only when attribute detection AND gender detection are both on."""
        return self.detection_enabled and self.gender_detection_enabled


def _above_user(decision: Decision) -> str | None:
    """The layer that locks gender, ignoring the user's own (undoable) switch."""
    if decision.enabled or decision.locked_by == "user":
        return None
    return decision.locked_by


def resolve_speaker_attribute_flags(
    db: Session, user_id: int, *, organization_id: int | None = None
) -> SpeakerAttributeFlags:
    """Resolve the user's speaker-attribute toggles (user > system > default, then policy).

    Args:
        db: Session, borrowed read-only.
        user_id: The person whose switches apply.
        organization_id: The tenant of the record being processed, taken from its row. None
            means personal scope, which never reads an organization policy row, so a caller
            that has a file must pass the file's organization or use
            :func:`resolve_speaker_attribute_flags_for_file`.

    Returns:
        The flags. ``gender_detection_enabled`` is False when the privacy policy forbids
        inference for the scope, whatever the user chose; ``gender_locked_by`` says which
        layer. A policy that cannot be read denies (fail closed).
    """
    from app.models.prompt import UserSetting
    from app.services.system_settings_service import get_settings_map

    env_enabled = os.environ.get("SPEAKER_ATTRIBUTE_DETECTION_ENABLED", "true").lower() == "true"
    names = {
        "detection_enabled": ("speaker_attribute_detection_enabled", env_enabled),
        "gender_detection_enabled": ("speaker_attribute_gender_detection_enabled", True),
        "show_on_cards": ("speaker_attribute_show_on_cards", True),
    }

    system = get_settings_map(db, [f"speaker_attribute.{name}" for name in names])
    user_rows = (
        db.query(UserSetting.setting_key, UserSetting.setting_value)
        .filter(
            UserSetting.user_id == user_id,
            UserSetting.setting_key.in_([key for key, _ in names.values()]),
        )
        .all()
    )
    user = {key: value for key, value in user_rows}

    resolved: dict[str, bool] = {}
    for name, (user_key, default) in names.items():
        if user_key in user:
            resolved[name] = str(user[user_key]).lower() == "true"
            continue
        raw = system.get(f"speaker_attribute.{name}")
        resolved[name] = raw.lower() in _TRUTHY if raw is not None else default

    decision = gender_decision(db, user_id=user_id, organization_id=organization_id)
    resolved["gender_detection_enabled"] = resolved["gender_detection_enabled"] and decision.enabled
    return SpeakerAttributeFlags(**resolved, gender_locked_by=_above_user(decision))


def resolve_speaker_attribute_flags_for_file(
    db: Session, media_file_id: int, *, user_id: int | None = None
) -> SpeakerAttributeFlags:
    """:func:`resolve_speaker_attribute_flags` for the owner and organization of a file.

    The organization comes from the file's row. Gender is additionally checked against the
    file owner's own scope, so a caller passing a different ``user_id`` (an admin reprocess)
    cannot widen what the owner's organization allows. A file that cannot be read denies
    gender (fail closed) and reports detection as it resolves for ``user_id``.

    Args:
        db: Session, borrowed read-only.
        media_file_id: The file being processed.
        user_id: Whose toggles apply; defaults to the file's owner.
    """
    from app.models.media import MediaFile

    row = (
        db.query(MediaFile.user_id, MediaFile.organization_id)
        .filter(MediaFile.id == media_file_id)
        .first()
    )
    if row is None:
        logger.warning("speaker attributes: file %s not found; denying gender", media_file_id)
        flags = resolve_speaker_attribute_flags(db, user_id or 0)
        return replace(flags, gender_detection_enabled=False, gender_locked_by="instance")

    owner = int(row[0])
    flags = resolve_speaker_attribute_flags(
        db,
        owner if user_id is None else user_id,
        organization_id=int(row[1]) if row[1] is not None else None,
    )
    owner_decision = gender_decision_for_file(db, media_file_id)
    if flags.gender_detection_enabled and not owner_decision.enabled:
        return replace(
            flags,
            gender_detection_enabled=False,
            gender_locked_by=_above_user(owner_decision),
        )
    return flags
