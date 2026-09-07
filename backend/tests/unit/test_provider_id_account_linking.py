"""A stale/reassigned provider identifier may not silently take over an account.

``account_linking.py`` protected the email-match branch (never link a super_admin;
never link an unverified email) but explicitly declared the provider-ID-match branch
(``ldap_uid`` / ``oidc_subject`` / ``saml_subject`` / ``pki_subject_dn`` /
``external_id``) out of scope, on the assumption that a stored identifier was only
ever set by a deliberate admin action. That assumption is false: JIT provisioning
stamps the same identifier on an ordinary first login too, on every one of LDAP,
OIDC, SAML, PKI and the registry-based external seam. A stale or reassigned
identifier (a recycled LDAP uid, a replayed OIDC ``sub`` from a different tenant, a
reissued certificate subject DN) therefore matched with **no guard at all** and
silently authenticated the new holder of that identifier as the account's original
owner — including, in the worst case, a ``super_admin`` account.

``account_linking.assert_provider_id_link_permitted`` closes that gap: never
super_admin, and the login's asserted email (when it asserts one) must still agree
with the account's stored email. These tests pin three things: the exploit (a
divergent email) is refused; the ordinary case (matching email, or a source that
asserts none) is unaffected; and the super_admin protection applies on this branch
too, unconditionally.
"""

from __future__ import annotations

from typing import Any
from typing import cast
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.auth import account_linking
from app.auth import external_sync
from app.auth import ldap_auth
from app.auth import pki_auth
from app.auth.oidc import provisioning as oidc_provisioning
from app.auth.provider_registry import ExternalIdentity
from app.auth.saml import provisioning as saml_provisioning


class _FakeUser:
    def __init__(self, email="victim@example.com", role="user", auth_type="ldap"):
        self.id = 1
        self.email = email
        self.role = role
        self.auth_type = auth_type
        self.ldap_uid = "existing-uid"
        self.pki_subject_dn = "CN=Existing"
        self.oidc_subject = "existing-sub"
        self.saml_subject = "existing-nameid"
        self.external_id = "existing-ext"


class _FakeQuery:
    def __init__(self, results):
        self._results = results

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._results.pop(0) if self._results else None


class _FakeProviderIdSession:
    """The provider-id lookup finds ``user`` directly — no email fallback needed."""

    def __init__(self, user):
        self._user = user

    def query(self, _model):
        return _FakeQuery([self._user])

    def refresh(self, _obj):
        pass

    def commit(self):
        pass


@pytest.mark.unit
class TestProviderIdLinkGuardRule:
    """The shared rule, exercised directly."""

    def test_divergent_asserted_email_is_refused(self):
        """The exploit: the identifier matched, but the person behind it changed."""
        with pytest.raises(HTTPException) as exc:
            account_linking.assert_provider_id_link_permitted(
                _FakeUser(email="victim@example.com"),
                provider="ldap",
                source_identifier="recycled-uid",
                asserted_email="attacker@example.com",
                failure_detail="Incorrect username or password",
            )
        assert exc.value.status_code == 401

    def test_matching_asserted_email_is_permitted(self):
        """The ordinary case: same identifier, same person. Must not be blocked."""
        account_linking.assert_provider_id_link_permitted(
            _FakeUser(email="victim@example.com"),
            provider="ldap",
            source_identifier="existing-uid",
            asserted_email="victim@example.com",
            failure_detail="nope",
        )

    def test_no_asserted_email_is_permitted(self):
        """A source with no verified-address concept must still work as before."""
        account_linking.assert_provider_id_link_permitted(
            _FakeUser(),
            provider="pki",
            source_identifier="CN=Existing",
            asserted_email=None,
            failure_detail="nope",
        )

    def test_super_admin_is_refused_even_with_a_matching_email(self):
        """Unconditional — a matching email does not lift the super_admin guard."""
        with pytest.raises(HTTPException):
            account_linking.assert_provider_id_link_permitted(
                _FakeUser(email="victim@example.com", role="super_admin"),
                provider="oidc",
                source_identifier="existing-sub",
                asserted_email="victim@example.com",
                failure_detail="Invalid access token",
            )

    def test_refusal_is_audited_as_provider_id_match(self):
        with patch.object(account_linking.audit_logger, "log") as log:
            with pytest.raises(HTTPException):
                account_linking.assert_provider_id_link_permitted(
                    _FakeUser(email="victim@example.com"),
                    provider="ldap",
                    source_identifier="recycled-uid",
                    asserted_email="attacker@example.com",
                    failure_detail="Incorrect username or password",
                )

        assert log.called, "a refused provider-id match must leave an audit record"
        assert log.call_args.kwargs["error_code"] == account_linking.LINK_REFUSED_ERROR_CODE
        assert log.call_args.kwargs["details"]["matched_by"] == "provider_id"

    def test_refusal_response_matches_the_ordinary_failure(self):
        with pytest.raises(HTTPException) as exc:
            account_linking.assert_provider_id_link_permitted(
                _FakeUser(email="victim@example.com"),
                provider="ldap",
                source_identifier="recycled-uid",
                asserted_email="attacker@example.com",
                failure_detail="Incorrect username or password",
                failure_headers={"WWW-Authenticate": "Bearer"},
            )
        assert exc.value.detail == "Incorrect username or password"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


