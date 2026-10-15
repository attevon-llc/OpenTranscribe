"""Which cloud ASR providers plain users may choose, and whether they may bring a key (#1246).

Two deployment-wide ``SystemSettings`` keys (no migration):

- ``asr.cloud_providers_for_users``: JSON list of cloud provider ids. Absent / ``null`` means
  every provider is allowed (what a deployment did before this existed); ``[]`` means none.
- ``asr.users_may_add_own_keys``: absent means true.

Credentials are not stored here. They are the admin-shared ``UserASRSettings`` rows that
already exist (encrypted, write-only); this module only decides who may pick which provider.
Enforcement is the pipeline plan's (``services/pipeline_plan_inputs``), so dispatch,
set-active and the speaker-source PUT refuse without a check of their own. Creating or
re-pointing a config is checked here because no plan exists for a config nobody has
activated yet.

Admins and super_admins are exempt: they own the deployment's choice and configure the
shared credentials. So is the ``ASR_PROVIDER`` environment default, which is the
deployment's own decision.

A stored value that cannot be read fails CLOSED (no cloud provider allowed): this is an
egress control, and guessing "everything" from a corrupt list would be the unsafe default.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException

from app.auth.roles import ELEVATED_ROLES
from app.core.constants import ASR_CLOUD_PROVIDERS_FOR_USERS_KEY
from app.core.constants import ASR_USERS_OWN_KEYS_KEY
from app.services import system_settings_service

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

LOCAL = "local"

#: Fixed sentence for the 409 when a user may not save a key of their own.
OWN_KEYS_DISABLED_MESSAGE = (
    "Your administrator does not allow adding your own transcription provider keys. "
    "Use a provider they have shared."
)
#: Fixed sentence for the 422 when the policy names a provider this build does not have.
UNKNOWN_PROVIDER_MESSAGE = "One of the providers is not a cloud transcription provider."


@dataclass(frozen=True)
class ProviderPolicy:
    """``allowed`` is ``None`` for "every provider"; ``own_keys`` gates user-held keys."""

    allowed: frozenset[str] | None
    own_keys: bool


def cloud_provider_ids() -> list[str]:
    """Every catalog provider id except ``local``, in catalog order."""
    from app.services.asr.factory import ASR_PROVIDER_CATALOG

    return [pid for pid in ASR_PROVIDER_CATALOG if pid != LOCAL]


def load_policy(db: Session) -> ProviderPolicy:
    """The stored policy; a missing key is "allow all", an unreadable one is "allow none"."""
    raw = system_settings_service.get_setting(db, ASR_CLOUD_PROVIDERS_FOR_USERS_KEY, None)
    allowed: frozenset[str] | None = None
    if raw is not None:
        try:
            parsed = json.loads(raw)
            if parsed is None:
                allowed = None
            elif isinstance(parsed, list) and all(isinstance(p, str) for p in parsed):
                allowed = frozenset(parsed)
            else:
                raise ValueError("not a list of strings")
        except ValueError:
            logger.warning(
                "Unreadable %s; allowing no cloud provider", ASR_CLOUD_PROVIDERS_FOR_USERS_KEY
            )
            allowed = frozenset()
    own_keys = system_settings_service.get_setting_bool(db, ASR_USERS_OWN_KEYS_KEY, True)
    return ProviderPolicy(allowed=allowed, own_keys=own_keys)


def save_policy(db: Session, allowed: list[str] | None, own_keys: bool) -> ProviderPolicy:
    """Store the policy.

    Args:
        db: An open session; committed.
        allowed: Provider ids users may choose, ``None`` for all.
        own_keys: Whether users may save keys of their own.

    Raises:
        ValueError: An id is not a cloud provider in the catalog.
    """
    if allowed is not None:
        known = set(cloud_provider_ids())
        if not set(allowed) <= known:
            raise ValueError("unknown provider")
        allowed = [pid for pid in cloud_provider_ids() if pid in set(allowed)]
    system_settings_service.set_setting(
        db,
        ASR_CLOUD_PROVIDERS_FOR_USERS_KEY,
        json.dumps(allowed),
        "Cloud ASR providers plain users may choose (null = all)",
    )
    system_settings_service.set_setting(
        db, ASR_USERS_OWN_KEYS_KEY, own_keys, "Whether users may add their own ASR provider keys"
    )
    return load_policy(db)


def is_exempt(role: str | None) -> bool:
    """Admins and super_admins choose for the deployment; the policy does not bind them."""
    return role in ELEVATED_ROLES


def provider_allowed(role: str | None, provider: str, policy: ProviderPolicy) -> bool:
    """Whether a user of ``role`` may use ``provider`` (``local`` is never restricted)."""
    if provider == LOCAL or is_exempt(role):
        return True
    return policy.allowed is None or provider in policy.allowed


def may_store_own_key(role: str | None, policy: ProviderPolicy) -> bool:
    """Whether a user of ``role`` may save a cloud configuration or key of their own."""
    return is_exempt(role) or policy.own_keys


def refuse_config_change(
    db: Session, user: object, provider: str | None, *, carries_credential: bool
) -> None:
    """409 when ``user`` may not create or re-point a config at ``provider``.

    Args:
        db: An open session.
        user: The caller (``role`` is read).
        provider: The provider the config will have; ``None`` or ``local`` is never refused.
        carries_credential: True when the request supplies a key; an own-key refusal needs
            either a new config or a credential, so a rename is not blocked by it.

    Raises:
        HTTPException: 409 ``{code, message}`` with a fixed sentence.
    """
    if not provider or provider == LOCAL:
        return
    role = getattr(user, "role", None)
    if is_exempt(role):
        return
    from app.services.pipeline_plan import ISSUE_MESSAGES

    policy = load_policy(db)
    if not provider_allowed(role, provider, policy):
        code = "asr_provider_not_allowed"
        raise HTTPException(status_code=409, detail={"code": code, "message": ISSUE_MESSAGES[code]})
    if carries_credential and not policy.own_keys:
        raise HTTPException(
            status_code=409,
            detail={"code": "own_asr_keys_disabled", "message": OWN_KEYS_DISABLED_MESSAGE},
        )


def visible_providers(
    providers: list[dict], role: str | None, policy: ProviderPolicy
) -> list[dict]:
    """The catalog as ``role`` may see it.

    Plain users get ``local`` plus the allowed providers. Admins get every provider, each
    with ``allowed_for_users`` so the admin UI can show the toggle. Entries are copied; the
    module-level catalog is never mutated.
    """
    if is_exempt(role):
        return [
            {**p, "allowed_for_users": provider_allowed(None, str(p.get("id")), policy)}
            for p in providers
        ]
    return [p for p in providers if provider_allowed(role, str(p.get("id")), policy)]
