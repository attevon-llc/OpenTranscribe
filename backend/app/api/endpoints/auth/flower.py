"""Authorization probe for the Flower dashboard reverse proxy.

The Flower UI is served by nginx at ``/flower/``, not by this app, so it cannot
carry a FastAPI dependency of its own. nginx therefore gates it with
``auth_request /api/auth/flower-authz;`` — a subrequest that forwards the
caller's headers (including the httpOnly session cookie) and allows the request
only on a 2xx.

Flower exposes task names with their arguments (file and user IDs) plus the
full worker topology, and the reverse proxy injects Flower's Basic-Auth
credentials for every ``/flower/`` request, so without this gate the dashboard
is readable by anyone who can load the origin. Admin and super_admin only.
"""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from fastapi import status

from app.api.endpoints.auth.dependencies import get_current_admin_user
from app.api.endpoints.auth.dependencies import get_current_user
from app.models.user import User

router = APIRouter()


@router.get(
    "/flower-authz",
    status_code=status.HTTP_200_OK,
    summary="nginx auth_request gate for the Flower dashboard (admin only)",
)
def flower_authz(current_user: User = Depends(get_current_user)) -> Response:
    """Allow the Flower dashboard for admins; deny everyone else with 401.

    Args:
        current_user: Resolved from the Bearer header or the httpOnly access
            cookie. An anonymous caller never reaches this body — the
            dependency already raises 401.

    Returns:
        An empty 200 response. nginx discards the body and only reads the
        status, so nothing is serialized on the hot path (this fires once per
        Flower asset request).

    Raises:
        HTTPException: 401 when the caller is authenticated but not an
            admin/super_admin. ``get_current_admin_user`` raises 403 there;
            nginx's ``auth_request`` treats only 401 as "not authenticated" and
            forwards 403 verbatim, so the denial is normalized to 401.
    """
    try:
        get_current_admin_user(current_user)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        raise
    return Response(status_code=status.HTTP_200_OK)
