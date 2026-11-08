"""Public read-only demo: passwordless sign-in and what the demo says about itself (issue #628).

A demo deployment publishes its credentials, and a failed-password lockout on a shared
account would let a few bad attempts lock every visitor out. ``POST /auth/demo/session``
therefore signs a visitor in as the one demo user without a password, bypassing the password
lockout entirely, and bounds abuse by a per-IP limit instead. It exists only when
``DEMO_MODE`` is on (404 otherwise), refuses any account that is not an ordinary user, and
mints through the same ``_generate_login_tokens`` as every other login, so cookies, refresh
rotation and the ``auth.login.success`` audit event (``auth_method="demo"``) are unchanged.
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.lifecycle_gates import _get_client_info
from app.api.endpoints.auth.login_completion import _generate_login_tokens
from app.auth.audit import audit_logger
from app.auth.rate_limit import get_auth_methods_rate_limit
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.auth.roles import ROLE_USER
from app.core.config import settings
from app.core.demo_info import last_upgrade_at
from app.core.demo_info import next_reset_at
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import DemoAttribution
from app.schemas.user import DemoAttributionsResponse
from app.schemas.user import DemoInfoResponse
from app.schemas.user import Token
from app.services.demo_session_service import find_demo_user
from app.services.demo_session_service import list_demo_attributions

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_demo_mode() -> None:
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.get("/demo", response_model=DemoInfoResponse)
@limiter.limit(get_auth_methods_rate_limit())
def get_demo_info(request: Request, response: Response) -> DemoInfoResponse:
    """Reset time and last-upgrade time for the banner and login page. Public: the login
    page needs it before a session exists. Reports ``enabled: false`` and nothing else
    anywhere but a demo deployment."""
    if not settings.DEMO_MODE:
        return DemoInfoResponse(enabled=False)
    return DemoInfoResponse(
        enabled=True,
        next_reset_at=next_reset_at(datetime.now(UTC)),
        upgraded_at=last_upgrade_at(settings.DEMO_STATUS_FILE),
    )


@router.post("/demo/session", response_model=Token)
@limiter.limit(get_auth_rate_limit())
def start_demo_session(request: Request, response: Response, db: Session = Depends(get_db)) -> Any:
    """Sign a visitor in as the shared read-only demo user, with no password."""
    _require_demo_mode()
    client_ip, user_agent = _get_client_info(request)

    email = settings.DEMO_USER_EMAIL.strip()
    user = find_demo_user(db, email)
    if user is None or not user.is_active:
        logger.error("demo session requested but the demo user is not available")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The demo is not available right now",
        )
    if user.role != ROLE_USER:
        # A passwordless door must never open onto an elevated account.
        logger.error("demo session refused: the demo user is not an ordinary user")
        audit_logger.log_login_failure(
            username=email,
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="DEMO_USER_NOT_ORDINARY",
            auth_method="demo",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The demo is not available right now",
        )

    result: JSONResponse = _generate_login_tokens(
        request,
        db,
        user,
        str(user.uuid),
        str(user.role),
        user_agent,
        client_ip,
        auth_method="demo",
    )
    return result


@router.get("/demo/attributions", response_model=DemoAttributionsResponse)
@limiter.limit(get_auth_rate_limit(), key_func=user_or_ip_key)
def get_demo_attributions(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> DemoAttributionsResponse:
    """Licence and credit for every seeded file the caller owns (CC BY terms require it to
    be shown wherever the file is viewed)."""
    _require_demo_mode()
    items = [DemoAttribution(**row) for row in list_demo_attributions(db, current_user.id)]
    return DemoAttributionsResponse(items=items)
