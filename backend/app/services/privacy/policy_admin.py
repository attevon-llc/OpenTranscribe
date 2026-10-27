"""Reading and editing the privacy layers (issues #1280, #1279).

The read side of the policy is ``policy.py``; this module is the write side the routes in
``api/endpoints/privacy.py`` and ``org_admin_policy.py`` share, so the endpoints stay thin:

* the **instance** layer: ``SystemSettings`` ``privacy.*``,
* the **organization** layer: the ``organization_policy`` row, including the redaction floor,
* the **user** layer: ``UserSetting`` ``privacy_voiceprint_matching_enabled``.

Every write calls ``clear_write_backstop_cache`` so this process stops trusting a cached "allowed"
immediately; other processes catch up within ``PRIVACY_WRITE_BACKSTOP_TTL_SECONDS``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.auth.audit import AuditOutcome
from app.core import constants
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.models.system_settings import SystemSettings
from app.schemas.privacy import InstancePrivacyPolicy
from app.schemas.privacy import OrgEffectivePrivacy
from app.schemas.privacy import OrgPolicy
from app.schemas.privacy import OrgPolicyUpdate
from app.schemas.privacy import PrivacyDecision
from app.schemas.privacy import RedactionFloorOut
from app.schemas.privacy import UserLockLayer
from app.services.privacy.label_reindex import queue_label_reindex
from app.services.privacy.policy import clear_write_backstop_cache
from app.services.privacy.policy import resolve_privacy_policy
from app.services.privacy.purge import PurgeScope
from app.services.privacy.purge import audit_purge
from app.services.privacy.purge import targets_still_enabled
from app.services.privacy.purge import write_record
from app.services.system_settings_service import set_setting

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_TRUTHY = ("true", "1", "yes", "on")

_INSTANCE_DESCRIPTIONS = {
    constants.PRIVACY_VOICEPRINT_MATCHING_KEY: "Allow speaker voiceprints to be created and matched",
    constants.PRIVACY_GENDER_INFERENCE_KEY: "Allow speaker gender to be inferred from voice or names",
}

#: ``RedactionFloorIn`` field -> ``organization_policy`` column.
_FLOOR_COLUMNS = {
    "forced_categories": "floor_forced_categories",
    "forced_pii_entities": "floor_forced_pii_entities",
    "forced_custom_words": "floor_forced_custom_words",
    "force_toxicity_threshold": "floor_toxicity_threshold",
    "force_export_redacted": "floor_export_redacted",
    "force_redact_before_llm": "floor_redact_before_llm",
}


def _flag(raw: str | None, default: bool) -> bool:
    """Same reading as the policy module: absent = default, NULL or unparseable = off."""
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _instance_value(db: Session, key: str, default: bool) -> bool:
    row = db.execute(select(SystemSettings.value).where(SystemSettings.key == key)).first()
    return default if row is None else _flag(row[0], default)


def _decision(value: Any) -> PrivacyDecision:
    return PrivacyDecision(enabled=value.enabled, locked_by=value.locked_by)


# --- instance layer ----------------------------------------------------------------------


def read_instance_policy(db: Session) -> InstancePrivacyPolicy:
    """The deployment's two switches, read straight from the database (never cached)."""
    return InstancePrivacyPolicy(
        voiceprint_matching_enabled=_instance_value(
            db,
            constants.PRIVACY_VOICEPRINT_MATCHING_KEY,
            constants.DEFAULT_PRIVACY_VOICEPRINT_MATCHING_ENABLED,
        ),
        gender_inference_enabled=_instance_value(
            db,
            constants.PRIVACY_GENDER_INFERENCE_KEY,
            constants.DEFAULT_PRIVACY_GENDER_INFERENCE_ENABLED,
        ),
    )


