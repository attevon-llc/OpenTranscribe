"""Edition capabilities / entitlements (cloud-edition seam).

Server-driven feature gating: the backend decides which feature surfaces
exist and the frontend renders only what ``GET /system/capabilities``
returns.

Community/self-hosted default: **everything ON** (zero behavior change).
The commercial cloud edition replaces the resolver via
``set_capability_resolver`` to compute ``edition ∩ subscription tier``
(e.g. hide engine tuning and watch folders from tenants, gate BYOK-LLM to a
paid tier). Capability KEYS are generic core vocabulary; the cloud
*resolution policy* is proprietary and lives in the private layer.

Rule: every gate — backend dependency, beat schedule, frontend tab — reads
the same capability key. Never sprinkle ``if edition == "cloud"`` in feature
code.

What a capability actually enforces today
-----------------------------------------
The keys below are declared in one map but are **not** all enforced to the
same depth. Read this before assuming a key protects anything:

1. **Server-enforced** — wired to a ``require_capability`` router dependency
   in ``app/api/router.py``; disabling one makes every route under that
   router 404, so the UI gate is genuinely cosmetic:
   ``watch_sources``, ``organizations``, ``auth.config_ui``,
   ``llm.user_settings``, ``asr.user_providers``, ``engine.settings``.

2. **UI-only vocabulary** — read *only* by ``SettingsModal.svelte`` to hide a
   settings panel. The underlying endpoints stay fully reachable, so this is
   presentation, not authorization: ``recording``, ``exports``,
   ``transcription.prefs``, ``vocab.user``, ``prompts.user``,
   ``redaction.user``, ``sharing.teams``, ``redaction.policy``, ``billing``,
   ``usage_dashboard``, ``users.local_admin``, ``system.hardware_stats``,
   ``audit.logs``, and the ``admin.*`` platform panels.

3. **Declared but unread** — no router, no task, no UI reads these yet.
   Setting ``upload: False`` or ``search: False`` changes nothing at all:
   ``upload``, ``url_ingest``, ``search``, ``comments``,
   ``collections.shared``, ``speakers.shared``, ``prompts.shared``,
   ``org.defaults``, ``asr.model_selection``, ``llm.byok``.

Tiers 2 and 3 are a to-do list, not a contract. A key becomes authoritative
only when a router (or task/beat) reads it — attaching one is a behavior
change and needs its own review.

``tests/unit/test_capability_contract.py`` pins the frontend's ``cap:``
strings as a subset of ``COMMUNITY_CAPABILITIES``: the two maps previously
drifted in opposite directions, and because unknown keys fail **closed**
here but **open** in the frontend store, the drift was invisible until a key
was finally declared.
"""

import logging
from collections.abc import Callable

from fastapi import HTTPException
from fastapi import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---- Audience taxonomy -------------------------------------------------------
# WHO a surface is for when it is enabled. Orthogonal to enabled-ness: the
# capability map says whether the surface EXISTS in this deployment; the
# audience says which role's UI/permissions it belongs to. End users are
# never admins; org admins manage their tenant only (billing, members,
# policies) and are NEVER platform admins; "platform" = Attevon staff in
# cloud / the operator in self-host.
AUDIENCE_USER = "user"  # individual end users (their own content/prefs)
AUDIENCE_TEAM = "team"  # inner-team collaboration among org/group members
AUDIENCE_ORG_ADMIN = "org_admin"  # tenant administrators (billing, seats, policies)
AUDIENCE_PLATFORM = "platform"  # platform staff / self-host operator (config & ops)

# Known capability keys with their COMMUNITY defaults (everything on except
# the cloud-only surfaces, which don't exist without the cloud layer).
COMMUNITY_CAPABILITIES: dict[str, bool] = {
    # -- user-facing ------------------------------------------------------------
    "upload": True,  # file upload + presigned flow
    "recording": True,  # in-browser recording
    "url_ingest": True,  # yt-dlp URL ingestion
    "search": True,  # hybrid/semantic search
    "exports": True,  # SRT/VTT/TXT/bulk exports
    "comments": True,  # per-transcript comments
    "transcription.prefs": True,  # language/translate/speaker prefs
    "vocab.user": True,  # custom vocabulary (own terms)
    "prompts.user": True,  # own summary prompts
    "redaction.user": True,  # personal redaction preferences
    "asr.user_providers": True,  # per-user cloud-ASR provider configs + keys
    "llm.user_settings": True,  # per-user LLM provider/endpoint/keys
    "watch_sources": True,  # local/S3/SMB auto-import (Settings → Watch Sources)
    # -- team-facing (inner-team collaboration) ----------------------------------
    "sharing.teams": True,  # groups + collection shares
    "collections.shared": True,  # shared/org-visible collections
    "speakers.shared": True,  # shared speaker profiles
    "prompts.shared": True,  # shared prompt library
    # -- org-admin (tenant administration; cloud-only surfaces) ------------------
    "billing": False,
    "usage_dashboard": False,
    "organizations": False,  # member/seat management surface
    "org.defaults": False,  # org-level default settings/policies
    "llm.byok": False,  # bring-your-own LLM key (cloud tier-gated extra)
    "redaction.policy": True,  # enforcement floor (org admin in cloud; operator here)
    # -- platform (staff / self-host operator config & ops) ----------------------
    "auth.config_ui": True,  # LDAP/Keycloak/PKI admin configuration UI
    "users.local_admin": True,  # local user CRUD admin UI
    "asr.model_selection": True,  # admin local-model pinning UI
    "engine.settings": True,  # diarization/boundary tuning admin panel
    "system.hardware_stats": True,  # GPU/CPU stats, benchmarks
    "audit.logs": True,  # audit log viewer (super-admin)
    "admin.data_integrity": True,  # orphaned-record / storage reconciliation panel
    "admin.retention": True,  # file retention + derived-media cache policy
    "admin.backup": True,  # scheduled database backups
    "admin.search_indexing": True,  # reindex / embedding-model management
    "admin.embedding_migration": True,  # embedding migration + consistency panel
    "admin.task_health": True,  # stuck-task recovery / queue health panel
}

