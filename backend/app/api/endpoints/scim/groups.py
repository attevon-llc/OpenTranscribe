"""``/scim/v2/Groups`` — RFC 7644 over ``user_group`` / ``user_group_member``.

A SCIM group is an OpenTranscribe :class:`~app.models.group.UserGroup`, the same
object the sharing UI uses. Two consequences worth stating:

* ``user_group.owner_id`` is NOT NULL, so a SCIM-created group is owned by the
  administrator who **issued the token**. That keeps it visible and manageable in the
  groups UI instead of belonging to nobody. When that account has since been deleted
  (``scim_token.created_by`` is ``ON DELETE SET NULL``) the oldest ``super_admin`` is
  used, because a group with no owner cannot be stored at all.
* Membership rows are written with ``source='scim'``, which
  ``MEMBERSHIP_SOURCES_PROTECTED`` shields from directory reconciliation — and, in
  the other direction, this router only ever removes ``scim`` rows. Whoever wrote a
  membership owns it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.endpoints.scim.auth import require_scim_token
from app.api.endpoints.scim.errors import SCIMError
from app.api.endpoints.scim.errors import bad_request
from app.api.endpoints.scim.errors import conflict
from app.api.endpoints.scim.errors import not_found
from app.api.endpoints.scim.filters import parse_eq_filter
from app.api.endpoints.scim.filters import parse_pagination
from app.api.endpoints.scim.patch_ops import parse_group_operation
from app.api.endpoints.scim.users import _is_uuid
from app.api.endpoints.scim.users import _scim_json
from app.auth.roles import ROLE_SUPER_ADMIN
from app.db.base import get_db
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.scim_token import SCIMToken
from app.models.user import User
from app.schemas.scim import SCIMGroupRequest
from app.schemas.scim import SCIMPatchRequest
from app.schemas.scim import group_resource
from app.schemas.scim import list_response
from app.services import scim_group_service
from app.services import scim_service

router = APIRouter()
logger = logging.getLogger(__name__)

#: ``GET /Groups?filter=`` accepts only this attribute — ``user_group`` has no
#: ``externalId`` column, and pretending otherwise would return everything.
GROUP_FILTER_ATTRS = ("displayName",)


def _owner_id(db: Session, token: SCIMToken) -> int:
    """Who owns a group this token creates. See the module docstring."""
    if token.created_by is not None:
        return int(token.created_by)
    fallback = db.query(User).filter(User.role == ROLE_SUPER_ADMIN).order_by(User.id).first()
    if fallback is None:  # pragma: no cover - a deployment always seeds one
        raise SCIMError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "No administrator is available to own a SCIM-created group",
        )
    return int(fallback.id)


def _load_group(db: Session, group_id: str) -> UserGroup:
    """Resolve a SCIM group id (our ``user_group.uuid``) or raise 404."""
    group = (
        db.query(UserGroup).filter(UserGroup.uuid == group_id).first()
        if _is_uuid(group_id)
        else None
    )
    if group is None:
        raise not_found("Group", group_id)
    return group


def _members(db: Session, group: UserGroup) -> list[dict[str, str]]:
    """Every member of *group*, as SCIM references, whatever wrote the row."""
    rows = (
        db.query(User)
        .join(UserGroupMember, UserGroupMember.user_id == User.id)
        .filter(UserGroupMember.group_id == group.id)
        .all()
    )
    return [{"value": str(u.uuid), "display": str(u.full_name or u.email)} for u in rows]


def _resolve_member_ids(db: Session, scim_ids: set[str]) -> set[int]:
    """Turn SCIM member ids (account UUIDs) into local primary keys.

    An unknown id is a 400 rather than a silent skip: a connector that thinks it
    added ten people and added eight has no way to notice.
    """
    valid = {i for i in scim_ids if _is_uuid(i)}
    rows = db.query(User).filter(User.uuid.in_(valid)).all() if valid else []
    found = {str(u.uuid) for u in rows}
    missing = scim_ids - found
    if missing:
        raise bad_request(f"Unknown member id(s): {', '.join(sorted(missing))}")
    return {int(u.id) for u in rows}


@router.get("/Groups")
def list_groups(
    startIndex: int = Query(1),  # noqa: N803 - SCIM wire name
    count: int | None = Query(None),
    filter: str | None = Query(None),  # noqa: A002 - SCIM wire name
    db: Session = Depends(get_db),
    _token: SCIMToken = Depends(require_scim_token),
):
    """List or search groups. Supports ``displayName eq``."""
    offset, limit = parse_pagination(startIndex, count)
    query = db.query(UserGroup)

    parsed = parse_eq_filter(filter, allowed=GROUP_FILTER_ATTRS)
    if parsed:
        query = query.filter(UserGroup.name == parsed[1])

    total = query.order_by(None).count()
    rows = query.order_by(UserGroup.id).offset(offset).limit(limit).all()
    return _scim_json(
        list_response(
            [group_resource(g, members=_members(db, g)) for g in rows],
            total=total,
            start_index=offset + 1,
        )
    )


@router.get("/Groups/{group_id}")
def get_group(
    group_id: str,
    db: Session = Depends(get_db),
    _token: SCIMToken = Depends(require_scim_token),
):
    """Fetch one group by SCIM id."""
    group = _load_group(db, group_id)
    return _scim_json(group_resource(group, members=_members(db, group)))


@router.post("/Groups", status_code=status.HTTP_201_CREATED)
def create_group(
    payload: SCIMGroupRequest,
    db: Session = Depends(get_db),
    token: SCIMToken = Depends(require_scim_token),
):
    """Create a group and set its initial membership."""
    if not payload.displayName or not payload.displayName.strip():
        raise bad_request("displayName is required")
    try:
        group = scim_group_service.create_group(
            db,
            display_name=payload.displayName,
            owner_id=_owner_id(db, token),
            actor=str(token.name),
        )
    except scim_service.SCIMConflictError as exc:
        raise conflict(str(exc)) from exc

    member_ids = _resolve_member_ids(db, {m.value for m in payload.members if m.value})
    if member_ids:
        scim_group_service.set_group_members(db, group, member_ids, actor=str(token.name))
    return _scim_json(group_resource(group, members=_members(db, group)), status.HTTP_201_CREATED)


@router.put("/Groups/{group_id}")
def replace_group(
    group_id: str,
    payload: SCIMGroupRequest,
    db: Session = Depends(get_db),
    token: SCIMToken = Depends(require_scim_token),
):
    """Replace a group's name and its SCIM-owned membership."""
    group = _load_group(db, group_id)
    if payload.displayName and payload.displayName.strip() != str(group.name):
        group.name = payload.displayName.strip()  # type: ignore[assignment]
        db.commit()
    scim_group_service.set_group_members(
        db,
        group,
        _resolve_member_ids(db, {m.value for m in payload.members if m.value}),
        actor=str(token.name),
    )
    db.refresh(group)
    return _scim_json(group_resource(group, members=_members(db, group)))


