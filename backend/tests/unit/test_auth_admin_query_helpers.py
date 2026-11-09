"""MFA, session, config, group, SCIM, timing and invitation query helpers, real database."""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import event

from app.models.auth_config import AuthConfig
from app.models.group import GroupMapping
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.invitation import UserInvitation
from app.models.refresh_token import RefreshToken
from app.models.user_mfa import UserMFA
from app.services import auth_config_queries
from app.services import group_mapping_queries
from app.services import invitation_queries
from app.services import mfa_queries
from app.services import scim_queries
from app.services import session_queries


def _session_row(user_id: int, *, revoked: bool, expires_in: timedelta) -> RefreshToken:
    now = datetime.now(UTC)
    return RefreshToken(
        user_id=user_id,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        jti=str(uuid.uuid4()),
        expires_at=now + expires_in,
        revoked_at=now if revoked else None,
    )


def test_active_sessions_exclude_revoked_and_expired(normal_user, db_session):
    live = _session_row(normal_user.id, revoked=False, expires_in=timedelta(hours=1))
    revoked = _session_row(normal_user.id, revoked=True, expires_in=timedelta(hours=1))
    expired = _session_row(normal_user.id, revoked=False, expires_in=-timedelta(hours=1))
    db_session.add_all([live, revoked, expired])
    db_session.commit()

    assert [s.jti for s in session_queries.list_active_sessions(db_session, normal_user.id)] == [
        live.jti
    ]
    assert [s.jti for s in session_queries.lock_active_sessions(db_session, normal_user.id)] == [
        live.jti
    ]


def test_mfa_helpers_find_the_row_and_count_enabled(normal_user, db_session):
    assert mfa_queries.get_user_mfa(db_session, normal_user.id) is None
    before = mfa_queries.count_totp_enabled(db_session)
    db_session.add(UserMFA(user_id=normal_user.id, totp_secret="x", totp_enabled=True))
    db_session.commit()
    enrolled = mfa_queries.get_user_mfa(db_session, normal_user.id)
    assert enrolled is not None
    assert enrolled.totp_enabled is True
    assert mfa_queries.count_totp_enabled(db_session) == before + 1


def test_auth_config_helpers_filter_by_category_and_keys(db_session):
    key = f"unit_probe_{uuid.uuid4().hex[:8]}"
    db_session.add(AuthConfig(config_key=key, config_value="v", category="unit_probe_cat"))
    db_session.commit()
    assert [
        c.config_key
        for c in auth_config_queries.list_configs_in_category(db_session, "unit_probe_cat")
    ] == [key]
    assert set(auth_config_queries.get_configs_by_keys(db_session, [key, "absent_key"])) == {key}


def test_group_and_scim_helpers_follow_membership(normal_user, super_admin_user, db_session):
    group = UserGroup(name=f"g-{uuid.uuid4().hex[:8]}", owner_id=super_admin_user.id)
    db_session.add(group)
    db_session.commit()
    db_session.add(UserGroupMember(group_id=group.id, user_id=normal_user.id, source="manual"))
    db_session.add(UserGroupMember(group_id=group.id, user_id=super_admin_user.id, source="ldap"))
    db_session.commit()

    fetched = scim_queries.get_group_by_uuid(db_session, str(group.uuid))
    assert fetched is not None
    assert fetched.id == group.id
    assert scim_queries.get_group_by_uuid(db_session, str(uuid.uuid4())) is None
    assert {u.id for u in scim_queries.get_group_members(db_session, group.id)} == {
        normal_user.id,
        super_admin_user.id,
    }
    assert [g.id for g in scim_queries.get_groups_of_user(db_session, normal_user.id)] == [group.id]
    rows, total = scim_queries.list_groups_page(db_session, group.name, 0, 10)
    assert ([g.id for g in rows], total) == ([group.id], 1)
    assert scim_queries.list_groups_page(db_session, "no-such-group", 0, 10) == ([], 0)

    owned = group_mapping_queries.list_directory_member_rows(db_session, {group.id})
    assert [(r.group_id, r.source) for r in owned] == [(group.id, "ldap")]
    assert [g.id for g in group_mapping_queries.get_groups_by_ids(db_session, [group.id])] == [
        group.id
    ]


def test_scim_user_page_filters_by_email_and_external_id(normal_user, db_session):
    normal_user.external_id = f"ext-{uuid.uuid4().hex[:8]}"
    db_session.commit()
    rows, total = scim_queries.list_users_page(
        db_session, email=None, external_id=normal_user.external_id, offset=0, limit=10
    )
    assert ([u.id for u in rows], total) == ([normal_user.id], 1)
    rows, total = scim_queries.list_users_page(
        db_session, email=str(normal_user.email), external_id=None, offset=0, limit=10
    )
    assert ([u.id for u in rows], total) == ([normal_user.id], 1)


def test_group_mapping_listing_is_scoped_by_source(super_admin_user, db_session):
    claim = f"unit-claim-{uuid.uuid4().hex[:8]}"
    db_session.add(GroupMapping(source="oidc", claim_value=claim, grants_role="user"))
    db_session.commit()
    assert claim in {m.claim_value for m in group_mapping_queries.list_mappings(db_session, "oidc")}
    assert claim not in {
        m.claim_value for m in group_mapping_queries.list_mappings(db_session, "ldap")
    }
    assert claim in {
        m.claim_value for m in group_mapping_queries.list_mappings_for_source(db_session, "oidc")
    }


def test_invitation_listing_hides_inactive_unless_asked(super_admin_user, db_session):
    live = UserInvitation(
        email=f"live-{uuid.uuid4().hex[:6]}@example.com",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        created_by_id=super_admin_user.id,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    spent = UserInvitation(
        email=f"spent-{uuid.uuid4().hex[:6]}@example.com",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        created_by_id=super_admin_user.id,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=datetime.now(UTC),
    )
    db_session.add_all([live, spent])
    db_session.commit()
    pending = {i.email for i in invitation_queries.list_invitations(db_session, False)}
    everything = {i.email for i in invitation_queries.list_invitations(db_session, True)}
    assert live.email in pending and spent.email not in pending
    assert {live.email, spent.email} <= everything


def test_query_helpers_never_commit(normal_user, db_session):
    commits: list[int] = []

    def count(_session):
        commits.append(1)

    event.listen(db_session, "after_commit", count)
    try:
        session_queries.list_active_sessions(db_session, normal_user.id)
        session_queries.lock_active_sessions(db_session, normal_user.id)
        mfa_queries.get_user_mfa(db_session, normal_user.id)
        mfa_queries.count_totp_enabled(db_session)
        invitation_queries.list_invitations(db_session, True)
        group_mapping_queries.list_mappings(db_session, None)
        scim_queries.list_users_page(db_session, email=None, external_id=None, offset=0, limit=1)
    finally:
        event.remove(db_session, "after_commit", count)
    assert commits == []