# Every capability key MUST be classified, and the two maps must carry the
# SAME key set (both pinned by tests). The frontend uses this to place
# surfaces in the right area (user settings / team / org admin / platform
# admin) — and to render nothing at all for roles below the audience.
CAPABILITY_AUDIENCE: dict[str, str] = {
    "upload": AUDIENCE_USER,
    "recording": AUDIENCE_USER,
    "url_ingest": AUDIENCE_USER,
    "search": AUDIENCE_USER,
    "exports": AUDIENCE_USER,
    "comments": AUDIENCE_USER,
    "transcription.prefs": AUDIENCE_USER,
    "vocab.user": AUDIENCE_USER,
    "prompts.user": AUDIENCE_USER,
    "redaction.user": AUDIENCE_USER,
    "asr.user_providers": AUDIENCE_USER,
    "llm.user_settings": AUDIENCE_USER,
    "watch_sources": AUDIENCE_USER,
    "sharing.teams": AUDIENCE_TEAM,
    "collections.shared": AUDIENCE_TEAM,
    "speakers.shared": AUDIENCE_TEAM,
    "prompts.shared": AUDIENCE_TEAM,
    "billing": AUDIENCE_ORG_ADMIN,
    "usage_dashboard": AUDIENCE_ORG_ADMIN,
    "organizations": AUDIENCE_ORG_ADMIN,
    "org.defaults": AUDIENCE_ORG_ADMIN,
    "llm.byok": AUDIENCE_ORG_ADMIN,
    "redaction.policy": AUDIENCE_ORG_ADMIN,
    "auth.config_ui": AUDIENCE_PLATFORM,
    "users.local_admin": AUDIENCE_PLATFORM,
    "asr.model_selection": AUDIENCE_PLATFORM,
    "engine.settings": AUDIENCE_PLATFORM,
    "system.hardware_stats": AUDIENCE_PLATFORM,
    "audit.logs": AUDIENCE_PLATFORM,
    "admin.data_integrity": AUDIENCE_PLATFORM,
    "admin.retention": AUDIENCE_PLATFORM,
    "admin.backup": AUDIENCE_PLATFORM,
    "admin.search_indexing": AUDIENCE_PLATFORM,
    "admin.embedding_migration": AUDIENCE_PLATFORM,
    "admin.task_health": AUDIENCE_PLATFORM,
}

# Resolver signature: (request | None) -> capability dict. The request is
# offered so a cloud resolver can derive tenant/tier from the verified
# identity stashed by get_current_user (request.state.external_identity).
CapabilityResolver = Callable[[Request | None], dict[str, bool]]


def _community_resolver(_request: Request | None) -> dict[str, bool]:
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


def get_capabilities(request: Request | None = None) -> dict[str, bool]:
    """Effective capability map for this deployment/request.

    Unknown keys from a custom resolver are passed through; missing known
    keys fall back to the community defaults so a partial resolver cannot
    accidentally disable surfaces it never considered.
    """
    resolved = _resolver(request)
    caps = dict(COMMUNITY_CAPABILITIES)
    caps.update(resolved)
    return caps


def capability_enabled(key: str, request: Request | None = None) -> bool:
    """Check a single capability (False for unknown keys)."""
    return bool(get_capabilities(request).get(key, False))


def require_capability(key: str, *, platform_admin_bypass: bool = True) -> Callable:
    """FastAPI dependency factory: 404 when the capability is off.

    404 (not 403) on purpose — a disabled feature surface should not exist
    at all for this deployment, exactly like an unknown route.

    ``platform_admin_bypass`` (default on) lets platform staff
    (``is_superuser``) through even when the capability is hidden from
    tenants — cloud tenants can never become superusers (external_sync never
    grants it), so this exactly implements the capabilities matrix's
    "invisible to org users / available to platform staff" column.
    """

    def _dependency(request: Request) -> None:
        if capability_enabled(key, request):
            return
        if platform_admin_bypass:
            try:
                from app.api.endpoints.auth import get_optional_current_user
                from app.db.base import SessionLocal

                db = SessionLocal()
                try:
                    user = get_optional_current_user(request, db)
                    if user is not None and bool(user.is_superuser):
                        return
                finally:
                    db.close()
            except Exception:
                # Bypass is best-effort; any failure falls through to 404.
                logger.debug("platform-admin bypass check failed", exc_info=True)
        raise HTTPException(status_code=404, detail="Not Found")

    return _dependency


def edition() -> str:
    """Deployment edition string ("community" | "cloud")."""
    return settings.DEPLOYMENT_EDITION
