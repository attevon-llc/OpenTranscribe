"""Provisioning an LDAP login into a ``User`` row: create, update, the race, the privilege inputs.

What this pins, against a real database:

* a new account's ``full_name`` falls back to the email's local part;
* the unique-constraint race in ``_create_ldap_user`` returns the row the concurrent request
  created (found by uid, then by email) and raises ``ValueError`` when neither exists;
* ``sync_ldap_user_to_db`` creates a new account at the role the *effective* admin signal
  (directory signal OR a mapping grant) implies, and hands ``reconcile_user`` the directory's
  own signal, not the OR-ed one;
* ``_update_ldap_user`` never blanks a stored name and never touches privilege.

``reconcile_user`` and ``resolve_grants`` are replaced with recorders so the role observed is
the one the account was *created* with, not one reconciliation applied afterwards.
"""

from __future__ import annotations

import inspect
import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.auth import ldap_auth
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.models.user import User
from app.services import idp_group_mapping_service


def _uid() -> str:
    return f"p3-ldap-{uuid_pkg.uuid4().hex[:10]}"


def _data(
    uid: str,
    *,
    email: str | None = None,
    full_name: str = "Directory Person",
    is_admin: bool = False,
    groups: list[str] | None = None,
) -> ldap_auth.LdapUserData:
    return ldap_auth.LdapUserData(
        username=uid,
        email=email if email is not None else f"{uid}@example.com",
        full_name=full_name,
        is_admin=is_admin,
        groups=list(groups or []),
    )