def _ldap_data(email: str) -> ldap_auth.LdapUserData:
    return cast(
        "ldap_auth.LdapUserData",
        {
            "username": "existing-uid",
            "email": email,
            "full_name": "A",
            "is_admin": False,
            "groups": [],
        },
    )


@pytest.mark.unit
class TestLdapSyncProviderIdGuard:
    def test_divergent_email_blocks_the_login(self):
        user = _FakeUser(email="victim@example.com", auth_type="ldap")
        db = _FakeProviderIdSession(user)

        with patch.object(ldap_auth, "_update_ldap_user") as update:
            with pytest.raises(HTTPException) as exc:
                ldap_auth.sync_ldap_user_to_db(db, _ldap_data("attacker@example.com"))

        assert exc.value.status_code == 401
        assert not update.called, "the account must not be updated/authenticated into"

    def test_matching_email_still_authenticates_normally(self):
        """Common case, unchanged: same uid, same email, ordinary login proceeds."""
        user = _FakeUser(email="victim@example.com", auth_type="ldap")
        db = _FakeProviderIdSession(user)

        with (
            patch.object(ldap_auth, "_update_ldap_user", return_value=user) as update,
            patch("app.services.idp_group_mapping_service.reconcile_user", return_value=None),
        ):
            result = ldap_auth.sync_ldap_user_to_db(db, _ldap_data("victim@example.com"))

        assert update.called
        assert result is user

    def test_super_admin_account_is_never_taken_over_via_provider_id(self):
        user = _FakeUser(email="victim@example.com", role="super_admin", auth_type="ldap")
        db = _FakeProviderIdSession(user)

        with patch.object(ldap_auth, "_update_ldap_user") as update:
            with pytest.raises(HTTPException) as exc:
                ldap_auth.sync_ldap_user_to_db(db, _ldap_data("victim@example.com"))

        assert exc.value.status_code == 401
        assert not update.called


def _pki_data(email: str) -> pki_auth.PKIUserData:
    return cast(
        "pki_auth.PKIUserData",
        {
            "subject_dn": "CN=Existing",
            "common_name": "Existing",
            "email": email,
            "is_admin": False,
            "serial_number": None,
            "issuer_dn": None,
            "organization": None,
            "organizational_unit": None,
            "not_before": None,
            "not_after": None,
            "fingerprint": None,
        },
    )


@pytest.mark.unit
class TestPkiSyncProviderIdGuard:
    def test_divergent_email_blocks_the_login(self):
        user = _FakeUser(email="victim@example.com", auth_type="pki")
        db = _FakeProviderIdSession(user)

        with patch.object(pki_auth, "_update_pki_user") as update:
            with pytest.raises(HTTPException) as exc:
                pki_auth.sync_pki_user_to_db(db, _pki_data("attacker@example.com"))

        assert exc.value.status_code == 401
        assert not update.called

    def test_matching_email_still_authenticates_normally(self):
        user = _FakeUser(email="victim@example.com", auth_type="pki")
        db = _FakeProviderIdSession(user)

        with patch.object(pki_auth, "_update_pki_user", return_value=user) as update:
            result = pki_auth.sync_pki_user_to_db(db, _pki_data("victim@example.com"))

        assert update.called
        assert result is user

    def test_super_admin_account_is_never_taken_over_via_provider_id(self):
        user = _FakeUser(email="victim@example.com", role="super_admin", auth_type="pki")
        db = _FakeProviderIdSession(user)

        with patch.object(pki_auth, "_update_pki_user") as update:
            with pytest.raises(HTTPException) as exc:
                pki_auth.sync_pki_user_to_db(db, _pki_data("victim@example.com"))

        assert exc.value.status_code == 401
        assert not update.called


def _oidc_data(email: str) -> dict[str, Any]:
    return {
        "oidc_subject": "existing-sub",
        "email": email,
        "email_verified": True,
        "full_name": "A",
        "username": "a",
        "is_admin": False,
        "roles": [],
        "claim_keys": [],
        "roles_claim_source": "id_token",
    }


