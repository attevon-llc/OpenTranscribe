"""OIDC admission control: who may get an account here at all.

The gap: ``sync_oidc_user_to_db`` created an account for any identity that
completed the flow, so a deployment pointed at a corporate tenant provisioned
everyone in it. LDAP has had the equivalent guard since it shipped, and these
tests pin the two halves that matter — the semantics of the lists, and the fact
that the check runs *before* anything is written.
"""

# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (dict payloads, fake sessions, fake
# users) to signatures declaring the real dataclasses. Declared once here
# rather than as a cast at every call site — casts bury the assertion, and
# widening a production signature to suit a test is worse.
from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import HTTPException

from app.auth.oidc.admission import REASON_BLOCKED
from app.auth.oidc.admission import REASON_GROUPS_UNKNOWN
from app.auth.oidc.admission import REASON_NOT_ALLOWED
from app.auth.oidc.admission import assert_oidc_admission_permitted
from app.auth.oidc.admission import check_group_admission
from app.auth.oidc.admission import parse_group_list
from app.auth.oidc.config import OIDCConfig
from app.auth.oidc.provisioning import LINK_REFUSED_DETAIL
from app.auth.oidc.provisioning import sync_oidc_user_to_db
from app.models.user import User
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("oidc_admission")


def _claims(**overrides) -> dict:
    """A minimal verified-claim set, shaped like ``OIDCUserData``."""
    subject = f"sub-{uuid_pkg.uuid4().hex}"
    data: dict = {
        "oidc_subject": subject,
        "email": f"{subject}@example.com",
        "email_verified": True,
        "full_name": "Admitted Person",
        "username": subject,
        "is_admin": False,
        "roles": [],
        "groups_overage": False,
        "groupless_provider": False,
        "cert_dn": None,
        "cert_serial": None,
        "cert_issuer": None,
        "cert_org": None,
        "cert_ou": None,
        "cert_valid_from": None,
        "cert_valid_until": None,
        "cert_fingerprint": None,
    }
    data.update(overrides)
    return data


class TestParseGroupList:
    """Semicolons, because a brokered directory group value is a DN full of commas."""

    def test_empty_is_empty(self):
        assert parse_group_list("") == []
        assert parse_group_list(None) == []

    def test_single_value(self):
        assert parse_group_list("Transcribe-Users") == ["Transcribe-Users"]

    def test_whitespace_is_stripped_and_blanks_dropped(self):
        assert parse_group_list(" a ; ;b ") == ["a", "b"]

    def test_a_dn_survives_intact(self):
        dn = "CN=Transcribe Users,OU=Groups,DC=example,DC=com"
        assert parse_group_list(f"{dn};CN=Other,DC=example,DC=com") == [
            dn,
            "CN=Other,DC=example,DC=com",
        ]


class TestCheckGroupAdmission:
    def test_empty_allow_list_admits_everyone(self):
        """The upgrade-safe default. Empty means "no requirement", never "nobody"."""
        assert check_group_admission([], allowed_groups="", blocked_groups="") is None
        assert check_group_admission(["anything"], allowed_groups=None, blocked_groups=None) is None

    def test_non_empty_allow_list_requires_membership(self):
        assert (
            check_group_admission(
                ["Transcribe-Users"], allowed_groups="Transcribe-Users", blocked_groups=""
            )
            is None
        )
        assert (
            check_group_admission(
                ["Everyone"], allowed_groups="Transcribe-Users", blocked_groups=""
            )
            == REASON_NOT_ALLOWED
        )

    def test_no_claim_values_at_all_is_refused_by_a_non_empty_allow_list(self):
        """A provider that emits no groups cannot satisfy a group requirement."""
        assert (
            check_group_admission([], allowed_groups="Transcribe-Users", blocked_groups="")
            == REASON_NOT_ALLOWED
        )

    def test_any_one_of_several_allowed_groups_is_enough(self):
        assert check_group_admission(["b"], allowed_groups="a;b;c", blocked_groups="") is None

    def test_matching_is_case_insensitive_and_whitespace_tolerant(self):
        assert (
            check_group_admission(
                ["  transcribe-USERS "], allowed_groups="Transcribe-Users", blocked_groups=""
            )
            is None
        )

    def test_blocked_denies_even_when_also_allowed(self):
        """Blocked means DENIED, not "exempt from the allow-list"."""
        assert (
            check_group_admission(
                ["Contractors", "Transcribe-Users"],
                allowed_groups="Transcribe-Users",
                blocked_groups="Contractors",
            )
            == REASON_BLOCKED
        )

    def test_blocked_denies_when_the_allow_list_is_empty(self):
        """An empty allow-list admits everyone EXCEPT a blocked group."""
        assert (
            check_group_admission(["Contractors"], allowed_groups="", blocked_groups="Contractors")
            == REASON_BLOCKED
        )

    def test_blocked_is_evaluated_before_allowed(self):
        """Both refusals apply; the recorded reason must be the blocking one."""
        assert (
            check_group_admission(
                ["Contractors"], allowed_groups="Staff", blocked_groups="Contractors"
            )
            == REASON_BLOCKED
        )