def update_instance_policy(
    db: Session, *, voiceprint_matching: bool | None, gender_inference: bool | None
) -> list[dict[str, Any]]:
    """Write the switches that were given. Returns one ``{setting, from, to}`` per change.

    A switch set to its current value is not written and not reported.
    """
    before = read_instance_policy(db)
    wanted = (
        (
            constants.PRIVACY_VOICEPRINT_MATCHING_KEY,
            before.voiceprint_matching_enabled,
            voiceprint_matching,
        ),
        (constants.PRIVACY_GENDER_INFERENCE_KEY, before.gender_inference_enabled, gender_inference),
    )
    changes: list[dict[str, Any]] = []
    for key, current, new in wanted:
        if new is None or new == current:
            continue
        set_setting(db, key, new, _INSTANCE_DESCRIPTIONS[key])
        changes.append({"setting": key, "from": current, "to": new})
    clear_write_backstop_cache()
    if any(c["setting"] == constants.PRIVACY_VOICEPRINT_MATCHING_KEY for c in changes):
        queue_label_reindex(db)
    return changes


# --- user layer --------------------------------------------------------------------------


def read_user_switch(db: Session, user_id: int) -> bool:
    """The person's own voiceprint switch (default on; a stored NULL reads as off)."""
    row = db.execute(
        select(UserSetting.setting_value).where(
            UserSetting.user_id == user_id,
            UserSetting.setting_key == constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
        )
    ).first()
    return True if row is None else _flag(row[0], True)