@router.patch("/Groups/{group_id}")
def patch_group(
    group_id: str,
    payload: SCIMPatchRequest,
    db: Session = Depends(get_db),
    token: SCIMToken = Depends(require_scim_token),
):
    """Apply a ``PatchOp`` to a group's membership or name."""
    group = _load_group(db, group_id)
    if not payload.Operations:
        raise bad_request("A PatchOp must carry at least one operation")

    actor = str(token.name)
    for operation in payload.Operations:
        kind, value = parse_group_operation(operation)
        if kind == "display_name":
            group.name = value  # type: ignore[assignment]
            db.commit()
        elif kind == "members_add":
            scim_group_service.add_group_members(
                db, group, _resolve_member_ids(db, value), actor=actor
            )
        elif kind == "members_remove":
            scim_group_service.remove_group_members(
                db, group, _resolve_member_ids(db, value), actor=actor
            )
        else:
            scim_group_service.set_group_members(
                db, group, _resolve_member_ids(db, value), actor=actor
            )

    db.refresh(group)
    return _scim_json(group_resource(group, members=_members(db, group)))


@router.delete("/Groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: str,
    db: Session = Depends(get_db),
    token: SCIMToken = Depends(require_scim_token),
):
    """Delete a group.

    Unlike a user, a group **is** deleted: it holds no content of its own, and its
    membership and collection shares cascade. Nobody's transcripts go with it.
    """
    group = _load_group(db, group_id)
    name = str(group.name)
    db.delete(group)
    db.commit()
    logger.info("SCIM token %r deleted group %s", token.name, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
