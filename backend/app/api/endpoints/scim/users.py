"""``/scim/v2/Users`` — RFC 7644 §3.2–§3.6.

Transport only: parse, page, render. Every write goes through
``services/scim_service.py``, which is where the rules live (no ``super_admin``, no
role writes, deactivation revokes sessions, delete means disable).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.endpoints.scim.auth import require_scim_token
from app.api.endpoints.scim.errors import SCIM_CONTENT_TYPE
from app.api.endpoints.scim.errors import SCIMError
from app.api.endpoints.scim.errors import bad_request
from app.api.endpoints.scim.errors import conflict
from app.api.endpoints.scim.errors import not_found
from app.api.endpoints.scim.filters import parse_eq_filter
from app.api.endpoints.scim.filters import parse_pagination
from app.api.endpoints.scim.patch_ops import build_user_update
from app.db.base import get_db
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.scim_token import SCIMToken
from app.models.user import User
from app.schemas.scim import SCIMPatchRequest
from app.schemas.scim import SCIMUserRequest
from app.schemas.scim import list_response
from app.schemas.scim import user_resource
from app.services import scim_service

router = APIRouter()
logger = logging.getLogger(__name__)

#: The attributes ``GET /Users?filter=`` accepts. See ``filters.py`` for why the
#: supported grammar is one production rather than a partial implementation.
USER_FILTER_ATTRS = ("userName", "externalId")


def _scim_json(body: dict[str, Any], status_code: int = status.HTTP_200_OK) -> JSONResponse:
    """Render a SCIM body with the media type RFC 7644 §3.1 requires."""
    return JSONResponse(status_code=status_code, content=body, media_type=SCIM_CONTENT_TYPE)


def _group_refs(db: Session, user: User) -> list[dict[str, str]]:
    """The user's group memberships, as SCIM resource references."""
    rows = (
        db.query(UserGroup)
        .join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
        .filter(UserGroupMember.user_id == user.id)
        .all()
    )
    return [{"value": str(g.uuid), "display": str(g.name)} for g in rows]


def _load_user(db: Session, user_id: str) -> User:
    """Resolve a SCIM ``id`` (our account UUID) or raise 404."""
    user = db.query(User).filter(User.uuid == user_id).first() if _is_uuid(user_id) else None
    if user is None:
        raise not_found("User", user_id)
    return user


def _is_uuid(value: str) -> bool:
    """Whether *value* parses as a UUID.

    A malformed id must be a clean 404, not a ``DataError`` wrapped in a 500 — an IdP
    retries a 404 and escalates a 500.
    """
    from uuid import UUID

    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _translate(exc: Exception) -> SCIMError:
    """Map a service-layer refusal onto its SCIM error."""
    if isinstance(exc, scim_service.SCIMConflictError):
        return conflict(str(exc))
    return SCIMError(status.HTTP_403_FORBIDDEN, str(exc), scim_type="mutability")


@router.get("/Users")
def list_users(
    request: Request,
    startIndex: int = Query(1),  # noqa: N803 - SCIM wire name
    count: int | None = Query(None),
    filter: str | None = Query(None),  # noqa: A002 - SCIM wire name
    db: Session = Depends(get_db),
    _token: SCIMToken = Depends(require_scim_token),
):
    """List or search users. Supports ``userName eq`` / ``externalId eq``."""
    del request
    offset, limit = parse_pagination(startIndex, count)
    query = db.query(User)

    parsed = parse_eq_filter(filter, allowed=USER_FILTER_ATTRS)
    if parsed:
        attribute, value = parsed
        if attribute == "userName":
            query = query.filter(User.email == value.strip().lower())
        else:
            query = query.filter(User.external_id == value)

    total = query.order_by(None).count()
    rows = query.order_by(User.id).offset(offset).limit(limit).all()
    return _scim_json(
        list_response(
            [user_resource(u, groups=_group_refs(db, u)) for u in rows],
            total=total,
            start_index=offset + 1,
        )
    )


