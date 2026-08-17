"""Real behavioral tests for the SCIM write layer (app/services/scim_service.py,
app/services/scim_group_service.py).

Issue #474: both modules had zero test coverage by the audit's string-level check,
even though ``tests/api/test_scim.py`` already exercises them thoroughly through the
HTTP layer — the module names simply never appear literally in that file. These
tests complement it at the service layer, where a few things the API suite cannot
reach from the outside are cheap to pin directly: audit event shape, the no-op path
(nothing committed/audited when nothing changed), and the exact set-membership math
in ``scim_group_service``.

Writing these surfaced a real bug: ``create_user``/``update_user`` never passed
``target_user_id`` to the shared ``_audit`` helper, so every SCIM-driven audit event
(including ``AUTH_ACCOUNT_DISABLED`` on deactivation) recorded the subject only by
username/email, never by the stable numeric id every other administrative emitter in
this codebase uses (``account_security_service``, ``idp_group_mapping_service``,
``directory_sync_service``, ``api/endpoints/admin.py``, ``api/endpoints/groups.py``
all pass ``target_user_id=int(user.id)``). ``app/auth/CLAUDE.md`` documents this as
an access-control invariant from issue #443 ("user_id is the ACTOR; the subject goes
in target_user_id / target_username"), and there is an AST test
(``tests/unit/test_audit_actor_target.py``) meant to enforce it — but it only walks
direct ``audit_logger.log(...)`` call sites, so it cannot see this module's calls,
which all go through the local ``_audit()`` wrapper. Fixed by passing
``target_user_id=int(user.id)`` from both callers.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.core.security import get_password_hash
from app.models.group import MEMBERSHIP_SOURCE_MANUAL
from app.models.group import MEMBERSHIP_SOURCE_SCIM
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services import scim_group_service
from app.services import scim_service


def _unique_email(prefix: str = "svc") -> str:
    return f"{prefix}-{uuid_pkg.uuid4().hex[:8]}@example.com"


def _make_user(db_session, *, role: str = "user", active: bool = True) -> User:
    user = User(
        email=_unique_email(),
        full_name="Test Subject",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role=role,
        auth_type="local",
        is_active=active,
        is_superuser=(role == "super_admin"),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class _AuditCapture:
    """Captures every kwargs dict passed to ``AuditLogger.log``."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *_args, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def audit_capture(monkeypatch):
    """Patches the one ``audit_logger`` singleton both modules' ``_audit()``
    helpers write through — ``scim_group_service`` imports ``_audit`` itself
    from ``scim_service`` rather than importing ``audit_logger`` directly."""
    capture = _AuditCapture()
    monkeypatch.setattr(scim_service.audit_logger, "log", capture)
    return capture


class TestCreateUser:
    def test_creates_a_local_pending_password_account(self, db_session):
        email = _unique_email()
        user = scim_service.create_user(
            db_session,
            email=email,
            display_name="Ada Lovelace",
            external_id="ext-1",
            active=True,
            actor="pytest-idp",
        )
        assert user.email == email
        assert user.auth_type == "local"
        assert user.role == "user"
        assert bool(user.is_superuser) is False
        assert user.external_id == "ext-1"

    def test_duplicate_email_raises_conflict_not_a_silent_overwrite(self, db_session):
        email = _unique_email()
        scim_service.create_user(
            db_session,
            email=email,
            display_name=None,
            external_id=None,
            active=True,
            actor="pytest-idp",
        )
        with pytest.raises(scim_service.SCIMConflictError):
            scim_service.create_user(
                db_session,
                email=email,
                display_name=None,
                external_id=None,
                active=True,
                actor="pytest-idp",
            )

    def test_audit_event_names_the_subject_by_id(self, db_session, audit_capture):
        """Regression test for the target_user_id omission bug (see module docstring)."""
        user = scim_service.create_user(
            db_session,
            email=_unique_email(),
            display_name=None,
            external_id=None,
            active=True,
            actor="pytest-idp",
        )
        create_events = [c for c in audit_capture.calls if c.get("username") == user.email]
        assert create_events, "no audit event was emitted for the create"
        assert create_events[0]["target_user_id"] == int(user.id), (
            "the subject must be queryable by id, not only by username — see "
            "app/auth/CLAUDE.md's issue #443 invariant"
        )
        # The actor is the SCIM token, never the subject (per _audit's own docstring).
        assert create_events[0].get("user_id") is None