def _existing(db, **fields: Any) -> User:
    user = User(
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        **fields,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestCreate:
    def test_an_empty_name_falls_back_to_the_email_local_part(self, db_session):
        uid = _uid()
        user = ldap_auth._create_ldap_user(
            db_session, uid, f"{uid}@example.com", _data(uid, full_name=""), is_admin=False
        )

        assert user.full_name == uid

    @pytest.mark.parametrize("is_admin,role", [(False, "user"), (True, "admin")])
    def test_the_row_carries_the_ldap_identity(self, db_session, is_admin, role):
        uid = _uid()
        user = ldap_auth._create_ldap_user(
            db_session, uid, f"{uid}@example.com", _data(uid), is_admin=is_admin
        )

        assert (user.auth_type, user.ldap_uid, user.email) == ("ldap", uid, f"{uid}@example.com")
        assert user.hashed_password == EXTERNAL_AUTH_NO_PASSWORD
        assert (user.role, user.is_superuser, user.is_active) == (role, False, True)
        assert user.full_name == "Directory Person"


class TestCreateRace:
    """``production_txn_view`` so the race's ``rollback()`` undoes only the losing insert."""

    def test_a_concurrent_row_with_the_same_uid_is_returned(self, db_session, production_txn_view):
        uid = _uid()
        winner = _existing(
            db_session, email=f"{uid}-first@example.com", auth_type="ldap", ldap_uid=uid
        )

        got = ldap_auth._create_ldap_user(
            production_txn_view, uid, f"{uid}@example.com", _data(uid), is_admin=False
        )

        assert got.id == winner.id

    def test_a_concurrent_row_with_the_same_email_is_returned(
        self, db_session, production_txn_view
    ):
        uid = _uid()
        email = f"{uid}@example.com"
        winner = _existing(db_session, email=email, auth_type="local")

        got = ldap_auth._create_ldap_user(
            production_txn_view, uid, email, _data(uid), is_admin=False
        )

        assert got.id == winner.id

    def test_a_conflict_whose_row_is_gone_raises_value_error(self, production_txn_view):
        """The conflicting row rolls back with the failed insert, so there is nothing to find."""
        uid = _uid()
        production_txn_view.add(
            User(
                email=f"{uid}@example.com",
                hashed_password="x",
                is_active=True,
                is_superuser=False,
                role="user",
                auth_type="ldap",
                ldap_uid=uid,
            )
        )
        production_txn_view.flush()

        with pytest.raises(ValueError, match=uid):
            ldap_auth._create_ldap_user(
                production_txn_view, uid, f"{uid}@example.com", _data(uid), is_admin=False
            )


#: Taken before any test replaces the functions, so recorded calls normalise against the real ones.
_RESOLVE_SIGNATURE = inspect.signature(idp_group_mapping_service.resolve_grants)
_RECONCILE_SIGNATURE = inspect.signature(idp_group_mapping_service.reconcile_user)


class _Recorders:
    def __init__(self, grants_admin: bool) -> None:
        self.grants_admin = grants_admin
        self.resolve_calls: list[inspect.BoundArguments] = []
        self.reconcile_calls: list[inspect.BoundArguments] = []

    def resolve_grants(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        bound = _RESOLVE_SIGNATURE.bind(*args, **kwargs)
        self.resolve_calls.append(bound)
        return SimpleNamespace(grants_admin=self.grants_admin)

    def reconcile_user(self, *args: Any, **kwargs: Any) -> None:
        bound = _RECONCILE_SIGNATURE.bind(*args, **kwargs)
        bound.apply_defaults()
        self.reconcile_calls.append(bound)


@pytest.fixture
def recorders(monkeypatch):
    """Replace both collaborators wherever the module under test might look them up."""

    def _install(grants_admin: bool) -> _Recorders:
        rec = _Recorders(grants_admin)
        for target in (idp_group_mapping_service, ldap_auth):
            monkeypatch.setattr(target, "resolve_grants", rec.resolve_grants, raising=False)
            monkeypatch.setattr(target, "reconcile_user", rec.reconcile_user, raising=False)
        return rec

    return _install


class TestSyncNewAccount:
    @pytest.mark.parametrize(
        "directory_admin,mapped_admin,role",
        [
            (False, False, "user"),
            (True, False, "admin"),
            (False, True, "admin"),
            (True, True, "admin"),
        ],
    )
    def test_the_account_is_created_at_the_effective_role(
        self, db_session, recorders, directory_admin, mapped_admin, role
    ):
        rec = recorders(mapped_admin)
        uid = _uid()
        groups = ["CN=Staff,DC=test"]

        with patch.object(
            ldap_auth, "_create_ldap_user", wraps=ldap_auth._create_ldap_user
        ) as create:
            user = ldap_auth.sync_ldap_user_to_db(
                db_session, _data(uid, is_admin=directory_admin, groups=groups)
            )

        assert create.call_count == 1
        assert create.call_args.kwargs["is_admin"] is (directory_admin or mapped_admin)
        assert (user.role, user.is_superuser, user.ldap_uid) == (role, False, uid)

        (resolved,) = rec.resolve_calls
        assert resolved.arguments["db"] is db_session
        assert resolved.arguments["source"] == "ldap"
        assert list(resolved.arguments["claim_values"]) == groups

        (reconciled,) = rec.reconcile_calls
        assert reconciled.arguments["db"] is db_session
        assert reconciled.arguments["user"] is user
        assert reconciled.arguments["source"] == "ldap"
        assert list(reconciled.arguments["claim_values"]) == groups
        assert reconciled.arguments["legacy_admin"] is directory_admin
        assert reconciled.arguments["reason"] == "idp_login"


class TestSyncExistingAccount:
    def test_an_ldap_account_is_updated_and_its_role_left_to_reconciliation(
        self, db_session, recorders
    ):
        rec = recorders(True)
        uid = _uid()
        existing = _existing(
            db_session, email=f"{uid}@example.com", auth_type="ldap", ldap_uid=uid, full_name="Old"
        )

        user = ldap_auth.sync_ldap_user_to_db(db_session, _data(uid, full_name="New Name"))

        assert user.id == existing.id
        assert (user.role, user.full_name) == ("user", "New Name")
        assert rec.reconcile_calls[0].arguments["legacy_admin"] is False

    def test_a_local_account_carrying_the_uid_is_converted(self, db_session, recorders):
        recorders(False)
        uid = _uid()
        existing = _existing(
            db_session, email=f"{uid}@example.com", auth_type="local", ldap_uid=uid, full_name="L"
        )

        user = ldap_auth.sync_ldap_user_to_db(db_session, _data(uid, full_name=""))

        assert user.id == existing.id
        assert (user.auth_type, user.hashed_password) == ("ldap", EXTERNAL_AUTH_NO_PASSWORD)
        assert (user.role, user.full_name) == ("user", "L")


class TestUpdateFields:
    def _user(self, **fields: Any) -> User:
        base: dict[str, Any] = {
            "email": "stored@example.com",
            "full_name": "Stored Name",
            "auth_type": "pki",
            "ldap_uid": None,
            "role": "admin",
            "is_superuser": False,
        }
        base.update(fields)
        return User(**base)

    def test_an_empty_directory_name_keeps_the_stored_one(self):
        user = self._user()

        ldap_auth._update_ldap_user(MagicMock(), user, "jdoe", "", _data("jdoe", full_name=""))

        assert user.full_name == "Stored Name"
        assert (user.auth_type, user.ldap_uid) == ("ldap", "jdoe")

    def test_a_directory_name_replaces_the_stored_one(self):
        user = self._user()

        ldap_auth._update_ldap_user(MagicMock(), user, "jdoe", "", _data("jdoe", full_name="Jane"))

        assert user.full_name == "Jane"

    def test_privilege_is_not_touched(self):
        user = self._user(role="admin")

        ldap_auth._update_ldap_user(MagicMock(), user, "jdoe", "", _data("jdoe", is_admin=False))

        assert (user.role, user.is_superuser) == ("admin", False)
