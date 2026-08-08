"""Detecting when a provider withheld group membership rather than the identity
genuinely having none (HANDOFF #40).

Entra ID omits the ``groups`` claim entirely above 200 memberships, replacing it
with the aggregated/distributed-claims markers ``_claim_names``/``_claim_sources``
(and, on some token versions, a bare ``hasgroups: true``). Google never emits a
groups claim at all, on any token, from any tenant. Both look identical to "this
user has no groups" unless something checks for them — which is exactly the shape
that silently demotes an admin (``cfg.admin_role in roles`` quietly becomes
``False``) or silently bypasses/violates an ``oidc_allowed_groups`` /
``oidc_blocked_groups`` list (``app/auth/oidc/admission.py``).
"""

from __future__ import annotations

from app.auth.oidc.claims import _has_groups_overage
from app.auth.oidc.claims import _is_google_issuer


class TestHasGroupsOverage:
    def test_no_markers_is_not_overage(self):
        assert _has_groups_overage({"sub": "abc", "groups": ["a", "b"]}, "groups") is False

    def test_claim_names_naming_the_roles_claim_is_overage(self):
        payload = {
            "sub": "abc",
            "_claim_names": {"groups": "src1"},
            "_claim_sources": {"src1": {"endpoint": "https://graph.microsoft.com/v1.0/..."}},
        }
        assert _has_groups_overage(payload, "groups") is True

    def test_claim_names_for_a_different_claim_is_not_overage(self):
        """Only the configured roles claim's absence matters — some other aggregated
        claim (e.g. a custom attribute) is not this deployment's problem."""
        payload = {"sub": "abc", "_claim_names": {"unrelated_claim": "src1"}}
        assert _has_groups_overage(payload, "groups") is False

    def test_hasgroups_true_is_overage(self):
        assert _has_groups_overage({"sub": "abc", "hasgroups": True}, "groups") is True

    def test_dotted_roles_claim_checks_the_top_level_segment(self):
        """`realm_access.roles` -> Entra would name `realm_access` in `_claim_names`,
        not the full dotted path (the claim being withheld is the top-level one)."""
        payload = {"sub": "abc", "_claim_names": {"realm_access": "src1"}}
        assert _has_groups_overage(payload, "realm_access.roles") is True

    def test_neither_marker_present_is_not_overage(self):
        assert _has_groups_overage({"sub": "abc"}, "groups") is False


class TestIsGoogleIssuer:
    def test_https_spelling(self):
        assert _is_google_issuer("https://accounts.google.com") is True

    def test_bare_spelling(self):
        assert _is_google_issuer("accounts.google.com") is True

    def test_other_issuers_are_not_google(self):
        assert _is_google_issuer("https://login.microsoftonline.com/tenant/v2.0") is False
        assert _is_google_issuer("https://idp.example.com/realms/opentranscribe") is False
