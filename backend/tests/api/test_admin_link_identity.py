"""P1.3 — the operator remedy `auth/account_linking.py` documents.

When a source cannot assert `email_verified` (Authentik hardcodes it `false` for
every account) the automatic email-match link is refused and the login fails, by
design. `PUT /api/admin/users/{uuid}/link-identity` is the explicit alternative: a
super_admin sets the provider's own identifier on the account so the *next* login
matches by that identifier and never reaches the email-match branch at all.
"""


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
        assert body == {
            "success": True,
            "provider": "oidc",
            "identifier": "authentik|abc123",
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
