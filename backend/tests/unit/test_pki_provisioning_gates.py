"""PKI just-in-time provisioning: the privilege and account-state it writes.

``sync_pki_user_to_db`` creates, updates or converts an account from a certificate
identity. These pin what each path writes: role from ``is_admin`` only, demotion when the
DN leaves the admin list, a local ``super_admin`` never touched, the local password
cleared on conversion, the stored email never replaced by a certificate's, approval
decided by policy, and both account-link guards consulted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from app.auth import pki_provisioning
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.pki_certificate import PKIUserData
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser

pytestmark = pytest.mark.unit

DN = "CN=Pat Person,O=Agency,C=US"


def _data(*, is_admin: bool, email: str = "pat@example.com") -> PKIUserData:
    return cast(
        PKIUserData,
        {
            "subject_dn": DN,
            "common_name": "Pat Person",
            "email": email,
            "is_admin": is_admin,
            "serial_number": "AB",
            "issuer_dn": "CN=CA",
            "organization": "Agency",
            "organizational_unit": None,
            "not_before": None,
            "not_after": None,
            "fingerprint": "AA:BB",
        },
    )


def _user(role: str, auth_type: str = "pki") -> SimpleNamespace:
    return SimpleNamespace(
        email="stored@example.com",
        role=role,
        is_superuser=role_implies_superuser(role),
        auth_type=auth_type,
        hashed_password="local-hash",
        pki_subject_dn=DN,
    )


class _Db:
    def __init__(self, found=None):
        self.found = found
        self.added: list = []

    def query(self, _model):
        return self

    def filter(self, *_a):
        return self

    def first(self):
        return self.found

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def refresh(self, _obj):
        pass


@pytest.mark.parametrize("update", ["_update_pki_user", "_convert_local_user_to_pki"])
class TestRoleDerivation:
    def _run(self, update, role, is_admin):
        user = _user(role)
        getattr(pki_provisioning, update)(_Db(), user, _data(is_admin=is_admin))
        return user

    def test_admin_dn_promotes_a_user(self, update):
        user = self._run(update, ROLE_USER, True)
        assert (user.role, user.is_superuser) == (ROLE_ADMIN, False)

    def test_leaving_the_admin_list_demotes(self, update):
        user = self._run(update, ROLE_ADMIN, False)
        assert (user.role, user.is_superuser) == (ROLE_USER, False)

    def test_super_admin_is_never_demoted_by_an_admin_dn(self, update):
        user = self._run(update, ROLE_SUPER_ADMIN, True)
        assert (user.role, user.is_superuser) == (ROLE_SUPER_ADMIN, True)


class TestAccountState:
    def test_conversion_clears_the_local_password(self):
        user = _user(ROLE_USER, auth_type="local")
        pki_provisioning._convert_local_user_to_pki(_Db(), user, _data(is_admin=False))
        assert user.hashed_password == EXTERNAL_AUTH_NO_PASSWORD
        assert user.auth_type == "pki"

    def test_certificate_email_never_replaces_the_stored_one(self):
        user = _user(ROLE_USER)
        pki_provisioning._update_pki_user(_Db(), user, _data(is_admin=False, email="new@x.com"))
        assert user.email == "stored@example.com"

    @pytest.mark.parametrize("is_admin", [False, True])
    def test_created_account_role_and_approval_follow_policy(self, is_admin):
        db = _Db()
        with patch("app.auth.approval.initial_approval_status", return_value="pending"):
            user = pki_provisioning._create_pki_user(db, _data(is_admin=is_admin))
        assert user.role == (ROLE_ADMIN if is_admin else ROLE_USER)
        assert user.is_superuser is False
        assert user.approval_status == "pending"


class TestSyncRouting:
    def test_existing_local_account_is_converted(self):
        user = _user(ROLE_USER, auth_type="local")
        with (
            patch("app.auth.account_linking.assert_provider_id_link_permitted"),
            patch.object(pki_provisioning, "_convert_local_user_to_pki", return_value=user) as conv,
        ):
            result = pki_provisioning.sync_pki_user_to_db(_Db(found=user), _data(is_admin=False))
        assert result is user
        assert conv.call_args.args[1] is user

    def test_dn_matched_account_goes_through_the_provider_id_guard(self):
        user = _user(ROLE_USER)
        with (
            patch("app.auth.account_linking.assert_provider_id_link_permitted") as guard,
            patch.object(pki_provisioning, "_update_pki_user", return_value=user),
        ):
            result = pki_provisioning.sync_pki_user_to_db(_Db(found=user), _data(is_admin=False))
        assert result is user
        assert guard.call_args.kwargs["source_identifier"] == DN
        assert guard.call_args.kwargs["provider"] == "pki"


class _RaceDb(_Db):
    """The insert loses a unique-constraint race; afterwards only ``by_email`` / ``by_dn`` exist."""

    def __init__(self, *, by_dn=None, by_email=None):
        super().__init__()
        self.by_dn, self.by_email = by_dn, by_email
        self.lost_race = False
        self._crit = ""

    def filter(self, *crit):
        self._crit = " ".join(str(c) for c in crit)
        return self

    def first(self):
        if not self.lost_race:
            return None
        return self.by_dn if "pki_subject_dn" in self._crit else self.by_email

    def commit(self):
        from sqlalchemy.exc import IntegrityError

        self.lost_race = True
        raise IntegrityError("insert", {}, Exception("duplicate key ix_user_email"))

    def rollback(self):
        pass


def _dn_only(dn: str, issuer: str = "CN=CA") -> PKIUserData:
    data = dict(_data(is_admin=False, email=""))
    data.update(subject_dn=dn, issuer_dn=issuer, common_name="John Smith")
    return cast(PKIUserData, data)


class TestPlaceholderIdentity:
    def _email(self, data: PKIUserData) -> str:
        with patch("app.auth.approval.initial_approval_status", return_value="approved"):
            return str(pki_provisioning._create_pki_user(_Db(), data).email)

    def test_same_cn_in_different_orgs_gets_different_placeholders(self):
        a = self._email(_dn_only("CN=John Smith,O=Legit,C=US"))
        b = self._email(_dn_only("CN=John Smith,O=Attacker,C=US"))
        assert a != b
        assert a.endswith("@pki.local") and b.endswith("@pki.local")

    def test_placeholder_is_not_the_bare_common_name(self):
        assert self._email(_dn_only("CN=John Smith,O=Legit,C=US")) != "john.smith@pki.local"

    def test_placeholder_depends_on_the_issuer_and_is_stable(self):
        dn = "CN=John Smith,O=Legit,C=US"
        assert self._email(_dn_only(dn, "CN=CA1")) != self._email(_dn_only(dn, "CN=CA2"))
        assert self._email(_dn_only(dn)) == self._email(_dn_only(dn))


class TestRaceFallbackFailsClosed:
    def _sync(self, db, data):
        with patch("app.auth.approval.initial_approval_status", return_value="approved"):
            return pki_provisioning.sync_pki_user_to_db(db, data)

    def test_email_collision_never_adopts_another_account(self):
        from app.core.exceptions import AuthenticationError

        victim = SimpleNamespace(email="x@pki.local", pki_subject_dn="CN=John Smith,O=Legit,C=US")
        db = _RaceDb(by_email=victim)
        with pytest.raises(AuthenticationError):
            self._sync(db, _dn_only("CN=John Smith,O=Attacker,C=US"))

    def test_concurrent_create_of_the_same_identity_is_returned(self):
        dn = "CN=John Smith,O=Legit,C=US"
        twin = SimpleNamespace(email="x@pki.local", pki_subject_dn=dn, pki_issuer_dn="CN=CA")
        assert self._sync(_RaceDb(by_dn=twin), _dn_only(dn)) is twin

    def test_same_dn_from_another_issuer_is_refused(self):
        from app.core.exceptions import AuthenticationError

        dn = "CN=John Smith,O=Legit,C=US"
        twin = SimpleNamespace(email="x@pki.local", pki_subject_dn=dn, pki_issuer_dn="CN=Other CA")
        with pytest.raises(AuthenticationError):
            self._sync(_RaceDb(by_dn=twin), _dn_only(dn))
