"""ASR provider catalog and status endpoints (``/providers``, ``/local-models``, ``/status``)."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app import models
from app.api.deps_context import get_current_context
from app.api.endpoints.asr_config_state import _config_to_dict
from app.api.endpoints.asr_config_state import _get_active_asr_config_id
from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings
from app.db.base import get_db
from app.schemas.asr_settings import ASRModelCapabilities
from app.services import asr_provider_policy
from app.services.request_context import RequestContext
from app.utils.tenant_sharing import owner_in_tenant

router = APIRouter()
logger = logging.getLogger(__name__)

# Cloud-based providers (excludes "local").
_CLOUD_PROVIDERS = {
    "deepgram",
    "assemblyai",
    "openai",
    "google",
    "azure",
    "aws",
    "speechmatics",
    "gladia",
    "pyannote",
}

# ---------------------------------------------------------------------------
# Lazy imports to avoid circular dependencies at module load time
# ---------------------------------------------------------------------------


def _get_provider_catalog() -> dict:
    """Return the ASR provider catalog, or empty dict if not yet available."""
    try:
        from app.services.asr.catalog import ASR_PROVIDER_CATALOG  # type: ignore[import]

        return ASR_PROVIDER_CATALOG
    except ImportError:
        logger.debug("ASR provider catalog not yet available (services/asr not yet created)")
        return {}


@router.get("/providers")
def get_providers(
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Any:
    """Return the ASR provider catalog with models, pricing, and capabilities.

    For the local provider, each model entry includes a ``downloaded`` boolean
    indicating whether the model weights are already present on disk. A plain user
    sees only the cloud providers the administrator enabled; an admin sees all, each
    with ``allowed_for_users`` (``services/asr_provider_policy``).
    """
    catalog = _get_provider_catalog()
    providers = asr_provider_policy.visible_providers(
        list(catalog.values()), current_user.role, asr_provider_policy.load_policy(db)
    )

    # Annotate local models with download availability
    try:
        from app.services.asr.model_discovery import get_downloaded_model_names

        downloaded = get_downloaded_model_names()
        for p in providers:
            if p.get("id") == "local":
                for m in p.get("models", []):
                    m["downloaded"] = m["id"] in downloaded
                break
    except Exception:
        logger.debug("Model discovery unavailable, skipping download annotations")

    return {"providers": providers}


@router.get("/local-models")
def get_local_models(
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Return locally downloaded Whisper models discovered from the cache directory."""
    from app.services.asr.model_discovery import discover_local_models

    return {"models": discover_local_models()}


@router.get("/status")
def get_asr_status(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Get ASR configuration status summary for the current user."""
    current_user = ctx.user
    from app.models.user_asr_settings import UserASRSettings  # type: ignore[import]

    # Single query: fetch all configs + active_config_id setting in 2 queries total.
    # Resolve active_config from the already-fetched list (no 3rd DB round-trip).
    active_config_id = _get_active_asr_config_id(db, current_user.id)

    configs = db.query(UserASRSettings).filter(UserASRSettings.user_id == current_user.id).all()
    has_settings = len(configs) > 0

    active_config = None
    if active_config_id:
        # Try own configs first, then shared
        active_config = next((c for c in configs if c.id == active_config_id), None)
        if not active_config:
            active_config = (
                db.query(UserASRSettings)
                .filter(
                    UserASRSettings.id == active_config_id,
                    UserASRSettings.is_shared == True,  # noqa: E712
                    owner_in_tenant(UserASRSettings.user_id, ctx.org_id),
                )
                .first()
            )
            if active_config:
                has_settings = True
        if active_config is not None and not active_config.is_active:
            # Switched off by its owner: jobs fall back to the deployment default.
            active_config = None

    deployment_mode = settings.DEPLOYMENT_MODE.lower()
    asr_provider = settings.ASR_PROVIDER.lower()
    using_local_default = active_config is None and asr_provider == "local"

    active_provider: str | None = None
    active_model: str | None = None
    active_config_uuid_str: str | None = None
    is_cloud_provider = False

    if active_config:
        active_provider = str(getattr(active_config, "provider", "") or "")
        active_model = str(getattr(active_config, "model_name", "") or "")
        active_config_uuid_str = str(getattr(active_config, "uuid", "") or "")
        is_cloud_provider = active_provider.lower() in _CLOUD_PROVIDERS
    elif not using_local_default:
        # Env-level provider (not local)
        active_provider = asr_provider
        is_cloud_provider = asr_provider in _CLOUD_PROVIDERS

    from app.services.asr.factory import ASRProviderFactory

    # Inline capability lookup using already-resolved active_config
    # (avoids 2 duplicate DB queries that get_active_model_capabilities would make)
    if active_config:
        provider_id = str(active_config.provider or "local")
        model_id = str(active_config.model_name or "")
        caps = ASRProviderFactory.get_model_capabilities(provider_id, model_id)
        model_capabilities = {"provider": provider_id, "model_id": model_id, **caps}
    elif asr_provider != "local":
        caps = ASRProviderFactory.get_model_capabilities(asr_provider, "")
        model_capabilities = {"provider": asr_provider, "model_id": "", **caps}
    else:
        whisper_model = settings.WHISPER_MODEL
        caps = ASRProviderFactory.get_model_capabilities("local", whisper_model)
        model_capabilities = {"provider": "local", "model_id": whisper_model, **caps}

    return {
        "has_settings": has_settings,
        "active_config": _config_to_dict(active_config) if active_config else None,
        "using_local_default": using_local_default,
        "deployment_mode": deployment_mode,
        "asr_configured": bool(active_config) or not using_local_default,
        "active_provider": active_provider,
        "active_model": active_model,
        "active_config_uuid": active_config_uuid_str,
        "is_cloud_provider": is_cloud_provider,
        # Validated through the typed schema so a catalog entry missing a capability
        # field fails here rather than reaching the SPA as an absent key.
        "active_model_capabilities": ASRModelCapabilities.model_validate(
            model_capabilities
        ).model_dump(),
    }
