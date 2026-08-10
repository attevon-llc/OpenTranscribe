"""Extracting application data from an already-verified SAML assertion.

``extract_saml_user_data`` never touches signatures or XML — python3-saml has
already done that by the time this runs — so these tests stub the ``Auth`` object
rather than constructing a signed assertion (the signature-verification path itself
is python3-saml's, exercised against a real IdP in the E2E round, task #20).
"""

# mypy: disable-error-code="arg-type"
# _FakeAuth is a structural stand-in for OneLogin_Saml2_Auth, not a subclass —
# declared once here rather than as a cast at every call site.
from __future__ import annotations

from app.auth.saml.assertion import SAML_ASSERTS_EMAIL_VERIFIED
from app.auth.saml.assertion import extract_saml_user_data
from app.auth.saml.config import SAMLConfig


class _FakeAuth:
    """Minimal stand-in for ``onelogin.saml2.auth.OneLogin_Saml2_Auth`` post-verify."""

    def __init__(self, nameid: str, attributes: dict[str, list[str]]):
        self._nameid = nameid
        self._attributes = attributes

    def get_nameid(self) -> str:
        return self._nameid

    def get_attribute(self, name: str) -> list[str] | None:
        return self._attributes.get(name)


def _cfg(**overrides) -> SAMLConfig:
    base = dict(
        email_attribute="email",
        name_attribute="name",
        groups_attribute="groups",
        admin_group="",
    )
    base.update(overrides)
    return SAMLConfig(**base)


class TestExtractSamlUserData:
    def test_email_verified_is_always_false(self):
        """SAML has no `email_verified` equivalent — hardcoded closed, matching
        PKI/LDAP, never an admin-togglable setting."""
        assert SAML_ASSERTS_EMAIL_VERIFIED is False
        auth = _FakeAuth("subj-1", {"email": ["a@example.com"]})
        data = extract_saml_user_data(auth, _cfg())
        assert data["email_verified"] is False

    def test_reads_the_nameid_as_the_subject(self):
        auth = _FakeAuth("subj-42", {})
        data = extract_saml_user_data(auth, _cfg())
        assert data["saml_subject"] == "subj-42"

    def test_email_falls_back_to_the_nameid_when_the_attribute_is_absent(self):
        auth = _FakeAuth("subj-nameid@example.com", {})
        data = extract_saml_user_data(auth, _cfg())
        assert data["email"] == "subj-nameid@example.com"

    def test_email_attribute_wins_over_the_nameid_when_present(self):
        auth = _FakeAuth("subj-1", {"email": ["real@example.com"]})
        data = extract_saml_user_data(auth, _cfg())
        assert data["email"] == "real@example.com"

    def test_multi_valued_groups_attribute_is_read_in_full(self):
        auth = _FakeAuth("subj-1", {"groups": ["Staff", "Engineering"]})
        data = extract_saml_user_data(auth, _cfg())
        assert data["groups"] == ["Staff", "Engineering"]

    def test_no_groups_attribute_is_an_empty_list_not_an_error(self):
        auth = _FakeAuth("subj-1", {})
        data = extract_saml_user_data(auth, _cfg())
        assert data["groups"] == []

    def test_admin_group_membership_grants_is_admin(self):
        auth = _FakeAuth("subj-1", {"groups": ["Staff", "OT-Admins"]})
        data = extract_saml_user_data(auth, _cfg(admin_group="OT-Admins"))
        assert data["is_admin"] is True

    def test_admin_group_match_is_case_insensitive(self):
        auth = _FakeAuth("subj-1", {"groups": ["ot-admins"]})
        data = extract_saml_user_data(auth, _cfg(admin_group="OT-Admins"))
        assert data["is_admin"] is True

    def test_no_admin_group_configured_never_grants_admin(self):
        auth = _FakeAuth("subj-1", {"groups": ["OT-Admins"]})
        data = extract_saml_user_data(auth, _cfg(admin_group=""))
        assert data["is_admin"] is False

    def test_missing_admin_group_membership_does_not_grant_admin(self):
        auth = _FakeAuth("subj-1", {"groups": ["Staff"]})
        data = extract_saml_user_data(auth, _cfg(admin_group="OT-Admins"))
        assert data["is_admin"] is False

    def test_full_name_attribute_is_read(self):
        auth = _FakeAuth("subj-1", {"name": ["Jane Doe"]})
        data = extract_saml_user_data(auth, _cfg())
        assert data["full_name"] == "Jane Doe"

    def test_missing_full_name_attribute_is_empty_string(self):
        auth = _FakeAuth("subj-1", {})
        data = extract_saml_user_data(auth, _cfg())
        assert data["full_name"] == ""
