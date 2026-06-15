"""Per-tenant limit resolvers (cloud-edition seam).

Two registry hooks the proprietary cloud layer overrides to apply per-org
(per-subscription-tier) policy on top of core's global defaults. Both default
to a community no-op (return ``None`` -> "no override, use the global value"),
so the open-source / self-host edition behaves EXACTLY as before — there is no
behavior change unless the cloud layer registers a resolver.

Why a resolver and not a column:
  * core stays vendor-neutral — it has no concept of subscription tiers, and
    the billing schema is closed-source (``organization_billing`` lives in the
    cloud Alembic chain). A resolver lets the cloud compute the value from its
    own billing state without core importing any cloud module.
  * mirrors the capability resolver (``app.core.capabilities``): core ships a
    community resolver; the cloud replaces it at activation.

Hook 1 — **retention override** (``resolve_retention_days``). core's file
retention is a single global ``files.retention_days`` SystemSetting. A cloud
tenant on a higher tier may keep files longer (or a free tier shorter). The
cleanup task asks the resolver per organization; ``None`` means "use the global
SystemSetting".

Hook 2 — **upload limits** (``resolve_upload_limits``). core enforces one global
ceiling (15 GB / 4 h). A cloud tier may allow more or less. The resolver returns
a :class:`TenantUploadLimits` (max bytes / max duration seconds, either field
``None`` = no per-tenant override for that dimension); ``None`` for the whole
result means "no override, use the global ceiling".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantUploadLimits:
    """Per-tenant upload ceiling (either dimension ``None`` = no override)."""

    max_file_bytes: int | None = None
    max_duration_seconds: int | None = None


# Resolver signatures. ``organization_id`` is None for personal/no-org scope
# (community edition is ALWAYS None here), in which case a community resolver
# returns None and the global value applies.
RetentionResolver = Callable[[int | None], int | None]
UploadLimitsResolver = Callable[[int | None], TenantUploadLimits | None]
# Reports the LARGEST per-org retention override currently in effect (days), or
# None if no override can exceed the global value. The cleanup task uses it to
# widen its candidate query so a tenant whose override is LONGER than the global
# setting is still considered (and then correctly kept, since its file age is
# below its effective retention). Community: None.
MaxRetentionResolver = Callable[[], int | None]


def _community_retention_resolver(_org_id: int | None) -> int | None:
    return None  # no override -> global files.retention_days applies


def _community_max_retention_resolver() -> int | None:
    return None  # no override can exceed the global value


def _community_upload_limits_resolver(_org_id: int | None) -> TenantUploadLimits | None:
    return None  # no override -> global ceiling applies


_retention_resolver: RetentionResolver = _community_retention_resolver
_max_retention_resolver: MaxRetentionResolver = _community_max_retention_resolver
_upload_limits_resolver: UploadLimitsResolver = _community_upload_limits_resolver


def set_retention_resolver(
    resolver: RetentionResolver,
    max_resolver: MaxRetentionResolver | None = None,
) -> None:
    """Replace the retention resolver (registered by the cloud layer).

    ``max_resolver`` (optional) reports the largest override in effect so the
    cleanup candidate query can be widened; defaults to the community no-op.
    """
    global _retention_resolver, _max_retention_resolver
    logger.info("Retention resolver overridden (cloud edition)")
    _retention_resolver = resolver
    if max_resolver is not None:
        _max_retention_resolver = max_resolver


def set_upload_limits_resolver(resolver: UploadLimitsResolver) -> None:
    """Replace the upload-limits resolver (registered by the cloud layer)."""
    global _upload_limits_resolver
    logger.info("Upload-limits resolver overridden (cloud edition)")
    _upload_limits_resolver = resolver


def reset_resolvers() -> None:
    """Restore the community resolvers (primarily for tests)."""
    global _retention_resolver, _max_retention_resolver, _upload_limits_resolver
    _retention_resolver = _community_retention_resolver
    _max_retention_resolver = _community_max_retention_resolver
    _upload_limits_resolver = _community_upload_limits_resolver


def max_retention_override_days() -> int | None:
    """Largest per-org retention override in effect (days), or None.

    Best-effort: any error returns None (no widening).
    """
    try:
        return _max_retention_resolver()
    except Exception:
        logger.exception("max-retention resolver failed; not widening cleanup window")
        return None


def resolve_retention_days(organization_id: int | None) -> int | None:
    """Per-org retention override in days, or ``None`` to use the global setting.

    Best-effort: a misbehaving resolver never breaks cleanup — on any error we
    fall back to the global value (return None).
    """
    try:
        return _retention_resolver(organization_id)
    except Exception:
        logger.exception("retention resolver failed; falling back to global setting")
        return None


def resolve_upload_limits(organization_id: int | None) -> TenantUploadLimits | None:
    """Per-org upload ceiling, or ``None`` to use the global ceiling.

    Best-effort: a misbehaving resolver never blocks uploads — on any error we
    fall back to the global ceiling (return None).
    """
    try:
        return _upload_limits_resolver(organization_id)
    except Exception:
        logger.exception("upload-limits resolver failed; falling back to global ceiling")
        return None