@router.get("/Users/{user_id}")
def get_user(
    user_id: str,
    db: Session = Depends(get_db),
    _token: SCIMToken = Depends(require_scim_token),
):
    """Fetch one user by SCIM id."""
    user = _load_user(db, user_id)
    return _scim_json(user_resource(user, groups=_group_refs(db, user)))


@router.post("/Users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: SCIMUserRequest,
    db: Session = Depends(get_db),
    token: SCIMToken = Depends(require_scim_token),
):
    """Provision a new account.

    An address that already exists is a **409**, never a silent update: RFC 7644
    §3.3 requires it, and every IdP treats the 409 as "already provisioned, switch to
    PATCH" rather than as a failure.
    """
    email = payload.resolved_email()
    if not email:
        raise bad_request("userName must be an email address, or emails must contain one")

    try:
        user = scim_service.create_user(
            db,
            email=email,
            display_name=payload.resolved_display_name(),
            external_id=payload.externalId,
            active=True if payload.active is None else bool(payload.active),
            actor=str(token.name),
        )
    except (scim_service.SCIMConflictError, scim_service.SCIMForbiddenError) as exc:
        raise _translate(exc) from exc
    return _scim_json(user_resource(user, groups=[]), status.HTTP_201_CREATED)


@router.put("/Users/{user_id}")
def replace_user(
    user_id: str,
    payload: SCIMUserRequest,
    db: Session = Depends(get_db),
    token: SCIMToken = Depends(require_scim_token),
):
    """Replace a user's mutable attributes."""
    user = _load_user(db, user_id)
    try:
        updated = scim_service.update_user(
            db,
            user,
            email=payload.resolved_email(),
            display_name=payload.resolved_display_name(),
            external_id=payload.externalId,
            active=payload.active,
            actor=str(token.name),
        )
    except (scim_service.SCIMConflictError, scim_service.SCIMForbiddenError) as exc:
        raise _translate(exc) from exc
    return _scim_json(user_resource(updated, groups=_group_refs(db, updated)))


@router.patch("/Users/{user_id}")
def patch_user(
    user_id: str,
    payload: SCIMPatchRequest,
    db: Session = Depends(get_db),
    token: SCIMToken = Depends(require_scim_token),
):
    """Apply a ``PatchOp``.

    The supported operation set is closed and documented in
    ``api/endpoints/scim/patch_ops.py``; anything outside it is a 400
    ``invalidPath`` rather than a 200 that changed nothing.
    """
    user = _load_user(db, user_id)
    if not payload.Operations:
        raise bad_request("A PatchOp must carry at least one operation")

    updates = build_user_update(payload.Operations)
    # `user` has one name column, so the two name parts collapse into it. Doing that
    # here rather than in patch_ops keeps the parser free of schema knowledge.
    given = updates.pop("given_name", None)
    family = updates.pop("family_name", None)
    if given is not None or family is not None:
        current = str(user.full_name or "")
        current_given, _, current_family = current.partition(" ")
        merged = " ".join(
            p for p in (given or current_given, family or current_family) if p
        ).strip()
        if merged:
            updates.setdefault("display_name", merged)

    try:
        updated = scim_service.update_user(db, user, actor=str(token.name), **updates)
    except (scim_service.SCIMConflictError, scim_service.SCIMForbiddenError) as exc:
        raise _translate(exc) from exc
    return _scim_json(user_resource(updated, groups=_group_refs(db, updated)))


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    token: SCIMToken = Depends(require_scim_token),
):
    """**Soft-disable** the account and revoke its sessions.

    Not a row deletion. Open WebUI's equivalent is documented as "deactivate" and
    performs a cascading hard delete that destroys the user's content; a connector
    dropping someone from its assignment scope must not be able to erase their
    transcripts. Real erasure is ``gdpr_erasure_service``, behind an explicit
    administrator action.
    """
    user = _load_user(db, user_id)
    try:
        scim_service.update_user(db, user, active=False, actor=str(token.name))
    except (scim_service.SCIMConflictError, scim_service.SCIMForbiddenError) as exc:
        raise _translate(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