def write_user_switch(db: Session, user_id: int, enabled: bool) -> None:
    """Store the person's own voiceprint switch and commit."""
    changed = read_user_switch(db, user_id) != enabled
    value = "true" if enabled else "false"
    row = db.execute(
        select(UserSetting).where(
            UserSetting.user_id == user_id,
            UserSetting.setting_key == constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            UserSetting(
                user_id=user_id,
                setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
                setting_value=value,
            )
        )
    else:
        row.setting_value = value
    db.commit()
    clear_write_backstop_cache()
    if changed:
        queue_label_reindex(db, user_id=user_id)


def user_lock_layer(db: Session, organization_id: int | None) -> UserLockLayer | None:
    """The layer above a user that has voiceprints off for a tenant, if any.

    Resolved with no user, so the user layer inherits; the highest layer that is off is named.
    A policy that could not be read denies as ``instance`` (see ``policy.resolve_privacy_policy``).
    """
    decision = resolve_privacy_policy(db, user_id=None, organization_id=organization_id).voiceprints
    if decision.enabled:
        return None
    # With no user in play ``locked_by`` can only be platform, instance or organization; the
    # error path denies as instance.
    if decision.locked_by == "platform":
        return "platform"
    if decision.locked_by == "organization":
        return "organization"
    return "instance"


# --- organization layer ------------------------------------------------------------------


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _floor_out(row: OrganizationPolicy | None) -> RedactionFloorOut:
    if row is None:
        return RedactionFloorOut()
    return RedactionFloorOut(
        forced_categories=_strings(row.floor_forced_categories),
        forced_pii_entities=_strings(row.floor_forced_pii_entities),
        forced_custom_words=_strings(row.floor_forced_custom_words),
        force_toxicity_threshold=row.floor_toxicity_threshold,
        force_export_redacted=bool(row.floor_export_redacted),
        force_redact_before_llm=bool(row.floor_redact_before_llm),
    )


def read_org_policy(db: Session, organization_id: int, *, include_floor: bool) -> OrgPolicy:
    """The organization's policy as served by ``GET /org-admin/policy``."""
    row = db.execute(
        select(OrganizationPolicy).where(OrganizationPolicy.organization_id == organization_id)
    ).scalar_one_or_none()
    effective = resolve_privacy_policy(db, user_id=None, organization_id=organization_id)
    return OrgPolicy(
        voiceprint_matching_enabled=True if row is None else bool(row.voiceprint_matching_enabled),
        gender_inference_enabled=True if row is None else bool(row.gender_inference_enabled),
        instance=read_instance_policy(db),
        effective=OrgEffectivePrivacy(
            voiceprint_matching=_decision(effective.voiceprints),
            gender_inference=_decision(effective.gender),
        ),
        redaction_floor=_floor_out(row) if include_floor else None,
        updated_at=None if row is None else row.updated_at,
    )


def apply_org_policy_update(
    db: Session, organization_id: int, actor_user_id: int, body: OrgPolicyUpdate
) -> list[str]:
    """Apply a PATCH to the organization's row and commit. Returns the NAMES of fields changed.

    Only names are returned because they go to the audit trail and a custom word can be sensitive.
    The row is created on first use and locked while it is edited, so two concurrent patches
    serialize instead of overwriting each other.
    """
    db.execute(
        pg_insert(OrganizationPolicy)
        .values(organization_id=organization_id)
        .on_conflict_do_nothing(index_elements=[OrganizationPolicy.organization_id])
    )
    row = db.execute(
        select(OrganizationPolicy)
        .where(OrganizationPolicy.organization_id == organization_id)
        .with_for_update()
    ).scalar_one()

    changed: list[str] = []

    def set_field(name: str, column: str, new: Any) -> None:
        if getattr(row, column) != new:
            setattr(row, column, new)
            changed.append(name)

    if body.voiceprint_matching_enabled is not None:
        set_field(
            "voiceprint_matching_enabled",
            "voiceprint_matching_enabled",
            body.voiceprint_matching_enabled,
        )
    if body.gender_inference_enabled is not None:
        set_field(
            "gender_inference_enabled", "gender_inference_enabled", body.gender_inference_enabled
        )
    if body.redaction_floor is not None:
        for field_name in body.redaction_floor.model_fields_set:
            set_field(
                f"redaction_floor.{field_name}",
                _FLOOR_COLUMNS[field_name],
                getattr(body.redaction_floor, field_name),
            )

    row.updated_by_user_id = actor_user_id
    db.commit()
    clear_write_backstop_cache()
    if "voiceprint_matching_enabled" in changed:
        queue_label_reindex(db, organization_id=organization_id)
    return sorted(changed)


# --- starting a purge --------------------------------------------------------------------


class PurgeStillEnabledError(Exception):
    """A purge was asked for while a target's switch is still on at the purging layer."""

    def __init__(self, targets: list[str]) -> None:
        super().__init__(f"still enabled: {targets}")
        self.targets = targets


class PurgeNotQueuedError(Exception):
    """The purge task could not be handed to the broker."""


def start_purge(
    db: Session,
    scope: PurgeScope,
    targets: list[str],
    *,
    actor_user_id: int,
    actor_username: str | None,
    source_ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Check, record and queue a purge. Returns the task id.

    The route has already verified authority and the typed confirmation. Here: refuse while a
    target is still on (:class:`PurgeStillEnabledError`, fail closed), create the task row, audit
    the request, and queue ``privacy.purge_voiceprints`` (:class:`PurgeNotQueuedError` if the
    broker refuses; the row is then marked failed and the audit event says so).
    """
    import uuid

    from app.tasks.privacy_purge_task import TASK_TYPE
    from app.tasks.privacy_purge_task import privacy_purge_task
    from app.utils.task_utils import TASK_STATUS_FAILED
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status

    blocked = targets_still_enabled(db, scope, targets)
    if blocked:
        raise PurgeStillEnabledError(blocked)

    task_id = str(uuid.uuid4())
    create_task_record(db, task_id, actor_user_id, None, TASK_TYPE)
    write_record(task_id, actor_user_id, {})

    def audit(phase: str, outcome: AuditOutcome = AuditOutcome.SUCCESS) -> None:
        audit_purge(
            scope=scope,
            targets=targets,
            phase=phase,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            outcome=outcome,
            source_ip=source_ip,
            user_agent=user_agent,
        )

    try:
        privacy_purge_task.apply_async(
            kwargs={
                "scope": scope.to_payload(),
                "targets": targets,
                "actor_user_id": actor_user_id,
                "phase": 1,
            },
            task_id=task_id,
        )
    except Exception as exc:
        logger.exception("privacy purge %s could not be queued", task_id)
        update_task_status(db, task_id, TASK_STATUS_FAILED, completed=True)
        audit("not_queued", AuditOutcome.FAILURE)
        raise PurgeNotQueuedError from exc
    audit("requested")
    return task_id
