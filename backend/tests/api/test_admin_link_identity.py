"""P1.3 — the operator remedy `auth/account_linking.py` documents.

When a source cannot assert `email_verified` (Authentik hardcodes it `false` for
every account) the automatic email-match link is refused and the login fails, by
design. `PUT /api/admin/users/{uuid}/link-identity` is the explicit alternative: a
super_admin sets the provider's own identifier on the account so the *next* login
matches by that identifier and never reaches the email-match branch at all.

Issue #912 narrowing: a `local` account carrying an external identifier
(`oidc_subject` / `ldap_uid` / `pki_subject_dn`) was a reachable, contradictory
state — a login resolves such an account through the provider-id branch and then
runs it through every non-local login rule, while `auth_type='local'` still tells
everything else the account has a usable local password. This endpoint is the sole
producer of that state via a deliberate admin action (JIT provisioning stamps the
identifier AND flips `auth_type` in the same block), so it is also the sole fix
point: linking an identifier onto a `local` account now converts `auth_type` to the
provider being linked and revokes the account's sessions. An account whose
`auth_type` is already non-local (including a SCIM-provisioned `local` + `external_id`
row — a deliberately different, still-legitimate state `_EXTERNAL_IDENTITY_COLUMNS`
does not touch) is left alone.
"""

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.api.endpoints import admin as admin_module
from app.auth.audit import AuditOutcome
from app.auth.constants import AUTH_TYPE_LDAP
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import AUTH_TYPE_OIDC
from app.auth.constants import AUTH_TYPE_PKI
from app.auth.roles import ROLE_SUPER_ADMIN
from app.core.security import get_password_hash
from app.models.refresh_token import RefreshToken
from app.models.user import User


def _make_second_super_admin(db_session) -> User:
    """A super_admin distinct from the caller — needed to tell actor from target."""
    user = User(
        email=f"extra-sa-{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="Extra Super Admin",
        hashed_password=get_password_hash("extrapass123"),
        is_active=True,
        is_superuser=True,
        role=ROLE_SUPER_ADMIN,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _create_refresh_token(db_session, user) -> RefreshToken:
    token = RefreshToken(
        user_id=user.id,
        token_hash=f"hash-{uuid_pkg.uuid4().hex}",
        jti=str(uuid_pkg.uuid4()),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db_session.add(token)
    db_session.commit()
    return token


class TestLinkExternalIdentity:
    def test_super_admin_can_link_an_oidc_subject(
        self, client, super_admin_token_headers, normal_user, db_session
    ):
        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|abc123"},
        )
        assert response.status_code == 200, response.json()
        body = response.json()
        # This exact-dict assertion is deliberately kept exact-equality (not a
        # subset check): the contract just grew a field, and losing that on a
        # sloppy assertion here is exactly how #912's fix would go unnoticed.
        assert body == {
            "success": True,
            "provider": "oidc",
            "identifier": "authentik|abc123",
            "auth_type": "oidc",
        }

        db_session.refresh(normal_user)
        assert normal_user.oidc_subject == "authentik|abc123"

    def test_super_admin_can_link_an_ldap_uid(
        self, client, super_admin_token_headers, normal_user, db_session
    ):
        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "ldap", "identifier": "jdoe"},
        )
        assert response.status_code == 200, response.json()

        db_session.refresh(normal_user)
        assert normal_user.ldap_uid == "jdoe"

    def test_super_admin_can_link_a_pki_subject_dn(
        self, client, super_admin_token_headers, normal_user, db_session
    ):
        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "pki", "identifier": "CN=John Doe,OU=Staff,O=Example,C=US"},
        )
        assert response.status_code == 200, response.json()

        db_session.refresh(normal_user)
        assert normal_user.pki_subject_dn == "CN=John Doe,OU=Staff,O=Example,C=US"

    def test_plain_admin_is_forbidden(self, client, admin_token_headers, normal_user):
        """This grants login capability — the same tier as a role change, not user management."""
        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|abc123"},
        )
        assert response.status_code == 403

    def test_super_admin_target_is_refused(
        self, client, super_admin_token_headers, super_admin_user
    ):
        """super_admin is local-only by architectural invariant — never linkable."""
        response = client.put(
            f"/api/admin/users/{super_admin_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|abc123"},
        )
        assert response.status_code == 400
        assert "local-only" in response.json()["detail"]

    def test_identifier_already_linked_to_another_user_is_a_conflict(
        self, client, super_admin_token_headers, normal_user, other_user, db_session
    ):
        other_user.oidc_subject = "authentik|taken"
        db_session.commit()

        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|taken"},
        )
        assert response.status_code == 409

        db_session.refresh(normal_user)
        assert normal_user.oidc_subject is None

    def test_unknown_user_is_404(self, client, super_admin_token_headers):
        response = client.put(
            "/api/admin/users/00000000-0000-7000-8000-000000000000/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|abc123"},
        )
        assert response.status_code == 404

    def test_unsupported_provider_is_rejected(self, client, super_admin_token_headers, normal_user):
        """`local` and `proxy` are not linkable identities in this sense."""
        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "local", "identifier": "whatever"},
        )
        assert response.status_code == 422

    def test_blank_identifier_is_rejected(self, client, super_admin_token_headers, normal_user):
        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "   "},
        )
        assert response.status_code == 422

    def test_relinking_the_same_user_to_their_own_current_identifier_is_a_noop_success(
        self, client, super_admin_token_headers, normal_user, db_session
    ):
        """Re-running the same link must not trip the conflict check against itself."""
        normal_user.oidc_subject = "authentik|abc123"
        db_session.commit()

        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|abc123"},
        )
        assert response.status_code == 200, response.json()