@pytest.mark.unit
class TestOidcSyncProviderIdGuard:
    def test_divergent_email_blocks_the_login(self):
        user = _FakeUser(email="victim@example.com", auth_type="oidc")
        db = _FakeProviderIdSession(user)

        with (
            patch.object(oidc_provisioning, "_update_oidc_user") as update,
            patch(
                "app.auth.oidc.admission.assert_oidc_admission_permitted",
                return_value=None,
            ),
            patch(
                "app.services.idp_group_mapping_service.resolve_grants",
                return_value=type("G", (), {"grants_admin": False})(),
            ),
            patch("app.services.idp_group_mapping_service.reconcile_user", return_value=None),
        ):
            with pytest.raises(HTTPException) as exc:
                oidc_provisioning.sync_oidc_user_to_db(
                    db, cast("Any", _oidc_data("attacker@example.com"))
                )

        assert exc.value.status_code == 401
        assert not update.called

    def test_matching_email_still_authenticates_normally(self):
        user = _FakeUser(email="victim@example.com", auth_type="oidc")
        db = _FakeProviderIdSession(user)

        with (
            patch.object(oidc_provisioning, "_update_oidc_user", return_value=user) as update,
            patch(
                "app.auth.oidc.admission.assert_oidc_admission_permitted",
                return_value=None,
            ),
            patch(
                "app.services.idp_group_mapping_service.resolve_grants",
                return_value=type("G", (), {"grants_admin": False})(),
            ),
            patch("app.services.idp_group_mapping_service.reconcile_user", return_value=None),
        ):
            result = oidc_provisioning.sync_oidc_user_to_db(
                db, cast("Any", _oidc_data("victim@example.com"))
            )

        assert update.called
        assert result is user

    def test_super_admin_account_is_never_taken_over_via_provider_id(self):
        user = _FakeUser(email="victim@example.com", role="super_admin", auth_type="oidc")
        db = _FakeProviderIdSession(user)

        with (
            patch.object(oidc_provisioning, "_update_oidc_user") as update,
            patch(
                "app.auth.oidc.admission.assert_oidc_admission_permitted",
                return_value=None,
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                oidc_provisioning.sync_oidc_user_to_db(
                    db, cast("Any", _oidc_data("victim@example.com"))
                )

        assert exc.value.status_code == 401
        assert not update.called


def _saml_data(email: str) -> dict[str, Any]:
    return {
        "saml_subject": "existing-nameid",
        "email": email,
        "email_verified": False,
        "full_name": "A",
        "groups": [],
        "is_admin": False,
    }


@pytest.mark.unit
class TestSamlSyncProviderIdGuard:
    def test_divergent_email_blocks_the_login(self):
        user = _FakeUser(email="victim@example.com", auth_type="saml")
        db = _FakeProviderIdSession(user)

        with (
            patch.object(saml_provisioning, "_update_saml_user") as update,
            patch(
                "app.auth.saml.admission.assert_saml_admission_permitted",
                return_value=None,
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                saml_provisioning.sync_saml_user_to_db(
                    db, cast("Any", _saml_data("attacker@example.com"))
                )

        assert exc.value.status_code == 401
        assert not update.called

    def test_matching_email_still_authenticates_normally(self):
        user = _FakeUser(email="victim@example.com", auth_type="saml")
        db = _FakeProviderIdSession(user)

        with (
            patch.object(saml_provisioning, "_update_saml_user", return_value=user) as update,
            patch(
                "app.auth.saml.admission.assert_saml_admission_permitted",
                return_value=None,
            ),
        ):
            result = saml_provisioning.sync_saml_user_to_db(
                db, cast("Any", _saml_data("victim@example.com"))
            )

        assert update.called
        assert result is user

    def test_super_admin_account_is_never_taken_over_via_provider_id(self):
        user = _FakeUser(email="victim@example.com", role="super_admin", auth_type="saml")
        db = _FakeProviderIdSession(user)

        with (
            patch.object(saml_provisioning, "_update_saml_user") as update,
            patch(
                "app.auth.saml.admission.assert_saml_admission_permitted",
                return_value=None,
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                saml_provisioning.sync_saml_user_to_db(
                    db, cast("Any", _saml_data("victim@example.com"))
                )

        assert exc.value.status_code == 401
        assert not update.called


@pytest.mark.unit
class TestExternalSyncProviderIdGuard:
    def _identity(self, email: str) -> ExternalIdentity:
        return ExternalIdentity(
            provider="cloudidp",
            external_id="existing-ext",
            email=email,
            email_verified=True,
        )

    def test_divergent_email_blocks_the_login(self):
        user = _FakeUser(email="victim@example.com", auth_type="cloudidp")
        db = cast("Any", _FakeProviderIdSession(user))

        with pytest.raises(PermissionError):
            external_sync.sync_external_user_to_db(db, self._identity("attacker@example.com"))

        assert user.email == "victim@example.com", "the account row must be left untouched"

    def test_matching_email_still_authenticates_normally(self):
        user = _FakeUser(email="victim@example.com", auth_type="cloudidp")
        db = cast("Any", _FakeProviderIdSession(user))

        result = external_sync.sync_external_user_to_db(db, self._identity("victim@example.com"))

        assert cast("Any", result) is user

    def test_super_admin_account_is_never_taken_over_via_provider_id(self):
        user = _FakeUser(email="victim@example.com", role="super_admin", auth_type="cloudidp")
        db = cast("Any", _FakeProviderIdSession(user))

        with pytest.raises(PermissionError):
            external_sync.sync_external_user_to_db(db, self._identity("victim@example.com"))

        assert user.role == "super_admin"
