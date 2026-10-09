"""``PlatformBypass`` and the chokepoints that evaluate it (issue #1122).

The bypass is decided on the LOADED row with the row's own tenant. Single-tenant mode keeps
the instance-wide admin access exactly; multi-tenant mode grants no implicit content access.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import FileStatus
from app.models.media import Collection
from app.models.media import MediaFile
from app.models.organization import Organization
from app.services.platform_access import TenancyMode
from app.services.platform_access import reset_tenancy_mode_cache
from app.services.platform_bypass import PlatformBypass
from app.services.platform_bypass import audit_metadata_access
from app.services.platform_bypass import build_bypass
from app.utils.uuid_helpers import get_collection_by_uuid_with_permission
from app.utils.uuid_helpers import get_collection_by_uuid_with_sharing
from app.utils.uuid_helpers import get_file_by_uuid_with_permission
from app.utils.uuid_helpers import require_resource_owner

_AUDIT = "app.auth.audit.audit_logger.log"


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "auto")
    monkeypatch.setattr(settings, "DEPLOYMENT_EDITION", "community")
    reset_tenancy_mode_cache()
    yield
    reset_tenancy_mode_cache()


def _org(db_session) -> Organization:
    org = Organization(name=f"bypass-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.flush()
    return org


def _file(db_session, owner, organization_id=None) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"bypass_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _allows(bypass, row, need="read"):
    return bypass.allows(
        org_id=row.organization_id,
        owner_id=row.user_id,
        need=need,
        resource_type="media_file",
        resource_uuid=str(row.uuid),
    )


def test_none_allows_nothing_and_exposes_no_reach(admin_user, normal_user, db_session):
    row = _file(db_session, normal_user)
    bypass = PlatformBypass.none()
    assert not _allows(bypass, row)
    assert not bypass.sees_all_in_scope
    assert not bypass.admin_reveal_allowed
    assert not bypass.user_is_admin


def test_a_hand_built_context_fails_closed(admin_user):
    from app.api.deps_context import RequestContext

    ctx = RequestContext(user=admin_user)
    assert ctx.bypass == PlatformBypass.none()


def test_single_mode_admin_reaches_any_row(admin_user, normal_user, db_session):
    row = _file(db_session, normal_user)
    bypass = build_bypass(db_session, admin_user, None)
    assert bypass.mode is TenancyMode.SINGLE
    assert _allows(bypass, row, "read")
    assert _allows(bypass, row, "write")
    assert bypass.sees_all_in_scope
    assert bypass.admin_reveal_allowed


def test_single_mode_non_admin_gets_nothing(normal_user, other_user, db_session):
    row = _file(db_session, other_user)
    bypass = build_bypass(db_session, normal_user, None)
    assert not _allows(bypass, row)
    assert not bypass.sees_all_in_scope
    assert not bypass.admin_reveal_allowed


def test_multi_mode_admin_gets_no_implicit_access(admin_user, normal_user, db_session):
    org = _org(db_session)
    org_row = _file(db_session, normal_user, organization_id=org.id)
    personal_row = _file(db_session, normal_user)
    bypass = build_bypass(db_session, admin_user, None)
    assert bypass.mode is TenancyMode.MULTI
    assert not _allows(bypass, org_row, "read")
    assert not _allows(bypass, personal_row, "read")
    assert not _allows(bypass, personal_row, "write")
    assert not bypass.sees_all_in_scope
    assert not bypass.admin_reveal_allowed
    # Quarantine review stays an admin capability; it never widens tenant scope.
    assert bypass.user_is_admin


def test_forced_multi_removes_the_bypass_on_a_community_install(
    admin_user, normal_user, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "TENANCY_MODE", "multi")
    row = _file(db_session, normal_user)
    assert not _allows(build_bypass(db_session, admin_user, None), row)


def test_build_bypass_does_not_touch_the_database_for_a_non_admin(normal_user):
    db = MagicMock(spec=Session)
    db.scalar.side_effect = AssertionError("a non-admin must not trigger a tenancy probe")

    assert build_bypass(db, normal_user, None).user_is_admin is False
    db.scalar.assert_not_called()


# --- forced single over live orgs: allowed, but never silent -------------------------------


def test_forced_single_with_orgs_audits_a_cross_tenant_use_once(
    admin_user, normal_user, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    org = _org(db_session)
    row = _file(db_session, normal_user, organization_id=org.id)
    bypass = build_bypass(db_session, admin_user, None)
    assert bypass.audit_cross_tenant

    with patch(_AUDIT) as audit:
        assert _allows(bypass, row)
        assert _allows(bypass, row)  # same resource, same request: one event

    assert audit.call_count == 1
    event, _outcome = audit.call_args.args[:2]
    assert event == "platform_admin.content.access"
    kwargs = audit.call_args.kwargs
    assert kwargs["user_id"] == admin_user.id
    assert kwargs["organization_id"] == org.id
    assert kwargs["target_user_id"] == normal_user.id
    assert kwargs["details"]["resource_uuid"] == str(row.uuid)
    assert "filename" not in kwargs["details"]


def test_forced_single_same_tenant_use_is_not_audited(
    admin_user, normal_user, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    org = _org(db_session)
    row = _file(db_session, normal_user, organization_id=org.id)
    bypass = build_bypass(db_session, admin_user, org.id)
    with patch(_AUDIT) as audit:
        assert _allows(bypass, row)
    audit.assert_not_called()


def test_forced_single_without_orgs_emits_nothing_new(
    admin_user, normal_user, db_session, monkeypatch
):
    """Community byte-for-byte: no organization rows means no new audit traffic."""
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    row = _file(db_session, normal_user)
    bypass = build_bypass(db_session, admin_user, None)
    assert not bypass.audit_cross_tenant
    with patch(_AUDIT) as audit:
        assert _allows(bypass, row)
    audit.assert_not_called()


def test_auto_single_never_audits(admin_user, normal_user, db_session):
    row = _file(db_session, normal_user)
    bypass = build_bypass(db_session, admin_user, None)
    with patch(_AUDIT) as audit:
        assert _allows(bypass, row)
    audit.assert_not_called()


# --- metadata access audit ------------------------------------------------------------------


def test_metadata_access_is_audited_in_multi_mode_with_ids_only(admin_user, db_session):
    org = _org(db_session)
    with patch(_AUDIT) as audit:
        emitted = audit_metadata_access(
            db_session,
            admin_user,
            route="/api/admin/files/quarantined",
            count=3,
            organization_ids=[org.id, org.id, None],
        )
    assert emitted is True
    assert audit.call_count == 1
    assert audit.call_args.args[0] == "platform_admin.metadata.access"
    assert audit.call_args.kwargs["user_id"] == admin_user.id
    assert audit.call_args.kwargs["details"] == {
        "route": "/api/admin/files/quarantined",
        "count": 3,
        "organization_ids": [org.id],
    }


def test_metadata_access_is_silent_in_single_mode(admin_user, db_session):
    with patch(_AUDIT) as audit:
        emitted = audit_metadata_access(
            db_session, admin_user, route="/x", count=1, organization_ids=[None]
        )
    assert emitted is False
    audit.assert_not_called()


# --- the chokepoints evaluate the bypass on the loaded row ----------------------------------


def test_file_helper_single_mode_admin_passes_a_foreign_tenant_file(
    admin_user, normal_user, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")  # forced: an org exists below
    org = _org(db_session)
    row = _file(db_session, normal_user, organization_id=org.id)
    bypass = build_bypass(db_session, admin_user, None)
    found = get_file_by_uuid_with_permission(
        db_session, row.uuid, admin_user.id, bypass=bypass, organization_id=None
    )
    assert found.id == row.id


def test_file_helper_multi_mode_admin_is_refused_with_403(admin_user, normal_user, db_session):
    org = _org(db_session)
    row = _file(db_session, normal_user, organization_id=org.id)
    bypass = build_bypass(db_session, admin_user, None)
    assert bypass.mode is TenancyMode.MULTI
    with pytest.raises(HTTPException) as exc:
        get_file_by_uuid_with_permission(
            db_session, row.uuid, admin_user.id, bypass=bypass, organization_id=None
        )
    assert exc.value.status_code == 403


def test_file_helper_without_a_bypass_refuses_even_a_single_mode_admin(
    admin_user, normal_user, db_session
):
    """Nothing reaches the platform role by default; a caller must hand the bypass in."""
    row = _file(db_session, normal_user)
    with pytest.raises(HTTPException) as exc:
        get_file_by_uuid_with_permission(db_session, row.uuid, admin_user.id, organization_id=None)
    assert exc.value.status_code == 403


def test_file_helper_owner_still_passes_in_multi_mode(normal_user, db_session):
    _org(db_session)
    row = _file(db_session, normal_user)
    found = get_file_by_uuid_with_permission(
        db_session,
        row.uuid,
        normal_user.id,
        bypass=build_bypass(db_session, normal_user, None),
        organization_id=None,
    )
    assert found.id == row.id


def _collection(db_session, owner, organization_id=None) -> Collection:
    collection = Collection(
        user_id=owner.id, organization_id=organization_id, name=f"c-{uuid.uuid4().hex[:8]}"
    )
    db_session.add(collection)
    db_session.flush()
    return collection


def test_collection_helpers_single_mode_admin_is_owner(admin_user, normal_user, db_session):
    collection = _collection(db_session, normal_user)
    bypass = build_bypass(db_session, admin_user, None)
    assert (
        get_collection_by_uuid_with_permission(
            db_session, collection.uuid, admin_user.id, bypass=bypass, organization_id=None
        ).id
        == collection.id
    )
    _found, level = get_collection_by_uuid_with_sharing(
        db_session,
        collection.uuid,
        admin_user.id,
        "editor",
        bypass=bypass,
        organization_id=None,
    )
    assert level == "owner"


def test_collection_helpers_multi_mode_admin_is_refused(admin_user, normal_user, db_session):
    org = _org(db_session)
    collection = _collection(db_session, normal_user, organization_id=org.id)
    bypass = build_bypass(db_session, admin_user, None)
    with pytest.raises(HTTPException) as exc:
        get_collection_by_uuid_with_permission(
            db_session, collection.uuid, admin_user.id, bypass=bypass, organization_id=None
        )
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        get_collection_by_uuid_with_sharing(
            db_session, collection.uuid, admin_user.id, bypass=bypass, organization_id=None
        )
    assert exc2.value.status_code == 403


def test_require_resource_owner_bypass_follows_the_mode(
    admin_user, normal_user, db_session, monkeypatch
):
    row = _file(db_session, normal_user)
    bypass = build_bypass(db_session, admin_user, None)
    # single mode: admin passes the owner gate when the caller hands the bypass in
    require_resource_owner(row, admin_user, forbidden_detail="no", bypass=bypass)
    # no bypass handed in: refused, as before
    with pytest.raises(HTTPException):
        require_resource_owner(row, admin_user, forbidden_detail="no")
    # multi mode: refused even with the bypass
    _org(db_session)
    multi = build_bypass(db_session, admin_user, None)
    with pytest.raises(HTTPException):
        require_resource_owner(row, admin_user, forbidden_detail="no", bypass=multi)
