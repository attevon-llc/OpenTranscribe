"""E2E: the organization privacy panel is absent where organizations do not exist (#1335).

Organizations are a managed-edition capability (``organizations`` in
``app/core/capabilities.py`` is False for the community build, and the frontend's
``isCloudEdition`` is a build-time constant). This repository has no way to create an
organization or an organization administrator over HTTP on a community stack, so the
PRESENT half of the journey (an org admin sees the panel, edits it, a plain member does not)
cannot be exercised here. That half is covered at the unit level by ``orgPrivacyVisible``
(vitest) and the org-admin route tests (``tests/api``, which switch the capability on in
process). What an end-to-end test can and must prove on this stack is the ABSENT half, at
both layers a person could reach it by: the sidebar, and the API.

Each assertion has a positive twin that proves the thing being searched is really rendered,
so an empty sidebar or a dead route cannot make "absent" pass.
"""

from __future__ import annotations

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import open_settings
from privacy_ui import token_for

pytestmark = pytest.mark.settings

ORG_PANEL_LABELS = ("Organization privacy", "Privacy policy")


def _nav_texts(page: Page) -> list[str]:
    items = page.locator(".settings-sidebar .nav-item")
    expect(items.first).to_be_visible(timeout=10000)
    return [text.strip() for text in items.all_inner_texts()]


class TestOrgPrivacyPanelGating:
    def test_plain_user_has_no_organization_privacy_entry(self, second_user_page: Page) -> None:
        page = second_user_page
        open_settings(page)
        texts = _nav_texts(page)

        # Positive twin: the sidebar is rendered and the user's own privacy entry is there.
        assert any("Privacy & Redaction" in text for text in texts), texts
        for label in ORG_PANEL_LABELS:
            assert not any(label in text for text in texts), (label, texts)

    def test_admin_without_an_organization_has_no_organization_privacy_entry(
        self, authenticated_page: Page
    ) -> None:
        page = authenticated_page
        open_settings(page)
        texts = _nav_texts(page)

        assert any("Privacy & Redaction" in text for text in texts), texts
        for label in ORG_PANEL_LABELS:
            assert not any(label in text for text in texts), (label, texts)

    def test_org_policy_api_is_not_served_to_anyone(
        self, backend_url: str, admin_token: str, second_user: dict[str, str]
    ) -> None:
        """404 (the router is not mounted) for a super admin and for a plain user alike."""
        user_token = token_for(backend_url, second_user["email"], second_user["password"])
        for token in (admin_token, user_token):
            response = requests.get(
                f"{backend_url}/api/org-admin/policy",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            assert response.status_code == 404, response.text[:200]

        # Positive twin: the instance-level privacy route this stack DOES serve answers a
        # super admin, so a wrong prefix or a dead backend cannot produce the 404s above.
        served = requests.get(
            f"{backend_url}/api/admin/privacy-policy",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        assert served.status_code == 200, served.text[:200]
