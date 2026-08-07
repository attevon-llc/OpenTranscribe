"""An external directory may not claim an existing account by email.

``sync_ldap_user_to_db`` and ``sync_pki_user_to_db`` looked a user up by provider id
and, on a miss, fell back to ``User.email == ...`` and converted whatever they found
to the external auth type — clearing its local password on the way. The email is an
attribute of the external source, so anyone who can write it (a directory admin
setting ``mail``, a self-service directory, anyone who can have a certificate issued)
could point it at an existing account and inherit it, privileges included.

The cloud JIT seam (``external_sync.sync_external_user_to_db``, documented at
``constants.CLOUD_SEAM_VERSION`` v2) already had the rule: link only when the source
asserts the address verified, and never link a ``super_admin``. These tests pin that
the same rule now applies to LDAP and PKI, that a refusal is audited, and — the part
that is easy to get wrong — that a refusal does NOT fall through to creating a
duplicate account.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.auth import account_linking
from app.auth import ldap_auth
from app.auth import pki_auth


class _FakeUser:
    def __init__(self, email="victim@example.com", role="user", auth_type="local"):
        self.id = 1
        self.email = email
        self.role = role
        self.auth_type = auth_type
        self.ldap_uid = None
        self.pki_subject_dn = None


class _FakeQuery:
    def __init__(self, results):
        self._results = results

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._results.pop(0) if self._results else None


class _FakeSession:
    """Returns None for the provider-id lookup, then a user for the email lookup."""

    def __init__(self, email_match):
        self._results = [None, email_match]

    def query(self, _model):
        return _FakeQuery(self._results)

    def refresh(self, _obj):
        pass

    def commit(self):
        pass


@pytest.mark.unit
class TestLinkGuardRule:
    """The shared rule, exercised directly."""

    def test_unverified_source_is_refused(self):
        with pytest.raises(HTTPException) as exc:
            account_linking.assert_email_link_permitted(
                _FakeUser(),
                provider="ldap",
                source_identifier="jdoe",
                email_verified=False,
                failure_detail="Incorrect username or password",
            )
        assert exc.value.status_code == 401

    def test_super_admin_is_refused_even_when_verified(self):
        """The super_admin guard is unconditional — a verified address does not lift it."""
        with pytest.raises(HTTPException):
            account_linking.assert_email_link_permitted(
                _FakeUser(role="super_admin"),
                provider="pki",
                source_identifier="CN=Someone",
                email_verified=True,
                failure_detail="Invalid or missing client certificate",
            )

    def test_verified_non_privileged_link_is_permitted(self):
        """Returns quietly — the guard must not block a source that does assert."""
        account_linking.assert_email_link_permitted(
            _FakeUser(),
            provider="cloudidp",
            source_identifier="ext-1",
            email_verified=True,
            failure_detail="nope",
        )

    def test_refusal_is_audited(self):
        with patch.object(account_linking.audit_logger, "log") as log:
            with pytest.raises(HTTPException):
                account_linking.assert_email_link_permitted(
                    _FakeUser(role="super_admin"),
                    provider="ldap",
                    source_identifier="jdoe",
                    email_verified=True,
                    failure_detail="Incorrect username or password",
                )

        assert log.called, "a refused link must leave an audit record"
        assert log.call_args.kwargs["error_code"] == account_linking.LINK_REFUSED_ERROR_CODE

    def test_refusal_response_matches_the_ordinary_failure(self):
        """A distinct error would itself be the oracle the guard exists to remove."""
        with pytest.raises(HTTPException) as exc:
            account_linking.assert_email_link_permitted(
                _FakeUser(),
                provider="ldap",
                source_identifier="jdoe",
                email_verified=False,
                failure_detail="Incorrect username or password",
                failure_headers={"WWW-Authenticate": "Bearer"},
            )
        assert exc.value.detail == "Incorrect username or password"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.unit
class TestLdapSyncRefusesEmailMatch:
    def test_email_matched_local_account_is_not_converted(self):
        """The takeover path: directory ``mail`` pointed at an existing local user."""
        db = _FakeSession(_FakeUser())
        ldap_data = cast(
            "ldap_auth.LdapUserData",
            {
                "username": "attacker",
                "email": "victim@example.com",
                "full_name": "A",
                "is_admin": False,
                "groups": [],
            },
        )

        with (
            patch.object(ldap_auth, "_create_ldap_user") as create,
            patch.object(ldap_auth, "_convert_local_user_to_ldap") as convert,
        ):
            with pytest.raises(HTTPException) as exc:
                ldap_auth.sync_ldap_user_to_db(db, ldap_data)

        assert exc.value.status_code == 401
        assert not convert.called, "the local account must not be converted"
        assert not create.called, (
            "a refusal must not silently create a duplicate account for the same address"
        )

    def test_ldap_never_claims_a_verified_address(self):
        """``mail`` is a writable attribute; LDAP has no verified-address concept."""
        assert ldap_auth.LDAP_ASSERTS_EMAIL_VERIFIED is False


@pytest.mark.unit
class TestPkiSyncRefusesEmailMatch:
    def test_email_matched_local_account_is_not_converted(self):
        db = _FakeSession(_FakeUser())
        pki_data = cast(
            "pki_auth.PKIUserData",
            {
                "subject_dn": "CN=Attacker,OU=X",
                "common_name": "Attacker",
                "email": "victim@example.com",
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

        with (
            patch.object(pki_auth, "_create_pki_user") as create,
            patch.object(pki_auth, "_convert_local_user_to_pki") as convert,
        ):
            with pytest.raises(HTTPException) as exc:
                pki_auth.sync_pki_user_to_db(db, pki_data)

        assert exc.value.status_code == 401
        assert not convert.called
        assert not create.called

    def test_pki_address_is_not_treated_as_verified(self):
        """The address is parsed out of a proxy-supplied DN header, not a certificate."""
        assert pki_auth.PKI_ASSERTS_EMAIL_VERIFIED is False