class TestLinkIdentityConvertsAuthType:
    """#912: linking an identifier onto a `local` account must convert `auth_type`.

    Before this fix, `link-identity` set the provider column WITHOUT touching
    `auth_type`, leaving a `local` account carrying (say) an `ldap_uid` — a state
    that resolves through the provider-id login branch while every other reader of
    `auth_type` still believes the account is local-password-authenticated.
    """

    @pytest.mark.parametrize(
        ("provider", "column", "identifier"),
        [
            (AUTH_TYPE_OIDC, "oidc_subject", "authentik|conv-1"),
            (AUTH_TYPE_LDAP, "ldap_uid", "conv-jdoe"),
            (AUTH_TYPE_PKI, "pki_subject_dn", "CN=Conv User,O=Example,C=US"),
        ],
    )
    def test_linking_a_local_account_converts_its_auth_type(
        self,
        client,
        super_admin_token_headers,
        normal_user,
        db_session,
        provider,
        column,
        identifier,
    ):
        assert str(normal_user.auth_type) == AUTH_TYPE_LOCAL

        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": provider, "identifier": identifier},
        )
        assert response.status_code == 200, response.json()
        assert response.json()["auth_type"] == provider

        db_session.refresh(normal_user)
        assert str(normal_user.auth_type) == provider
        assert getattr(normal_user, column) == identifier

    def test_linking_does_not_overwrite_an_existing_external_auth_type(
        self, client, super_admin_token_headers, normal_user, db_session
    ):
        """MUST-NOT-FIRE control.

        A `pki` account with `allow_local_fallback=True` is exactly the row
        `account_security_service.assert_local_fallback_settable` exists to protect
        — an unconditional `auth_type` write here would silently demote it to
        `ldap` the next time an admin linked an (unrelated) ldap identifier. This
        test fails against a naive unconditional implementation, which is the whole
        reason `admin_link_external_identity` guards the write on
        ``previous_auth_type == AUTH_TYPE_LOCAL``.
        """
        normal_user.auth_type = AUTH_TYPE_PKI
        normal_user.pki_subject_dn = "CN=Existing,O=Example,C=US"
        normal_user.allow_local_fallback = True
        db_session.commit()

        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "ldap", "identifier": "unrelated-ldap-uid"},
        )
        assert response.status_code == 200, response.json()
        assert response.json()["auth_type"] == AUTH_TYPE_PKI

        db_session.refresh(normal_user)
        assert str(normal_user.auth_type) == AUTH_TYPE_PKI
        assert normal_user.allow_local_fallback is True
        # The column write itself is unconditional — only auth_type is guarded.
        assert normal_user.ldap_uid == "unrelated-ldap-uid"

    def test_linking_a_local_account_revokes_its_sessions(
        self, client, super_admin_token_headers, normal_user, db_session
    ):
        token = _create_refresh_token(db_session, normal_user)

        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|revoke-me"},
        )
        assert response.status_code == 200, response.json()

        db_session.refresh(token)
        assert token.revoked_at is not None, "converting auth_type must revoke existing sessions"

    def test_linking_an_already_external_account_leaves_its_sessions_alone(
        self, client, super_admin_token_headers, normal_user, db_session
    ):
        """Green control: no conversion happens, so nothing should be revoked."""
        normal_user.auth_type = AUTH_TYPE_PKI
        normal_user.pki_subject_dn = "CN=Already External,O=Example,C=US"
        db_session.commit()

        token = _create_refresh_token(db_session, normal_user)

        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "ldap", "identifier": "unrelated-uid"},
        )
        assert response.status_code == 200, response.json()

        db_session.refresh(token)
        assert token.revoked_at is None, "an already-external account's sessions must be untouched"

    def test_the_conversion_is_recorded_in_the_audit_details(
        self, client, super_admin_token_headers, normal_user, db_session, monkeypatch
    ):
        captured: list[dict] = []
        monkeypatch.setattr(
            admin_module.audit_logger, "log", lambda **kwargs: captured.append(kwargs)
        )

        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|audited"},
        )
        assert response.status_code == 200, response.json()

        assert captured, "linking emitted no audit record"
        details = captured[-1]["details"]
        assert details["previous_auth_type"] == AUTH_TYPE_LOCAL
        assert details["auth_type"] == "oidc"

    def test_the_super_admin_target_refusal_is_audited(
        self, client, super_admin_token_headers, super_admin_user, db_session, monkeypatch
    ):
        """Nothing was emitted here before this fix — the refusal was silent.

        The target must be a super_admin DIFFERENT from the caller, or ``user_id
        != target_user_id`` would trivially pass even if the implementation wrote
        the wrong one — the actor and the subject happening to be the same
        account can't distinguish the two.
        """
        target = _make_second_super_admin(db_session)
        captured: list[dict] = []
        monkeypatch.setattr(
            admin_module.audit_logger, "log", lambda **kwargs: captured.append(kwargs)
        )

        response = client.put(
            f"/api/admin/users/{target.uuid}/link-identity",
            headers=super_admin_token_headers,
            json={"provider": "oidc", "identifier": "authentik|nope"},
        )
        assert response.status_code == 400

        assert captured, "the super_admin-target refusal emitted no audit record"
        event = captured[-1]
        assert event["outcome"] == AuditOutcome.FAILURE
        assert event["details"]["action"] == "link_identity_super_admin_target"
        assert event["target_user_id"] == target.id
        assert event["user_id"] == super_admin_user.id, "user_id must be the ACTOR"
