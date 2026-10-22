"""The signed-in user's own takedown history (issue #1254).

A taken-down file 404s for its owner on every surface, and the live in-app notice is a
publish with no server-side copy, so an owner who had no open session when it was sent
would otherwise never learn of the takedown or how to dispute it. This read route is the
durable copy.

The subject is the authenticated user and nothing else: there is no user parameter. The
response model has no field for the internal basis, the requester reference or the handler.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import TAKEDOWN_OWNER_READ_RATE_LIMIT
from app.db.base import get_db
from app.schemas.takedown_notice import OwnerTakedownList
from app.services.takedown_notice_queries import owner_takedowns

router = APIRouter()


@router.get("/mine", response_model=OwnerTakedownList)
@limiter.limit(TAKEDOWN_OWNER_READ_RATE_LIMIT, key_func=user_or_ip_key)
def list_my_takedowns(
    request: Request,
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """Takedowns of the caller's own files: why, when, the status, and where to dispute."""
    takedowns, total = owner_takedowns(db, ctx.user.uuid, limit=limit, offset=offset)
    return OwnerTakedownList(takedowns=takedowns, total=total)
