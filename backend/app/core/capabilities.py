"""Edition capabilities / entitlements (cloud-edition seam).

Server-driven feature gating: the backend decides which feature surfaces
exist; the frontend renders only what `GET /system/capabilities` returns, and
gated endpoints 404 when a capability is off — UI hiding is cosmetic, the
backend gate is the authority.

Community/self-hosted default: **everything ON** (zero behavior change).
The commercial cloud edition replaces the resolver via
``set_capability_resolver`` to compute ``edition ∩ subscription tier``
(e.g. hide engine tuning and watch folders from tenants, gate BYOK-LLM to a
paid tier). Capability KEYS are generic core vocabulary; the cloud
*resolution policy* is proprietary and lives in the private layer.

Rule: every gate — backend dependency, beat schedule, frontend tab — reads
the same capability key. Never sprinkle ``if edition == "cloud"`` in feature
code.
"""

import logging
from typing import Callable
from typing import Optional

from fastapi import HTTPException
from fastapi import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

# Known capability keys with their COMMUNITY defaults (everything on except
# the cloud-only surfaces, which don't exist without the cloud layer).
COMMUNITY_CAPABILITIES: dict[str, bool] = {
    # Feature surfaces that exist today
    "watch_sources": True,  # local/S3/SMB auto-import (Settings → Watch Sources)
    "asr.user_providers": True,  # per-user cloud-ASR provider configs + keys
    "asr.model_selection": True,  # admin local-model pinning UI
    "engine.settings": True,  # diarization/boundary tuning admin panel
    "llm.user_settings": True,  # per-user LLM provider/endpoint/keys
    "auth.config_ui": True,  # LDAP/Keycloak/PKI admin configuration UI
    "users.local_admin": True,  # local user CRUD admin UI
    "system.hardware_stats": True,  # GPU/CPU stats, benchmarks
    "url_ingest": True,  # yt-dlp URL ingestion
    "recording": True,  # in-browser recording
    # Cloud-only surfaces (no implementation in core — the cloud layer
    # flips these on and mounts the corresponding routers/UI)
    "billing": False,
    "usage_dashboard": False,
    "organizations": False,
}

# Resolver signature: (request | None) -> capability dict. The request is
# offered so a cloud resolver can derive tenant/tier from the verified
# identity stashed by get_current_user (request.state.external_identity).
CapabilityResolver = Callable[[Optional[Request]], dict[str, bool]]


def _community_resolver(_request: Optional[Request]) -> dict[str, bool]:
    return dict(COMMUNITY_CAPABILITIES)


_resolver: CapabilityResolver = _community_resolver


def set_capability_resolver(resolver: CapabilityResolver) -> None:
    """Replace the capability resolver (registered by the cloud layer)."""
    global _resolver
    logger.info("Capability resolver overridden (cloud edition)")
    _resolver = resolver


def reset_capability_resolver() -> None:
    """Restore the community resolver (primarily for tests)."""
    global _resolver
    _resolver = _community_resolver


def get_capabilities(request: Optional[Request] = None) -> dict[str, bool]:
    """Effective capability map for this deployment/request.

    Unknown keys from a custom resolver are passed through; missing known
    keys fall back to the community defaults so a partial resolver cannot
    accidentally disable surfaces it never considered.
    """
    resolved = _resolver(request)
    caps = dict(COMMUNITY_CAPABILITIES)
    caps.update(resolved)
    return caps


def capability_enabled(key: str, request: Optional[Request] = None) -> bool:
    """Check a single capability (False for unknown keys)."""
    return bool(get_capabilities(request).get(key, False))


def require_capability(key: str) -> Callable:
    """FastAPI dependency factory: 404 when the capability is off.

    404 (not 403) on purpose — a disabled feature surface should not exist
    at all for this deployment, exactly like an unknown route.
    """

    def _dependency(request: Request) -> None:
        if not capability_enabled(key, request):
            raise HTTPException(status_code=404, detail="Not Found")

    return _dependency


def edition() -> str:
    """Deployment edition string ("community" | "cloud")."""
    return settings.DEPLOYMENT_EDITION