class TestUpdateUser:
    def test_no_changes_is_a_true_no_op(self, db_session, audit_capture):
        user = _make_user(db_session)
        result = scim_service.update_user(db_session, user, actor="pytest-idp")
        assert result is user
        assert audit_capture.calls == [], "nothing changed, so nothing should be audited"

    def test_deactivation_revokes_every_session(self, db_session):
        from app.auth.token_service import token_service

        user = _make_user(db_session, active=True)
        token_service.create_refresh_token(
            db=db_session, user_id=int(user.id), user_uuid=str(user.uuid), role=str(user.role)
        )
        token_service.create_refresh_token(
            db=db_session, user_id=int(user.id), user_uuid=str(user.uuid), role=str(user.role)
        )

        scim_service.update_user(db_session, user, active=False, actor="pytest-idp")

        db_session.refresh(user)
        assert user.is_active is False
        live = (
            db_session.query(RefreshToken)
            .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .count()
        )
        assert live == 0

    def test_redundant_deactivation_of_an_already_inactive_user_is_a_no_op(
        self, db_session, audit_capture
    ):
        user = _make_user(db_session, active=False)
        scim_service.update_user(db_session, user, active=False, actor="pytest-idp")
        assert audit_capture.calls == []

    def test_reactivating_a_user_does_not_require_super_admin_checks(self, db_session):
        user = _make_user(db_session, active=False)
        updated = scim_service.update_user(db_session, user, active=True, actor="pytest-idp")
        assert updated.is_active is True

    def test_email_conflict_on_update_raises_conflict(self, db_session):
        taken = _make_user(db_session)
        user = _make_user(db_session)
        with pytest.raises(scim_service.SCIMConflictError):
            scim_service.update_user(db_session, user, email=str(taken.email), actor="pytest-idp")

    def test_super_admin_cannot_be_deactivated(self, db_session):
        super_admin = _make_user(db_session, role="super_admin", active=True)
        with pytest.raises(scim_service.SCIMForbiddenError):
            scim_service.update_user(db_session, super_admin, active=False, actor="pytest-idp")
        db_session.refresh(super_admin)
        assert super_admin.is_active is True

    def test_super_admin_email_cannot_be_changed(self, db_session):
        """Not documented as a deactivation, but still refused: `_assert_not_super_admin`
        covers "change the userName of" as well — changing the sign-in address of the
        platform owner through an unattended provisioning connector is exactly the
        kind of silent account takeover the super_admin protection exists to prevent.
        """
        super_admin = _make_user(db_session, role="super_admin", active=True)
        original_email = str(super_admin.email)
        with pytest.raises(scim_service.SCIMForbiddenError):
            scim_service.update_user(
                db_session, super_admin, email=_unique_email("hijack"), actor="pytest-idp"
            )
        db_session.refresh(super_admin)
        assert str(super_admin.email) == original_email

    def test_super_admin_display_name_may_still_be_changed(self, db_session):
        """Only the identity-changing/deactivating writes are blocked — cosmetic
        fields are not, per the module docstring's scoped refusal list."""
        super_admin = _make_user(db_session, role="super_admin", active=True)
        updated = scim_service.update_user(
            db_session, super_admin, display_name="New Display Name", actor="pytest-idp"
        )
        assert updated.full_name == "New Display Name"

    def test_audit_event_names_the_subject_by_id_on_deactivation(self, db_session, audit_capture):
        """Regression test for the target_user_id omission bug (see module docstring).

        AUTH_ACCOUNT_DISABLED is one of the two event types
        tests/unit/test_audit_actor_target.py's AST guard requires a target for — but
        that guard cannot see this call site, since it goes through scim_service's own
        `_audit` wrapper rather than `audit_logger.log(...)` directly.
        """
        user = _make_user(db_session, active=True)
        scim_service.update_user(db_session, user, active=False, actor="pytest-idp")

        disable_events = [
            c for c in audit_capture.calls if c.get("event_type") == "auth.account.disabled"
        ]
        assert disable_events, "no AUTH_ACCOUNT_DISABLED event was emitted"
        assert disable_events[0]["target_user_id"] == int(user.id)
        assert disable_events[0].get("user_id") is None


