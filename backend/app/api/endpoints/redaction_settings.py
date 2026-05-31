"""Content-redaction settings API.

Two routers:
- ``user_router`` (``/user-settings/redaction``): per-user preferences (self-serve,
  on by default — no admin activation needed). Mirrors the transcription-settings pattern.
- ``admin_router`` (``/admin/redaction-policy``): admin governance — the enforcement
  floor that forces specific categories / censored exports for ALL users.

No ``.env`` vars: defaults are coded constants; per-user prefs live in ``UserSetting``;
the admin floor lives in ``SystemSettings`` (``redaction.force_*`` keys).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.core import constants as C  # noqa: N812
from app.db.base import get_db
from app.schemas.redaction_settings import RedactionPolicy
from app.schemas.redaction_settings import RedactionPolicyUpdate
from app.schemas.redaction_settings import RedactionSettings
from app.schemas.redaction_settings import RedactionSettingsUpdate
from app.schemas.redaction_settings import RedactionSystemDefaults
from app.services.redaction.config import _load_admin_policy
from app.services.system_settings_service import get_setting
from app.services.system_settings_service import set_setting

logger = logging.getLogger(__name__)

user_router = APIRouter()
admin_router = APIRouter()

# --- per-user setting keys (UserSetting) -------------------------------------
_USER_KEY = {
    "enabled": "redaction_enabled",
    "detectors": "redaction_detectors",
    "categories": "redaction_categories",
    "pii_entities": "redaction_pii_entities",
    "style": "redaction_style",
    "custom_words": "redaction_custom_words",
    "allowlist": "redaction_allowlist",
    "toxicity_threshold": "redaction_toxicity_threshold",
    "redact_before_llm": "redaction_redact_before_llm",
    "default_export_redacted": "redaction_default_export_redacted",
}
_LIST_FIELDS = {"detectors", "categories", "pii_entities", "custom_words", "allowlist"}
_BOOL_FIELDS = {"enabled", "redact_before_llm", "default_export_redacted"}
_FLOAT_FIELDS = {"toxicity_threshold"}


def _store_value(field: str, value: Any) -> str:
    if field in _LIST_FIELDS:
        return json.dumps(value if isinstance(value, list) else [])
    if field in _BOOL_FIELDS:
        return "true" if value else "false"
    return str(value)


def _parse_value(field: str, raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    if field in _LIST_FIELDS:
        try:
            val = json.loads(raw)
            return [str(x) for x in val] if isinstance(val, list) else default
        except (ValueError, TypeError):
            return [p.strip() for p in raw.split(",") if p.strip()]
    if field in _BOOL_FIELDS:
        return raw.lower() in ("true", "1", "yes", "on")
    if field in _FLOAT_FIELDS:
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default
    return raw


_DEFAULTS = {
    "enabled": C.DEFAULT_REDACTION_ENABLED,
    "detectors": list(C.DEFAULT_REDACTION_DETECTORS),
    "categories": list(C.DEFAULT_REDACTION_CATEGORIES),
    "pii_entities": list(C.DEFAULT_REDACTION_PII_ENTITIES),
    "style": C.DEFAULT_REDACTION_STYLE,
    "custom_words": [],
    "allowlist": [],
    "toxicity_threshold": C.DEFAULT_REDACTION_TOXICITY_THRESHOLD,
    "redact_before_llm": C.DEFAULT_REDACTION_REDACT_BEFORE_LLM,
    "default_export_redacted": C.DEFAULT_REDACTION_DEFAULT_EXPORT_REDACTED,
}


# =============================================================================
# Per-user preferences
# =============================================================================
def _read_user_settings(db: Session, user_id: int) -> RedactionSettings:
    """Load a user's redaction preferences (coded defaults for unset fields).

    Plain helper so route handlers don't call each other (FastAPI route functions
    return Any once decorated, and calling them internally is an anti-pattern).
    """
    rows = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(list(_USER_KEY.values())),
        )
        .all()
    )
    stored = {str(r.setting_key): str(r.setting_value) for r in rows}
    values = {
        field: _parse_value(field, stored.get(db_key), _DEFAULTS[field])
        for field, db_key in _USER_KEY.items()
    }
    return RedactionSettings(**values)


@user_router.get("/redaction", response_model=RedactionSettings)
def get_redaction_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> RedactionSettings:
    """Return the user's redaction preferences (coded defaults for unset fields)."""
    return _read_user_settings(db, int(current_user.id))


