"""SAML admission control: who may get an account here at all.

Same gap OIDC admission closes (see ``test_oidc_admission.py``), for a fourth
identity source. The group-list semantics are shared code
(``oidc.admission.check_group_admission``, already exhaustively covered there) —
these tests pin the SAML-specific wiring: that ``sync_saml_user_to_db`` calls it
before writing anything, and that ``SAML_ASSERTS_EMAIL_VERIFIED`` (always False)
keeps the email-match account-linking guard closed for every SAML deployment.
"""

# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (dict payloads shaped like SAMLUserData)
# to signatures declaring the real TypedDict. Declared once here rather than as a
# cast at every call site.
from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import HTTPException

from app.auth.saml.assertion import SAML_ASSERTS_EMAIL_VERIFIED
from app.auth.saml.config import SAMLConfig
from app.auth.saml.provisioning import LINK_REFUSED_DETAIL
from app.auth.saml.provisioning import sync_saml_user_to_db
from app.models.user import User

pytestmark = pytest.mark.xdist_group("saml_admission")


def _assertion(**overrides) -> dict:
    """A minimal verified-assertion payload, shaped like ``SAMLUserData``."""
    subject = f"subj-{uuid_pkg.uuid4().hex}"
    data: dict = {
        "saml_subject": subject,
        "email": f"{subject}@example.com",
        "email_verified": SAML_ASSERTS_EMAIL_VERIFIED,
        "full_name": "Admitted Person",
        "groups": [],
        "is_admin": False,
    }
    data.update(overrides)
    return data


class TestProvisioningRefusesBeforeItWrites:
    def _cfg(self, **kwargs) -> SAMLConfig:
        return SAMLConfig(enabled=True, sp_entity_id="https://sp.example.com", **kwargs)

    def test_admitted_identity_is_provisioned(self, db_session):
        assertion = _assertion(groups=["Transcribe-Users"])
        user = sync_saml_user_to_db(
            db_session, assertion, self._cfg(allowed_groups="Transcribe-Users")
        )
        assert user.saml_subject == assertion["saml_subject"]
        db_session.rollback()

    def test_refused_identity_creates_no_account(self, db_session):
        assertion = _assertion(groups=["Everyone"])

        with pytest.raises(HTTPException) as exc:
            sync_saml_user_to_db(
                db_session, assertion, self._cfg(allowed_groups="Transcribe-Users")
            )

        assert exc.value.status_code == 401
        assert (
            db_session.query(User).filter(User.saml_subject == assertion["saml_subject"]).first()
            is None
        )
        db_session.rollback()

    def test_refusal_is_byte_identical_to_the_invalid_assertion_response(self, db_session):
        with pytest.raises(HTTPException) as exc:
            sync_saml_user_to_db(
                db_session, _assertion(groups=["Everyone"]), self._cfg(allowed_groups="Staff")
            )
        assert exc.value.detail == LINK_REFUSED_DETAIL

    def test_a_blocked_identity_cannot_link_an_existing_account(self, db_session, normal_user):
        """SAML asserts no verified email, so this would already be refused by
        account_linking even with no admission rule at all — this pins the
        admission refusal fires first, before the link attempt is even reached."""
        assertion = _assertion(email=str(normal_user.email), groups=["Contractors"])

        with pytest.raises(HTTPException) as exc:
            sync_saml_user_to_db(db_session, assertion, self._cfg(blocked_groups="Contractors"))

        assert exc.value.status_code == 401
        db_session.refresh(normal_user)
        assert normal_user.saml_subject is None
        assert normal_user.auth_type == "local"
        db_session.rollback()

    def test_no_lists_configured_behaves_exactly_as_before(self, db_session):
        assertion = _assertion(groups=["whatever", "the", "idp", "said"])
        user = sync_saml_user_to_db(db_session, assertion, self._cfg())
        assert user.saml_subject == assertion["saml_subject"]
        db_session.rollback()


class TestEmailVerifiedNeverOpensTheAccountLinkGuard:
    """SAML has no verified-email concept, so a SAML identity must never take over
    a pre-existing local account by email match — even with an email that happens
    to coincide and no admission rule configured at all."""

    def test_an_email_match_to_an_existing_local_account_is_refused(self, db_session, normal_user):
        assertion = _assertion(email=str(normal_user.email))

        with pytest.raises(HTTPException) as exc:
            sync_saml_user_to_db(db_session, assertion, SAMLConfig(enabled=True))

        assert exc.value.status_code == 401
        db_session.refresh(normal_user)
        assert normal_user.saml_subject is None
        db_session.rollback()

    def test_an_email_match_to_a_super_admin_is_refused_unconditionally(
        self, db_session, super_admin_user
    ):
        assertion = _assertion(email=str(super_admin_user.email))

        with pytest.raises(HTTPException) as exc:
            sync_saml_user_to_db(db_session, assertion, SAMLConfig(enabled=True))

        assert exc.value.status_code == 401
        db_session.refresh(super_admin_user)
        assert super_admin_user.saml_subject is None
        assert super_admin_user.role == "super_admin"
        db_session.rollback()