class TestScimGroupServiceMembership:
    @pytest.fixture
    def owner(self, db_session) -> User:
        return _make_user(db_session, role="admin")

    @pytest.fixture
    def group(self, db_session, owner) -> UserGroup:
        row = UserGroup(name=f"svc-group-{uuid_pkg.uuid4().hex[:8]}", owner_id=int(owner.id))
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        return row

    def test_set_group_members_adds_and_removes_scim_rows(self, db_session, group):
        keep = _make_user(db_session)
        drop = _make_user(db_session)
        db_session.add(
            UserGroupMember(
                group_id=int(group.id),
                user_id=int(drop.id),
                role="member",
                source=MEMBERSHIP_SOURCE_SCIM,
            )
        )
        db_session.commit()

        scim_group_service.set_group_members(db_session, group, {int(keep.id)}, actor="idp")

        rows = db_session.query(UserGroupMember).filter(UserGroupMember.group_id == group.id).all()
        member_ids = {int(r.user_id) for r in rows}
        assert member_ids == {int(keep.id)}

    def test_set_group_members_never_touches_a_manual_row(self, db_session, group):
        manual = _make_user(db_session)
        db_session.add(
            UserGroupMember(
                group_id=int(group.id),
                user_id=int(manual.id),
                role="member",
                source=MEMBERSHIP_SOURCE_MANUAL,
            )
        )
        db_session.commit()

        # Replace with an empty target set — if the manual-row protection were
        # broken, this would delete everything.
        scim_group_service.set_group_members(db_session, group, set(), actor="idp")

        rows = db_session.query(UserGroupMember).filter(UserGroupMember.group_id == group.id).all()
        assert {int(r.user_id) for r in rows} == {int(manual.id)}

    def test_add_group_members_does_not_duplicate_an_existing_row_of_any_source(
        self, db_session, group
    ):
        already = _make_user(db_session)
        db_session.add(
            UserGroupMember(
                group_id=int(group.id),
                user_id=int(already.id),
                role="member",
                source=MEMBERSHIP_SOURCE_MANUAL,
            )
        )
        db_session.commit()

        scim_group_service.add_group_members(db_session, group, {int(already.id)}, actor="idp")

        rows = (
            db_session.query(UserGroupMember)
            .filter(UserGroupMember.group_id == group.id, UserGroupMember.user_id == already.id)
            .all()
        )
        assert len(rows) == 1
        assert str(rows[0].source) == MEMBERSHIP_SOURCE_MANUAL, (
            "must not overwrite the owning source"
        )

    def test_add_group_members_with_nothing_new_does_not_audit(
        self, db_session, group, audit_capture
    ):
        already = _make_user(db_session)
        db_session.add(
            UserGroupMember(
                group_id=int(group.id),
                user_id=int(already.id),
                role="member",
                source=MEMBERSHIP_SOURCE_SCIM,
            )
        )
        db_session.commit()
        audit_capture.calls.clear()

        scim_group_service.add_group_members(db_session, group, {int(already.id)}, actor="idp")
        assert audit_capture.calls == []

    def test_remove_group_members_only_deletes_scim_rows_in_the_requested_set(
        self, db_session, group
    ):
        target = _make_user(db_session)
        other_scim = _make_user(db_session)
        manual = _make_user(db_session)
        db_session.add_all(
            [
                UserGroupMember(
                    group_id=int(group.id),
                    user_id=int(target.id),
                    role="member",
                    source=MEMBERSHIP_SOURCE_SCIM,
                ),
                UserGroupMember(
                    group_id=int(group.id),
                    user_id=int(other_scim.id),
                    role="member",
                    source=MEMBERSHIP_SOURCE_SCIM,
                ),
                UserGroupMember(
                    group_id=int(group.id),
                    user_id=int(manual.id),
                    role="member",
                    source=MEMBERSHIP_SOURCE_MANUAL,
                ),
            ]
        )
        db_session.commit()

        scim_group_service.remove_group_members(db_session, group, {int(target.id)}, actor="idp")

        remaining = {
            int(r.user_id)
            for r in db_session.query(UserGroupMember).filter(UserGroupMember.group_id == group.id)
        }
        assert remaining == {int(other_scim.id), int(manual.id)}

    def test_remove_group_members_with_an_empty_set_is_a_true_no_op(
        self, db_session, group, audit_capture
    ):
        """Companion to the patch_ops.py fix: the service itself must genuinely no-op
        on an empty id set (parse_group_operation routes a bare "empty the group"
        remove through set_group_members instead, precisely because this function
        does not act on an empty set)."""
        present = _make_user(db_session)
        db_session.add(
            UserGroupMember(
                group_id=int(group.id),
                user_id=int(present.id),
                role="member",
                source=MEMBERSHIP_SOURCE_SCIM,
            )
        )
        db_session.commit()

        scim_group_service.remove_group_members(db_session, group, set(), actor="idp")

        remaining = {
            int(r.user_id)
            for r in db_session.query(UserGroupMember).filter(UserGroupMember.group_id == group.id)
        }
        assert remaining == {int(present.id)}
        assert audit_capture.calls == []
