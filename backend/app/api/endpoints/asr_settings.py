"""
API endpoints for user ASR (Automatic Speech Recognition) settings management.

Follows the same pattern as llm_settings.py for consistency. Pinning the local Whisper model
lives in ``asr_local_model`` (a different capability gate). The provider catalog/status and
the connection tests live in ``asr_provider_catalog`` and ``asr_connection_tests``; they are
spliced in around the configuration CRUD below so the route table keeps its order.
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.deps_context import get_current_context
from app.api.endpoints import asr_connection_tests
from app.api.endpoints import asr_provider_catalog
from app.api.endpoints.asr_config_state import _clear_active_asr_setting
from app.api.endpoints.asr_config_state import _clear_asr_shared_active_references
from app.api.endpoints.asr_config_state import _config_to_dict
from app.api.endpoints.asr_config_state import _get_active_asr_config_id
from app.api.endpoints.asr_config_state import _get_config_or_404
from app.api.endpoints.asr_config_state import _reset_diarizer_references
from app.api.endpoints.asr_config_state import _set_active_asr_configuration
from app.api.endpoints.asr_config_state import _validate_region
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.pipeline_settings import plan_issues_out
from app.db.base import get_db
from app.schemas.pipeline import ASRSwitchResult
from app.services import asr_provider_policy
from app.services.asr_switch_plan import SwitchOutcome
from app.services.asr_switch_plan import assess_asr_switch
from app.services.pipeline_plan_inputs import plan_for_user
from app.services.request_context import RequestContext
from app.utils.encryption import encrypt_api_key
from app.utils.encryption import test_encryption
from app.utils.lite_mode import refuse_on_lite
from app.utils.tenant_sharing import owner_in_tenant

logger = logging.getLogger(__name__)

# Registration order is the pre-split route table's order exactly. Routes are spliced rather
# than ``include_router``-ed because FastAPI refuses to nest a router whose path is empty
# (``POST ""``) under an empty prefix.
router = APIRouter()
router.routes.extend(asr_provider_catalog.router.routes)


def _refuse_if_introduced(outcome: SwitchOutcome) -> None:
    """409 ``{code, message}`` for a speaker-detection error the switch would introduce.

    Same shape and rule as ``PUT /user-settings/transcription``: the stable code and its
    fixed sentence, never exception text (``services/asr_switch_plan``).
    """
    if outcome.refusal is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": outcome.refusal.code, "message": outcome.refusal.message},
        )


def _refuse_if_switching_off_strands_holders(
    db: Session, config: Any, request: Request | None, *, exclude_user_id: int | None = None
) -> None:
    """409 when taking ``config`` away would send a user who runs on it to a broken default.

    An inactive configuration is treated as absent (#1207), so switching one off IS a switch
    for every user whose active pointer names it: its owner, and anyone it is shared with.
    Unsharing or deleting a shared configuration moves its members the same way, which is why
    those paths call this too, with ``exclude_user_id`` set to the owner (whose own pointer is
    handled by the caller). Each holder is assessed like ``clear-active`` (#1225, #1228); the
    first refusal wins.
    """
    query = db.query(models.UserSetting.user_id).filter(
        models.UserSetting.setting_key == "active_asr_config_id",
        models.UserSetting.setting_value == str(config.id),
    )
    if exclude_user_id is not None:
        query = query.filter(models.UserSetting.user_id != exclude_user_id)
    holders = [int(row[0]) for row in query.all()]
    for holder_id in holders:
        _refuse_if_introduced(assess_asr_switch(db, holder_id, None, request=request))


@router.get("")
def list_asr_settings(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """List the current user's ASR configurations and those shared within their tenant."""
    current_user = ctx.user
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    configs = (
        db.query(UserASRSettings)
        .filter(UserASRSettings.user_id == current_user.id)
        .order_by(UserASRSettings.created_at)
        .all()
    )
    active_config_id = _get_active_asr_config_id(db, current_user.id)

    # Resolve active UUID from the already-fetched list — no 3rd DB query needed.
    active_uuid = None
    if active_config_id:
        active = next((c for c in configs if c.id == active_config_id), None)
        if active and active.is_active:
            active_uuid = str(active.uuid)

    # Fetch shared configs from other users in the caller's active tenant
    shared_configs = (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.is_shared == True,  # noqa: E712
            UserASRSettings.user_id != current_user.id,
            owner_in_tenant(UserASRSettings.user_id, ctx.org_id),
        )
        .order_by(UserASRSettings.created_at)
        .all()
    )
    owner_ids = {c.user_id for c in shared_configs}
    owners = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(owner_ids)).all()}
        if owner_ids
        else {}
    )

    return {
        "configs": [_config_to_dict(c) for c in configs],
        "shared_configs": [
            _config_to_dict(c, owner=owners.get(c.user_id), is_own=False) for c in shared_configs
        ],
        "active_config_id": active_config_id,
        "active_config_uuid": active_uuid,
    }


