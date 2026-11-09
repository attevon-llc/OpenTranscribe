"""``app/utils/db_helpers.py`` — the tenant-scope choke point and query builders.

These are exercised against the real Postgres savepoint (``db_session``) rather
than a mocked ``Session``/``Query``, so a wrong filter clause actually fails the
assertion instead of a mock silently accepting whatever was called on it.
"""

from __future__ import annotations

import uuid

from app.core.enums import FileStatus
from app.core.tenancy import UNSCOPED
from app.models.media import MediaFile
from app.models.organization import Organization
from app.utils import db_helpers


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _make_org(db_session) -> Organization:
    org = Organization(name=f"org-{_suffix()}")
    db_session.add(org)
    db_session.flush()
    return org


def _make_file(db_session, owner, *, status=FileStatus.COMPLETED, **kwargs) -> MediaFile:
    file_uuid = uuid.uuid4()
    defaults = dict(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"dbhelpers-{file_uuid.hex[:8]}.wav",
        storage_path=f"dbhelpers-test/{file_uuid.hex}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=status,
    )
    defaults.update(kwargs)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.flush()
    return media_file


# ---------------------------------------------------------------------------
# apply_tenant_scope
# ---------------------------------------------------------------------------


def test_apply_tenant_scope_unscoped_filters_by_user_only(db_session, normal_user, admin_user):
    mine = _make_file(db_session, normal_user)
    _make_file(db_session, admin_user)

    query = db_helpers.apply_tenant_scope(
        db_session.query(MediaFile), MediaFile, user_id=normal_user.id, organization_id=UNSCOPED
    )
    results = query.all()

    assert [f.id for f in results] == [mine.id]


def test_apply_tenant_scope_explicit_org_filters_by_org(db_session, normal_user, admin_user):
    org_a = _make_org(db_session)
    org_b = _make_org(db_session)
    org_file = _make_file(db_session, normal_user, organization_id=org_a.id)
    _make_file(db_session, normal_user, organization_id=None)
    _make_file(db_session, admin_user, organization_id=org_b.id)

    query = db_helpers.apply_tenant_scope(
        db_session.query(MediaFile), MediaFile, user_id=normal_user.id, organization_id=org_a.id
    )
    results = query.all()

    assert [f.id for f in results] == [org_file.id]


def test_apply_tenant_scope_none_is_personal_scope_not_org(db_session, normal_user):
    """organization_id=None must mean 'this user's personal rows', not 'ignore org'."""
    org = _make_org(db_session)
    personal = _make_file(db_session, normal_user, organization_id=None)
    _make_file(db_session, normal_user, organization_id=org.id)

    query = db_helpers.apply_tenant_scope(
        db_session.query(MediaFile), MediaFile, user_id=normal_user.id, organization_id=None
    )
    results = query.all()

    assert [f.id for f in results] == [personal.id]


def test_apply_tenant_scope_none_excludes_other_users_personal_rows(
    db_session, normal_user, admin_user
):
    _make_file(db_session, admin_user, organization_id=None)

    query = db_helpers.apply_tenant_scope(
        db_session.query(MediaFile), MediaFile, user_id=normal_user.id, organization_id=None
    )

    assert query.all() == []


# ---------------------------------------------------------------------------
# get_user_files_query
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# safe_get_by_id
# ---------------------------------------------------------------------------
