"""Admin API for IdP group mappings (``v376``) — super_admin tier.

Mappings decide who is put into which sharing group and who is handed ``admin``
from a directory claim, so they configure how the deployment authorizes, not who
belongs to whose team. That puts them on the same tier as auth config itself
(``api/endpoints/auth/dependencies.get_current_active_superuser``), and
``tests/unit/test_route_privilege_tiers.py`` pins the prefix so a later route
cannot quietly land lower.

Surface:

- ``GET    /admin/group-mappings``            list, optionally filtered by source
- ``POST   /admin/group-mappings``            create
- ``PUT    /admin/group-mappings/{uuid}``     update
- ``DELETE /admin/group-mappings/{uuid}``     delete
- ``POST   /admin/group-mappings/test``       resolve a claim list, or a real LDAP
  user, without changing anything
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_USER
from app.db.base import get_db
from app.models.group import MAPPING_SOURCE_LDAP
from app.models.group import MEMBERSHIP_SOURCE_MANUAL
from app.models.group import GroupMapping
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.user import User
from app.schemas.group_mapping import GroupMapping as GroupMappingSchema
from app.schemas.group_mapping import GroupMappingCreate
from app.schemas.group_mapping import GroupMappingUpdate
from app.schemas.group_mapping import MappingTestGroup
from app.schemas.group_mapping import MappingTestRequest
from app.schemas.group_mapping import MappingTestResponse
from app.services.idp_group_mapping_service import RoleNotGrantableError
from app.services.idp_group_mapping_service import assert_grantable_role
from app.services.idp_group_mapping_service import normalize_claim_value
from app.services.idp_group_mapping_service import resolve_grants
from app.utils.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def _audit(request: Request, actor: User, action: str, details: dict) -> None:
    """Record a mapping change — it alters who gets admin, so it is auditable."""
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"setting": "group_mapping", "action": action, **details},
    )


def _member_counts(db: Session, mappings: list[GroupMapping]) -> dict[int, int]:
    """Directory-derived membership counts per target group, for the list view."""
    group_ids = {int(m.user_group_id) for m in mappings if m.user_group_id is not None}
    if not group_ids:
        return {}
    rows = (
        db.query(UserGroupMember.group_id, UserGroupMember.source)
        .filter(
            UserGroupMember.group_id.in_(group_ids),
            UserGroupMember.source != MEMBERSHIP_SOURCE_MANUAL,
        )
        .all()
    )
    counts: dict[int, int] = {}
    for group_id, _source in rows:
        counts[int(group_id)] = counts.get(int(group_id), 0) + 1
    return counts


def _to_schema(mapping: GroupMapping, member_count: int = 0) -> GroupMappingSchema:
    group = mapping.user_group
    assert mapping.created_at is not None  # server_default=now()
    assert mapping.updated_at is not None  # server_default=now()
    return GroupMappingSchema(
        uuid=mapping.uuid,
        source=str(mapping.source),
        claim_value=str(mapping.claim_value),
        group_uuid=group.uuid if group else None,
        group_name=group.name if group else None,
        grants_role=str(mapping.grants_role) if mapping.grants_role else None,
        description=mapping.description,
        member_count=member_count,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


def _resolve_group(db: Session, group_uuid) -> UserGroup | None:
    if group_uuid is None:
        return None
    return get_by_uuid(db, UserGroup, str(group_uuid), "Group not found")


def _assert_claim_free(db: Session, source: str, claim_value: str, exclude_id: int | None) -> None:
    """Reject a duplicate claim under the same matching rule the resolver uses.

    ``uq_group_mapping_source_claim`` catches the exact-string case, and for LDAP
    ``uq_group_mapping_ldap_claim_ci`` catches the case-only variant — this turns
    both into a 400 with a readable message instead of a 500 from the driver.
    """
    normalized = normalize_claim_value(source, claim_value)
    for existing in db.query(GroupMapping).filter(GroupMapping.source == source).all():
        if exclude_id is not None and existing.id == exclude_id:
            continue
        if normalize_claim_value(source, str(existing.claim_value)) == normalized:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"A {source} mapping for '{existing.claim_value}' already exists",
            )


@router.get("", response_model=list[GroupMappingSchema])
def list_group_mappings(
    source: str | None = Query(None, pattern="^(ldap|oidc)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """List every configured mapping, newest last."""
    query = db.query(GroupMapping)
    if source:
        query = query.filter(GroupMapping.source == source)
    mappings = query.order_by(GroupMapping.source, GroupMapping.id).all()
    counts = _member_counts(db, mappings)
    return [
        _to_schema(m, counts.get(int(m.user_group_id), 0) if m.user_group_id else 0)
        for m in mappings
    ]


@router.post("", response_model=GroupMappingSchema, status_code=status.HTTP_201_CREATED)
def create_group_mapping(
    request: Request,
    payload: GroupMappingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Create a mapping. ``super_admin`` is refused here as well as by the DB CHECK."""
    try:
        grants_role = assert_grantable_role(payload.grants_role)
    except RoleNotGrantableError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    group = _resolve_group(db, payload.group_uuid)
    _assert_claim_free(db, payload.source, payload.claim_value, exclude_id=None)

    mapping = GroupMapping(
        source=payload.source,
        claim_value=payload.claim_value.strip(),
        user_group_id=group.id if group else None,
        grants_role=grants_role,
        description=payload.description,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)

    _audit(
        request,
        current_user,
        "create",
        {
            "mapping_uuid": str(mapping.uuid),
            "source": payload.source,
            "claim_value": payload.claim_value,
            "group": group.name if group else None,
            "grants_role": grants_role,
        },
    )
    return _to_schema(mapping)


