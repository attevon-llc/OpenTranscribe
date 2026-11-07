"""Super-admin allowlist of private-network media hosts (``/admin/media-private-hosts``)."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_superuser
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.db.base import get_db
from app.services import media_private_hosts
from app.utils.media_private_hosts import get_private_hosts as read_private_hosts

logger = logging.getLogger(__name__)

router = APIRouter()


class PrivateHosts(BaseModel):
    """The allowlist, as normalised ``scheme://host[:port]`` strings."""

    hosts: list[str] = Field(default_factory=list, max_length=media_private_hosts.MAX_HOSTS)


@router.get("/media-private-hosts", response_model=PrivateHosts)
def get_private_hosts(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> PrivateHosts:
    """Return the private hosts media sources may use."""
    return PrivateHosts(hosts=read_private_hosts(db))


@router.put("/media-private-hosts", response_model=PrivateHosts)
def put_private_hosts(
    request: Request,
    body: PrivateHosts,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> PrivateHosts:
    """Replace the allowlist."""
    try:
        stored = media_private_hosts.set_private_hosts(db, body.hosts)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=request.client.host if request.client else None,
        details={"area": "media_sources.private_hosts", "hosts": stored},
    )
    logger.info("Super admin %s set %d private media host(s)", current_user.email, len(stored))
    return PrivateHosts(hosts=stored)
