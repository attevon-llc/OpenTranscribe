"""The ``require_capability`` router dependency: the HTTP adapter over ``app.core.capabilities``."""

import logging
from collections.abc import Callable

from fastapi import Request

from app.core.capabilities import NO_PLATFORM_BYPASS
from app.core.capabilities import capability_enabled
from app.core.capabilities import is_platform_staff
from app.core.db_overload import reraise_db_overload
from app.core.exceptions import ResourceNotFoundError

logger = logging.getLogger(__name__)


def require_capability(key: str, *, platform_admin_bypass: bool = True) -> Callable:
    """FastAPI dependency factory: 404 when the capability is off.

    404 (not 403) on purpose — a disabled feature surface should not exist
    at all for this deployment, exactly like an unknown route.

    ``platform_admin_bypass`` (default on) lets platform staff
    (``is_platform_staff``) through even when the capability is hidden from
    tenants — cloud tenants can never become superusers (external_sync never
    grants it), so this exactly implements the capabilities matrix's
    "invisible to org users / available to platform staff" column. Turning it
    off is only allowed for a key declared in ``NO_PLATFORM_BYPASS`` (ValueError
    otherwise), because ``get_capabilities_for_viewer`` must know not to overlay it.
    """
    if not platform_admin_bypass and key not in NO_PLATFORM_BYPASS:
        raise ValueError(
            f"require_capability({key!r}, platform_admin_bypass=False): declare the key in "
            "NO_PLATFORM_BYPASS so the viewer overlay does not show it to platform staff"
        )

    def _dependency(request: Request) -> None:
        if capability_enabled(key, request):
            return
        if platform_admin_bypass:
            try:
                from app.api.endpoints.auth import get_optional_current_user
                from app.db.base import SessionLocal

                db = SessionLocal()
                try:
                    if is_platform_staff(get_optional_current_user(request, db)):
                        return
                finally:
                    db.close()
            except Exception as exc:
                # Bypass is best-effort and falls through to 404, except that a database
                # overload is answered 503 (retryable), not a false "feature absent".
                reraise_db_overload(exc)
                logger.debug("platform-admin bypass check failed", exc_info=True)
        raise ResourceNotFoundError("Not Found")

    return _dependency