@router.put("/{mapping_uuid}", response_model=GroupMappingSchema)
def update_group_mapping(
    request: Request,
    mapping_uuid: str,
    payload: GroupMappingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Update a mapping. The "grants something" rule is re-checked after the patch."""
    mapping = get_by_uuid(db, GroupMapping, mapping_uuid, "Mapping not found")
    fields = payload.model_dump(exclude_unset=True)

    if "grants_role" in fields:
        try:
            mapping.grants_role = assert_grantable_role(fields["grants_role"])
        except RoleNotGrantableError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if "group_uuid" in fields:
        group = _resolve_group(db, fields["group_uuid"])
        mapping.user_group_id = group.id if group else None
    if "claim_value" in fields and fields["claim_value"]:
        _assert_claim_free(db, str(mapping.source), fields["claim_value"], exclude_id=mapping.id)
        mapping.claim_value = fields["claim_value"].strip()
    if "description" in fields:
        mapping.description = fields["description"]

    if mapping.user_group_id is None and mapping.grants_role is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A mapping must set group_uuid, grants_role, or both",
        )

    db.commit()
    db.refresh(mapping)
    _audit(request, current_user, "update", {"mapping_uuid": mapping_uuid, **fields})
    return _to_schema(mapping)


@router.delete("/{mapping_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group_mapping(
    request: Request,
    mapping_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Delete a mapping.

    Memberships it produced are NOT removed here — they carry the directory
    ``source`` and the next reconciliation (login or sweep) drops them, because a
    deleted mapping resolves to nothing. Deleting them synchronously would also
    take out rows another mapping still justifies.
    """
    mapping = get_by_uuid(db, GroupMapping, mapping_uuid, "Mapping not found")
    detail = {
        "mapping_uuid": mapping_uuid,
        "source": str(mapping.source),
        "claim_value": str(mapping.claim_value),
    }
    db.delete(mapping)
    db.commit()
    _audit(request, current_user, "delete", detail)
    return None


def _ldap_claims_for(db: Session, username: str) -> tuple[list[str], bool]:
    """Ask the directory for one user's groups, reusing the auth bind and search."""
    from app.auth.ldap_auth import DIRECTORY_ABSENT
    from app.auth.ldap_auth import LdapConfig
    from app.auth.ldap_auth import LdapDirectoryUnavailableError
    from app.auth.ldap_auth import ldap_directory_session
    from app.auth.ldap_auth import probe_ldap_user

    cfg = LdapConfig.from_db(db)
    if not cfg.enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="LDAP is not enabled")
    try:
        with ldap_directory_session(cfg) as conn:
            probe = probe_ldap_user(cfg, conn, username)
    except LdapDirectoryUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Directory could not be consulted: {exc}",
        ) from exc
    if probe.status == DIRECTORY_ABSENT:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found in the directory"
        )
    return list(probe.groups), probe.is_admin


@router.post("/test", response_model=MappingTestResponse)
def test_group_mapping(
    payload: MappingTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Show what a claim list — or a real LDAP account — would resolve to.

    Nothing is written. ``username`` is LDAP-only: an OIDC provider asserts group
    membership inside a token issued to the user, and there is no provider-neutral
    way to look it up for somebody else.
    """
    legacy_admin = False
    if payload.username:
        if payload.source != MAPPING_SOURCE_LDAP:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Looking a user up by name is LDAP-only; for OIDC, paste the role "
                    "or group values the provider emits."
                ),
            )
        claims, legacy_admin = _ldap_claims_for(db, payload.username)
    else:
        claims = list(payload.claim_values or [])

    grants = resolve_grants(db, payload.source, claims)
    groups = (
        db.query(UserGroup).filter(UserGroup.id.in_(grants.group_ids)).all()
        if grants.group_ids
        else []
    )
    matched = set(grants.matched_claims)
    effective = ROLE_ADMIN if (legacy_admin or grants.grants_admin) else ROLE_USER
    return MappingTestResponse(
        source=payload.source,
        claim_values=claims,
        matched_claims=list(grants.matched_claims),
        unmatched_claims=[c for c in claims if c not in matched],
        groups=[MappingTestGroup(uuid=g.uuid, name=g.name) for g in groups],
        grants_role=grants.role,
        legacy_admin=legacy_admin,
        effective_role=effective,
    )