class TestProvisioningRefusesBeforeItWrites:
    """The check has to run ahead of BOTH the create and the link branches."""

    def _cfg(self, **kwargs) -> OIDCConfig:
        return OIDCConfig(enabled=True, client_id="transcribe", **kwargs)

    def test_admitted_identity_is_provisioned(self, db_session):
        claims = _claims(roles=["Transcribe-Users"])
        user = sync_oidc_user_to_db(
            db_session, claims, self._cfg(allowed_groups="Transcribe-Users")
        )
        assert user.oidc_subject == claims["oidc_subject"]
        db_session.rollback()

    def test_refused_identity_creates_no_account(self, db_session):
        claims = _claims(roles=["Everyone"])

        with pytest.raises(HTTPException) as exc:
            sync_oidc_user_to_db(db_session, claims, self._cfg(allowed_groups="Transcribe-Users"))

        assert exc.value.status_code == 401
        assert (
            db_session.query(User).filter(User.oidc_subject == claims["oidc_subject"]).first()
            is None
        )
        assert db_session.query(User).filter(User.email == claims["email"]).first() is None
        db_session.rollback()

    def test_refusal_is_byte_identical_to_the_invalid_token_response(self, db_session):
        """A distinct message would answer "does this deployment know me?"."""
        with pytest.raises(HTTPException) as exc:
            sync_oidc_user_to_db(
                db_session, _claims(roles=["Everyone"]), self._cfg(allowed_groups="Staff")
            )
        assert exc.value.detail == LINK_REFUSED_DETAIL

    def test_a_blocked_identity_cannot_link_an_existing_account(self, db_session, normal_user):
        """Refusal precedes the email-match link, not just the create."""
        claims = _claims(email=str(normal_user.email), roles=["Contractors"])

        with pytest.raises(HTTPException) as exc:
            sync_oidc_user_to_db(db_session, claims, self._cfg(blocked_groups="Contractors"))

        assert exc.value.status_code == 401
        db_session.refresh(normal_user)
        assert normal_user.oidc_subject is None
        assert normal_user.auth_type == "local"
        db_session.rollback()

    def test_an_existing_oidc_account_is_re_checked_on_every_login(self, db_session):
        """Removing someone from the allowed group must lock them out, not just new users."""
        claims = _claims(roles=["Transcribe-Users"])
        sync_oidc_user_to_db(db_session, claims, self._cfg(allowed_groups="Transcribe-Users"))

        # Same subject, next login, now carrying a blocked group.
        with pytest.raises(HTTPException) as exc:
            sync_oidc_user_to_db(
                db_session,
                _claims(**{**claims, "roles": ["Contractors"]}),
                self._cfg(blocked_groups="Contractors"),
            )
        assert exc.value.status_code == 401
        db_session.rollback()

    def test_no_lists_configured_behaves_exactly_as_before(self, db_session):
        claims = _claims(roles=["whatever", "the", "idp", "said"])
        user = sync_oidc_user_to_db(db_session, claims, self._cfg())
        assert user.oidc_subject == claims["oidc_subject"]
        db_session.rollback()


class TestGeneratedEmailFallback:
    """A provider with no property mappings attached to its OAuth2 provider —
    confirmed against a real Authentik test container, issue #20/#14 — omits both
    ``email`` and ``preferred_username`` from the ID token. Falling back to only
    ``username`` left an empty local part (``"@oidc.local"``), an invalid address
    that 500'd every later ``/auth/me`` call rather than failing at provisioning
    time where it would have been visible.
    """

    def _cfg(self, **kwargs) -> OIDCConfig:
        return OIDCConfig(enabled=True, client_id="transcribe", **kwargs)

    def test_missing_email_and_username_falls_back_to_subject(self, db_session):
        claims = _claims(email="", username="")
        user = sync_oidc_user_to_db(db_session, claims, self._cfg())
        assert user.email == f"{claims['oidc_subject']}@oidc.local"
        db_session.rollback()

    def test_missing_email_with_username_present_is_unchanged(self, db_session):
        claims = _claims(email="", username="preferred-handle")
        user = sync_oidc_user_to_db(db_session, claims, self._cfg())
        assert user.email == "preferred-handle@oidc.local"
        db_session.rollback()

    def test_email_shaped_username_is_not_doubled(self, db_session):
        """A provider that sets ``preferred_username`` to the account's real email
        (common) must not produce ``"admin@example.com@oidc.local"`` — the exact
        failure reproduced against the real Authentik test container.
        """
        claims = _claims(email="", username="admin@example.com")
        user = sync_oidc_user_to_db(db_session, claims, self._cfg())
        assert user.email == "admin@oidc.local"
        db_session.rollback()