@user_router.put("/redaction", response_model=RedactionSettings)
def update_redaction_settings(
    body: RedactionSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> RedactionSettings:
    """Update the user's redaction preferences (only provided fields)."""
    from app.api.endpoints.user_settings import _upsert_user_setting

    updates = body.model_dump(exclude_none=True)
    for field, value in updates.items():
        _upsert_user_setting(db, int(current_user.id), _USER_KEY[field], _store_value(field, value))
    db.commit()
    return _read_user_settings(db, int(current_user.id))


@user_router.delete("/redaction")
def reset_redaction_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Reset the user's redaction preferences to defaults."""
    db.query(models.UserSetting).filter(
        models.UserSetting.user_id == current_user.id,
        models.UserSetting.setting_key.in_(list(_USER_KEY.values())),
    ).delete(synchronize_session=False)
    db.commit()
    return {"message": "Redaction settings reset to defaults"}


@user_router.get("/redaction/defaults", response_model=RedactionSystemDefaults)
def get_redaction_defaults(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> RedactionSystemDefaults:
    """Return option lists + the admin-forced/locked set so the UI can lock controls."""
    admin = _load_admin_policy(db)
    return RedactionSystemDefaults(
        locked_categories=sorted(admin["forced_categories"]),
        export_locked=bool(admin["force_export_redacted"]),
        redact_before_llm_locked=bool(admin["force_redact_before_llm"]),
    )


# =============================================================================
# Admin governance policy
# =============================================================================
_POLICY_KEY = {
    "force_pii": "redaction.force_pii",
    "force_pii_entities": "redaction.force_pii_entities",
    "force_toxicity": "redaction.force_toxicity",
    "force_toxicity_threshold": "redaction.force_toxicity_threshold",
    "force_profanity": "redaction.force_profanity",
    "force_custom_words": "redaction.force_custom_words",
    "force_export_redacted": "redaction.force_export_redacted",
    "force_redact_before_llm": "redaction.force_redact_before_llm",
    "pii_use_gliner": "redaction.pii_use_gliner",
}
_POLICY_LIST = {"force_pii_entities", "force_custom_words"}
_POLICY_BOOL = {
    "force_pii",
    "force_toxicity",
    "force_profanity",
    "force_export_redacted",
    "force_redact_before_llm",
    "pii_use_gliner",
}
_POLICY_FLOAT = {"force_toxicity_threshold"}
_POLICY_DESCRIPTIONS = {
    "force_pii": "Force PII redaction for all users",
    "force_pii_entities": "PII entity types mandated for all users",
    "force_toxicity": "Force toxicity redaction/flagging for all users",
    "force_toxicity_threshold": "Mandated toxicity threshold ceiling",
    "force_profanity": "Force profanity redaction for all users",
    "force_custom_words": "Org-wide mandatory custom redaction words",
    "force_export_redacted": "Mandate censored exports for all users",
    "force_redact_before_llm": "Mandate masked text to external LLM providers",
    "pii_use_gliner": "Use GLiNER for enhanced (slower) name detection",
}


def _read_policy(db: Session) -> RedactionPolicy:
    def val(field: str) -> Any:
        raw = get_setting(db, _POLICY_KEY[field])
        if field in _POLICY_LIST:
            return _parse_value("detectors", raw, [])  # reuse list parser
        if field in _POLICY_BOOL:
            return raw is not None and raw.lower() in ("true", "1", "yes", "on")
        if field in _POLICY_FLOAT:
            try:
                return float(raw) if raw is not None else C.DEFAULT_REDACTION_TOXICITY_THRESHOLD
            except (ValueError, TypeError):
                return C.DEFAULT_REDACTION_TOXICITY_THRESHOLD
        return raw

    return RedactionPolicy(**{field: val(field) for field in _POLICY_KEY})


@admin_router.get("", response_model=RedactionPolicy)
def get_redaction_policy(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> RedactionPolicy:
    """Return the admin redaction governance policy."""
    return _read_policy(db)


@admin_router.post("/update", response_model=RedactionPolicy)
def update_redaction_policy(
    body: RedactionPolicyUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> RedactionPolicy:
    """Update the admin governance policy (only provided fields)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    for field, value in updates.items():
        store = (
            json.dumps(value)
            if field in _POLICY_LIST
            else (
                "true"
                if (field in _POLICY_BOOL and value)
                else "false"
                if field in _POLICY_BOOL
                else str(value)
            )
        )
        set_setting(db, _POLICY_KEY[field], store, _POLICY_DESCRIPTIONS[field])
        logger.info("Admin %s set %s = %r", current_user.email, _POLICY_KEY[field], value)
    return _read_policy(db)


@admin_router.post("/reindex")
def trigger_redaction_reindex(
    only_stale: bool = True,
    current_user: models.User = Depends(get_current_admin_user),
) -> dict:
    """Backfill / re-index redaction spans across completed files (model upgrade / rollout)."""
    from app.tasks.redaction_task import redaction_reindex_all_task

    redaction_reindex_all_task.delay(only_stale=only_stale)
    return {"status": "dispatched", "only_stale": only_stale}