@router.get("/config/{config_uuid}")
def get_asr_config(
    config_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Get a specific ASR configuration by UUID (API key is never returned)."""
    current_user = ctx.user
    config = _get_config_or_404(db, config_uuid, current_user.id, shared_in=ctx)
    return _config_to_dict(config)


@router.post("", status_code=201)
def create_asr_config(
    request: Request,
    settings_in: schemas.UserASRSettingsCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Create a new ASR configuration.

    The user's first config is auto-activated unless it was created inactive or that would make
    their stored speaker detection choice unrunnable (issue #1225); ``auto_activated`` says which, and
    ``pipeline_issues`` describes the pipeline in effect afterwards. On lite a ``local``
    config is refused (409 ``local_asr_unavailable``, #1228): it could never transcribe.
    """
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    if settings_in.provider.value == "local":
        refuse_on_lite("local_asr_unavailable")
    asr_provider_policy.refuse_config_change(
        db, current_user, settings_in.provider.value, carries_credential=True
    )

    # Duplicate name check
    existing = (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.user_id == current_user.id,
            UserASRSettings.name == settings_in.name,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Configuration named '{settings_in.name}' already exists",
        )

    encrypted_key = None
    if settings_in.api_key:
        if not test_encryption():
            raise HTTPException(status_code=500, detail="Encryption system is not working properly")
        encrypted_key = encrypt_api_key(settings_in.api_key)
        if not encrypted_key:
            raise HTTPException(status_code=500, detail="Failed to encrypt API key")

    encrypted_access_key_id = None
    if settings_in.access_key_id:
        if not test_encryption():
            raise HTTPException(status_code=500, detail="Encryption system is not working properly")
        encrypted_access_key_id = encrypt_api_key(settings_in.access_key_id)
        if not encrypted_access_key_id:
            raise HTTPException(status_code=500, detail="Failed to encrypt access key ID")

    # Check before insert whether the user already has any config (determines auto-activate).
    # This avoids a second count query after the INSERT.
    has_existing = (
        db.query(UserASRSettings.id)
        .filter(UserASRSettings.user_id == current_user.id)
        .limit(1)
        .scalar()
        is not None
    )

    is_shared = bool(settings_in.is_shared)
    config = UserASRSettings(
        user_id=current_user.id,
        name=settings_in.name,
        provider=settings_in.provider.value,
        model_name=settings_in.model_name,
        api_key=encrypted_key,
        access_key_id=encrypted_access_key_id,
        base_url=settings_in.base_url,
        region=settings_in.region,
        is_active=settings_in.is_active,
        is_shared=is_shared,
        shared_at=datetime.now(UTC) if is_shared else None,
    )
    db.add(config)
    db.commit()
    db.refresh(config)

    # Auto-activate the user's first config, unless it would break their speaker detection
    # choice: a silent switch into an error combination is what #1225 rules out.
    auto_activated = False
    if not has_existing and config.is_active:
        outcome = assess_asr_switch(db, current_user.id, config, request=request)
        if outcome.refusal is None:
            _set_active_asr_configuration(db, current_user.id, config.id)
            auto_activated = True
        else:
            logger.info(
                "Not auto-activating ASR config %s for user %d: %s",
                config.uuid,
                current_user.id,
                outcome.refusal.code,
            )

    result = _config_to_dict(config)
    result["auto_activated"] = auto_activated
    result["pipeline_issues"] = plan_issues_out(plan_for_user(db, current_user.id, request=request))
    return result


@router.put("/config/{config_uuid}")
def update_asr_config(  # noqa: C901
    request: Request,
    config_uuid: UUID,
    settings_in: schemas.UserASRSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Update an existing ASR configuration (on lite, never into a ``local`` one, #1228).

    Turning a configuration OFF is refused with 409 ``{code, message}`` when a user whose
    active pointer names it (the owner, or a member of a shared one) would fall back to a
    default the stored speaker detection or the deployment cannot run (#1225, #1228).
    """
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    if settings_in.provider is not None and settings_in.provider.value == "local":
        refuse_on_lite("local_asr_unavailable")

    config = _get_config_or_404(db, config_uuid, current_user.id)
    turning_off = "is_active" in settings_in.model_fields_set and settings_in.is_active is False
    if turning_off and config.is_active:
        _refuse_if_switching_off_strands_holders(db, config, request)
    elif (
        config.is_shared
        and "is_shared" in settings_in.model_fields_set
        and not settings_in.is_shared
    ):
        # Unsharing moves every member to the default, exactly as switching it off does.
        _refuse_if_switching_off_strands_holders(
            db, config, request, exclude_user_id=current_user.id
        )
    supplies_key = any(
        isinstance(getattr(settings_in, field), str) and getattr(settings_in, field).strip()
        for field in ("api_key", "access_key_id")
    )
    if supplies_key or "provider" in settings_in.model_fields_set:
        asr_provider_policy.refuse_config_change(
            db,
            current_user,
            settings_in.provider.value if settings_in.provider else str(config.provider),
            carries_credential=bool(supplies_key),
        )

    # Cross-field region validation needs the effective provider (may be from existing config)
    if "region" in settings_in.model_fields_set:
        incoming_provider = settings_in.provider.value if settings_in.provider else config.provider
        _validate_region(incoming_provider, settings_in.region)

    # Name update with duplicate check
    if "name" in settings_in.model_fields_set:
        new_name = settings_in.name
        if new_name != config.name:
            dup = (
                db.query(UserASRSettings)
                .filter(
                    UserASRSettings.user_id == current_user.id,
                    UserASRSettings.name == new_name,
                    UserASRSettings.id != config.id,
                )
                .first()
            )
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail=f"Configuration named '{new_name}' already exists",
                )
        config.name = new_name  # type: ignore[assignment]

    if "provider" in settings_in.model_fields_set:
        new_provider = settings_in.provider.value  # type: ignore[union-attr]
        if new_provider != config.provider:
            # Provider change invalidates the existing API key (it belongs to the old
            # provider's account).  Clear the stored key so the user is prompted to
            # supply a new one rather than inadvertently inheriting a stale credential.
            config.api_key = None  # type: ignore[assignment]
        config.provider = new_provider  # type: ignore[assignment]
        # Reset test status when provider changes
        config.test_status = None  # type: ignore[assignment]
        config.test_message = None  # type: ignore[assignment]
        config.last_tested = None  # type: ignore[assignment]

    if "model_name" in settings_in.model_fields_set:
        config.model_name = settings_in.model_name  # type: ignore[assignment]

    if "api_key" in settings_in.model_fields_set:
        new_key = settings_in.api_key
        if new_key is None or (isinstance(new_key, str) and not new_key.strip()):
            # Empty string or null from the client means "keep the existing key".
            # The frontend sends "" when the user edits other fields without touching
            # the API key field.  Only a deliberate non-empty value replaces the key.
            pass  # Existing key is preserved
        else:
            if not test_encryption():
                raise HTTPException(
                    status_code=500, detail="Encryption system is not working properly"
                )
            encrypted = encrypt_api_key(new_key)
            if not encrypted:
                raise HTTPException(status_code=500, detail="Failed to encrypt API key")
            config.api_key = encrypted  # type: ignore[assignment]
            # Reset test status only when key actually changes
            config.test_status = None  # type: ignore[assignment]

    if "access_key_id" in settings_in.model_fields_set:
        new_akid = settings_in.access_key_id
        if new_akid is None or (isinstance(new_akid, str) and not new_akid.strip()):
            pass  # Empty/null means "keep the existing access key ID" (mirrors api_key)
        else:
            if not test_encryption():
                raise HTTPException(
                    status_code=500, detail="Encryption system is not working properly"
                )
            encrypted_akid = encrypt_api_key(new_akid)
            if not encrypted_akid:
                raise HTTPException(status_code=500, detail="Failed to encrypt access key ID")
            config.access_key_id = encrypted_akid  # type: ignore[assignment]
            config.test_status = None  # type: ignore[assignment]

    if "base_url" in settings_in.model_fields_set:
        config.base_url = settings_in.base_url  # type: ignore[assignment]

    if "region" in settings_in.model_fields_set:
        config.region = settings_in.region  # type: ignore[assignment]

    if "is_active" in settings_in.model_fields_set:
        config.is_active = settings_in.is_active  # type: ignore[assignment]

    # Handle is_shared toggle with shared_at timestamp
    if "is_shared" in settings_in.model_fields_set:
        new_shared = settings_in.is_shared
        if new_shared and not config.is_shared:
            config.is_shared = True  # type: ignore[assignment]
            config.shared_at = datetime.now(UTC)  # type: ignore[assignment]
        elif not new_shared and config.is_shared:
            config.is_shared = False  # type: ignore[assignment]
            config.shared_at = None  # type: ignore[assignment]
            _clear_asr_shared_active_references(db, config.id, exclude_user_id=current_user.id)

    db.add(config)
    db.commit()
    db.refresh(config)

    return _config_to_dict(config)


@router.post("/set-active", response_model=ASRSwitchResult)
def set_active_asr_config(
    request: Request,
    body: schemas.SetActiveASRConfigRequest,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ASRSwitchResult:
    """Set the active ASR configuration for the current user.

    409 ``{code, message}`` when the switch would make the stored speaker detection choice
    unrunnable (issue #1225); 409 ``asr_config_inactive`` for a configuration whose
    ``is_active`` is off; otherwise ``pipeline_issues`` describes the new pipeline.
    """
    current_user = ctx.user
    config_uuid = body.resolved_uuid()
    if not config_uuid:
        raise HTTPException(status_code=400, detail="config_uuid (or uuid) is required")

    config = _get_config_or_404(db, config_uuid, current_user.id, shared_in=ctx)
    if not config.is_active:
        # An inactive configuration is never used, so activating it would only mislead.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "asr_config_inactive",
                "message": "This configuration is switched off. Turn it on before activating it.",
            },
        )
    outcome = assess_asr_switch(db, current_user.id, config, request=request)
    _refuse_if_introduced(outcome)
    _set_active_asr_configuration(db, current_user.id, config.id)

    return ASRSwitchResult(
        message="Active ASR configuration updated",
        config_uuid=str(config.uuid),
        pipeline_issues=plan_issues_out(outcome.plan),
    )


@router.delete("/config/{config_uuid}", response_model=schemas.ASRConfigDeleted)
def delete_asr_config(
    request: Request,
    config_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> schemas.ASRConfigDeleted:
    """Delete an ASR configuration and report the pipeline it leaves (issue #1236).

    A speaker-detection choice naming this configuration (source ``asr_config``) is reset
    to the default for every user who held it (#1226); ``diarizer_source_reverted`` says
    whether the caller's was.

    If it was active, ``choose_after_active_delete`` picks what becomes active: the first
    remaining config that keeps the stored speaker detection choice runnable (#1225), else
    the cleared pointer. On lite, where clearing means local Whisper, a remaining config
    that can transcribe is kept instead, and with none the delete is refused (409
    ``local_asr_unavailable``, #1228). Otherwise deletion is never refused: a stored choice
    the remaining state cannot run comes back in ``pipeline_issues`` (as for
    ``set-active``), with ``config_uuid`` naming the config now active (``null``: none).
    """
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]
    from app.services.asr_switch_plan import active_delete_refusal
    from app.services.asr_switch_plan import choose_after_active_delete

    config = _get_config_or_404(db, config_uuid, current_user.id)
    active_config_id = _get_active_asr_config_id(db, current_user.id)
    was_active = active_config_id == config.id
    config_id = config.id
    candidates = (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.user_id == current_user.id,
            UserASRSettings.id != config_id,
            # An inactive config is never promoted: it would be active in name only.
            UserASRSettings.is_active.is_(True),
        )
        .order_by(UserASRSettings.id)
        .all()
        if was_active
        else []
    )
    if was_active:
        refusal = active_delete_refusal(db, current_user.id, candidates, request=request)
        if refusal is not None:
            raise HTTPException(
                status_code=409, detail={"code": refusal.code, "message": refusal.message}
            )

    # Clean up other users who had this shared config active (preserve owner's for auto-promote)
    if config.is_shared:
        _refuse_if_switching_off_strands_holders(
            db, config, request, exclude_user_id=current_user.id
        )
        _clear_asr_shared_active_references(db, config.id, exclude_user_id=current_user.id)
    reverted = _reset_diarizer_references(db, [str(config.uuid)], current_user.id)

    db.delete(config)
    db.commit()

    promoted = None
    if was_active:
        promoted = choose_after_active_delete(db, current_user.id, candidates, request=request)
        if promoted is not None:
            _set_active_asr_configuration(db, current_user.id, promoted.id)
        else:
            _clear_active_asr_setting(db, current_user.id)
    active_uuid = None
    if promoted is not None:
        active_uuid = str(promoted.uuid)
    elif not was_active and active_config_id is not None:
        still_active = db.query(UserASRSettings.uuid).filter_by(id=active_config_id).scalar()
        active_uuid = str(still_active) if still_active is not None else None
    return schemas.ASRConfigDeleted(
        message="ASR configuration deleted",
        config_uuid=active_uuid,
        diarizer_source_reverted=reverted,
        pipeline_issues=plan_issues_out(plan_for_user(db, current_user.id, request=request)),
    )


@router.post("/clear-active", response_model=ASRSwitchResult)
def clear_active_asr_config(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> ASRSwitchResult:
    """Clear the active ASR configuration, reverting to the deployment default.

    409 ``{code, message}`` when that would make the stored speaker detection choice
    unrunnable (issue #1225); otherwise ``pipeline_issues`` describes the new pipeline.
    """
    outcome = assess_asr_switch(db, current_user.id, None, request=request)
    _refuse_if_introduced(outcome)
    _clear_active_asr_setting(db, current_user.id)
    return ASRSwitchResult(
        message="Reverted to local ASR default",
        pipeline_issues=plan_issues_out(outcome.plan),
    )


@router.delete("/all", response_model=schemas.ASRConfigDeleted)
def delete_all_asr_configs(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> schemas.ASRConfigDeleted:
    """Delete all ASR configurations for the current user (reverts to system default).

    Speaker-detection choices naming any of them are reset like a single delete. On lite,
    where the default is local Whisper, deleting a user's configs is refused (409
    ``local_asr_unavailable``, #1228). Each shared configuration is treated as a single
    delete treats it: a member who runs on it is assessed first (the whole call is refused,
    and nothing is deleted, if any member would be stranded), and the members' active
    pointers are cleared with it.
    """
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    owned = db.query(UserASRSettings).filter(UserASRSettings.user_id == current_user.id)
    rows = owned.all()
    uuids = [str(row.uuid) for row in rows]
    if uuids:
        # Clearing every config clears the active one, and without a config lite would fall
        # back to local Whisper, which it does not ship (#1228). With nothing configured
        # there is nothing to delete and no fallback to introduce.
        refuse_on_lite("local_asr_unavailable")
    shared_ids = [int(row.id) for row in rows if row.is_shared]
    for row in rows:
        if row.is_shared:
            _refuse_if_switching_off_strands_holders(
                db, row, request, exclude_user_id=current_user.id
            )
    for config_id in shared_ids:
        _clear_asr_shared_active_references(db, config_id, exclude_user_id=current_user.id)
    reverted = _reset_diarizer_references(db, uuids, current_user.id)
    owned.delete()
    _clear_active_asr_setting(db, current_user.id)
    db.commit()
    return schemas.ASRConfigDeleted(
        message="All ASR configurations deleted",
        config_uuid=None,
        diarizer_source_reverted=reverted,
        pipeline_issues=plan_issues_out(plan_for_user(db, current_user.id, request=request)),
    )


router.routes.extend(asr_connection_tests.router.routes)