class TestGroupsWithheldNotEmpty:
    """Entra overage / Google's total absence of a groups claim (HANDOFF #40).

    ``roles=[]`` is ambiguous on its own — a real user with no group memberships
    looks identical to a provider that withheld them. ``groups_overage`` /
    ``groupless_provider`` disambiguate, and when a group-based list is
    configured this must fail loudly (a distinct reason, an ERROR-level log)
    rather than silently falling through ``check_group_admission`` as if the
    identity genuinely had zero groups.
    """

    def _cfg(self, **kwargs) -> OIDCConfig:
        return OIDCConfig(enabled=True, client_id="transcribe", **kwargs)

    def test_overage_is_refused_when_an_allow_list_is_configured(self):
        claims = _claims(roles=[], groups_overage=True)
        with pytest.raises(HTTPException) as exc:
            assert_oidc_admission_permitted(
                claims, self._cfg(allowed_groups="Transcribe-Users"), failure_detail="nope"
            )
        assert exc.value.status_code == 401

    def test_groupless_provider_is_refused_when_a_block_list_is_configured(self):
        claims = _claims(roles=[], groupless_provider=True)
        with pytest.raises(HTTPException):
            assert_oidc_admission_permitted(
                claims, self._cfg(blocked_groups="Contractors"), failure_detail="nope"
            )

    def test_overage_with_no_lists_configured_is_admitted(self):
        """Empty allow/deny lists mean 'no requirement' — withheld groups don't matter
        if nothing depends on them. No raise == admitted."""
        claims = _claims(roles=[], groups_overage=True)
        assert_oidc_admission_permitted(claims, self._cfg(), failure_detail="nope")

    def test_a_real_empty_roles_list_still_uses_the_ordinary_reason(self):
        """Without the overage/groupless flag, empty roles is an ordinary refusal —
        this behaviour must not change for every other provider."""
        claims = _claims(roles=[])
        with pytest.raises(HTTPException):
            assert_oidc_admission_permitted(
                claims, self._cfg(allowed_groups="Transcribe-Users"), failure_detail="nope"
            )

    def test_refusal_reason_is_distinct_from_not_allowed(self):
        """The audit trail must say 'we couldn't tell', not 'this identity is refused' —
        and the HTTP response must still be the generic, byte-identical detail (no
        account-existence oracle), same as every other admission refusal."""
        assert REASON_GROUPS_UNKNOWN != REASON_NOT_ALLOWED

        claims = _claims(roles=[], groups_overage=True)
        with pytest.raises(HTTPException) as exc:
            assert_oidc_admission_permitted(
                claims, self._cfg(allowed_groups="Transcribe-Users"), failure_detail="nope"
            )
        assert exc.value.detail == "nope"


class TestTheAdminSettingReachesTheCheck:
    """The behavioural half the dead-configuration test exists to demand."""

    def test_saving_oidc_allowed_groups_refuses_a_non_member(self, db_session, super_admin_user):
        AuthConfigService.bulk_update_category(
            db=db_session,
            category="oidc",
            config_dict={"oidc_allowed_groups": "Transcribe-Users"},
            user_id=super_admin_user.id,
        )

        cfg = OIDCConfig.from_db(db_session)
        assert cfg.allowed_groups == "Transcribe-Users"

        with pytest.raises(HTTPException):
            sync_oidc_user_to_db(db_session, _claims(roles=["Everyone"]), cfg)
        db_session.rollback()

    def test_saving_oidc_blocked_groups_refuses_a_member(self, db_session, super_admin_user):
        AuthConfigService.bulk_update_category(
            db=db_session,
            category="oidc",
            config_dict={"oidc_blocked_groups": "Contractors"},
            user_id=super_admin_user.id,
        )

        cfg = OIDCConfig.from_db(db_session)
        assert cfg.blocked_groups == "Contractors"

        with pytest.raises(HTTPException):
            sync_oidc_user_to_db(db_session, _claims(roles=["Contractors"]), cfg)
        db_session.rollback()
