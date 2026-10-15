"""Tenancy mode detection (issue #1122).

``User.is_admin`` historically works as a global content bypass. That is correct on a
single-tenant install and an unaudited cross-tenant read on a multi-tenant one, so every
bypass decision keys on the mode this module computes.

The mode **fails closed**: anything this module cannot positively classify as single-tenant
is multi-tenant, which removes the implicit admin bypass rather than granting it.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import StrEnum

from sqlalchemy import exists
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)


class TenancyMode(StrEnum):
    """Whether the deployment hosts more than one tenant."""

    SINGLE = "single"
    MULTI = "multi"


_lock = threading.Lock()
# A positive MULTI detection latches for the process: an organization row never legitimately
# disappears on a live multi-tenant deployment, and un-latching on a transient miss would
# re-open the bypass. SINGLE is cached only for SETTINGS_CACHE_TTL.
_multi_latched = False
_single_until = 0.0


def _testing() -> bool:
    """True under pytest; read at call time, like ``core.settings_cache``."""
    import os

    return os.environ.get("TESTING", "").lower() == "true"


def reset_tenancy_mode_cache() -> None:
    """Forget the cached detection (tests, and a forced-mode change at runtime)."""
    global _multi_latched, _single_until
    with _lock:
        _multi_latched = False
        _single_until = 0.0


def _detect_auto(db: Session) -> tuple[TenancyMode, bool]:
    """Auto mode detection. Returns ``(mode, positive)``.

    ``positive`` is True only when MULTI was established by evidence (a non-community
    edition or an active organization row), never by a swallowed error. Any database error
    returns MULTI with ``positive=False`` so it is neither cached nor latched, and a
    transient outage on a community install does not strip the admin bypass until restart.
    """
    if settings.DEPLOYMENT_EDITION != "community":
        return TenancyMode.MULTI, True
    from app.models.organization import Organization

    try:
        has_org = db.scalar(select(exists().where(Organization.is_active.is_(True))))
    except Exception:
        logger.error("Tenancy mode detection failed; treating as multi-tenant", exc_info=True)
        # The failed statement leaves the caller's transaction aborted; clear it so the
        # request can still run its own queries.
        try:
            db.rollback()
        except Exception:
            logger.debug("rollback after failed tenancy probe also failed", exc_info=True)
        return TenancyMode.MULTI, False
    if has_org:
        return TenancyMode.MULTI, True
    return TenancyMode.SINGLE, True


def tenancy_mode(db: Session) -> TenancyMode:
    """Return the deployment's tenancy mode.

    ``TENANCY_MODE`` of ``single`` / ``multi`` forces the mode; ``auto`` detects it; any
    other value is treated as ``multi`` and logged. A positive MULTI detection latches for
    the process and SINGLE is cached for ``SETTINGS_CACHE_TTL``; the cache is bypassed when
    ``TESTING=true``.
    """
    global _multi_latched, _single_until
    configured = (settings.TENANCY_MODE or "").strip().lower()
    if configured == "single":
        return TenancyMode.SINGLE
    if configured == "multi":
        return TenancyMode.MULTI
    if configured != "auto":
        logger.error("Unknown TENANCY_MODE %r; failing closed to multi-tenant", configured)
        return TenancyMode.MULTI

    if _testing():
        return _detect_auto(db)[0]
    with _lock:
        if _multi_latched:
            return TenancyMode.MULTI
        if time.monotonic() < _single_until:
            return TenancyMode.SINGLE
    mode, positive = _detect_auto(db)
    if positive:
        with _lock:
            if mode is TenancyMode.MULTI:
                _multi_latched = True
            else:
                _single_until = time.monotonic() + max(settings.SETTINGS_CACHE_TTL, 1)
    return mode


def warn_if_forced_single_with_orgs(db: Session) -> None:
    """Startup notice: ``TENANCY_MODE=single`` over a deployment that has active orgs."""
    if (settings.TENANCY_MODE or "").strip().lower() != "single":
        return
    from app.models.organization import Organization

    try:
        has_org = db.scalar(select(exists().where(Organization.is_active.is_(True))))
    except Exception:
        logger.warning("Could not check organizations for the TENANCY_MODE=single notice")
        return
    if has_org:
        logger.warning(
            "TENANCY_MODE=single is forced but active organizations exist: platform admins "
            "keep instance-wide content access and every cross-tenant use is audited "
            "(PLATFORM_ADMIN_CONTENT_ACCESS)."
        )
