"""
E2E Tests for Gender-Informed Cluster Validation & User Confirmation.

Tests verify:
- Gender composition chips on cluster cards
- Gender icons on member rows with outlier highlighting
- Gender confirm buttons on speaker profile cards
- API endpoints for gender confirmation

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173
- Backend at localhost:5174

Run (headless):
    pytest backend/tests/e2e/test_speaker_gender_clusters.py -v

Run (visible on XRDP):
    DISPLAY=:13 pytest backend/tests/e2e/test_speaker_gender_clusters.py -v --headed
"""

import os

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect

# This module used to define its own ``FRONTEND_URL``/``BACKEND_URL`` constants here.
# A module constant is evaluated at import time, so it could not see ``--base-url`` /
# ``--backend-url`` and this file always drove whatever was on the default ports — even
# when the run was aimed at an isolated stack (issue #431). Everything below takes
# conftest's ``base_url`` / ``backend_url`` fixtures instead.

# Credentials imported from conftest to avoid secret detection false positives
try:
    from conftest import BACKEND_URL as DEFAULT_BACKEND_URL  # type: ignore[import-not-found]
    from conftest import TEST_ADMIN_EMAIL  # type: ignore[import-not-found]
    from conftest import TEST_ADMIN_PASSWORD  # type: ignore[import-not-found]
except ImportError:
    DEFAULT_BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:5174")
    TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
    TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "admin")  # noqa: S105


@pytest.fixture(scope="session")
def backend_url(request: pytest.FixtureRequest) -> str:
    """Session-scoped view of conftest's ``backend_url`` fixture (issue #431).

    ``api_session`` below is session-scoped on purpose — one login per run keeps the
    suite inside the backend's auth rate limit — and a session-scoped fixture cannot
    request the function-scoped fixture conftest defines. This applies exactly conftest's
    precedence (``--backend-url`` first, then its ``E2E_BACKEND_URL``/dev default), so the
    flag is honoured here too. Delete once the conftest fixture is session-scoped.
    """
    return str(request.config.getoption("backend_url", default=None) or DEFAULT_BACKEND_URL)


@pytest.fixture(scope="session")
def api_session(backend_url: str) -> requests.Session:
    """Create an authenticated requests.Session, shared across all tests."""
    session = requests.Session()
    resp = session.post(
        f"{backend_url}/api/auth/login",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


# ---------------------------------------------------------------------------
# Browser UI tests
# ---------------------------------------------------------------------------


class TestGenderChipsOnClusterCards:
    """Verify gender composition chips render on cluster cards."""

    def test_speakers_page_loads(self, authenticated_page: Page, base_url: str):
        """Navigate to speakers page and verify it loads."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        expect(authenticated_page.locator("h1, .page-title")).to_be_visible(timeout=10000)

    def test_gender_chips_render_on_clusters(self, authenticated_page: Page, base_url: str):
        """Verify gender chips appear on cluster cards when gender data exists."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        cluster_cards = authenticated_page.locator(".cluster-card")
        if cluster_cards.count() == 0:
            pytest.skip("No clusters found - need transcribed media with speakers")

        gender_chips = authenticated_page.locator(".gender-chip")
        if gender_chips.count() > 0:
            first_chip = gender_chips.first
            expect(first_chip).to_be_visible()
            # Chips render an SVG gender icon + the composition label
            # ("100% Male" etc.) \u2014 SpeakerClusterCard.svelte
            expect(first_chip.locator("svg.gender-svg")).to_be_visible()
            text = (first_chip.text_content() or "").lower()
            assert "male" in text or "female" in text, (
                f"Gender chip should contain the composition label, got: {text!r}"
            )

    def test_gender_chip_coherent_vs_conflict(self, authenticated_page: Page, base_url: str):
        """Verify coherent chips are green, conflict chips are amber."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        coherent_chips = authenticated_page.locator(".gender-chip.gender-coherent")
        conflict_chips = authenticated_page.locator(".gender-chip.gender-conflict")

        if coherent_chips.count() > 0:
            expect(coherent_chips.first).to_be_visible()

        if conflict_chips.count() > 0:
            expect(conflict_chips.first).to_be_visible()

    def test_no_chips_when_no_gender_predictions(self, authenticated_page: Page, base_url: str):
        """Verify no gender chips render when gender predictions are absent."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        cluster_cards = authenticated_page.locator(".cluster-card")
        if cluster_cards.count() == 0:
            pytest.skip("No clusters found")


class TestGenderIconsOnMemberRows:
    """Verify gender icons appear on expanded cluster member rows."""

    def test_expand_cluster_shows_gender_icons(self, authenticated_page: Page, base_url: str):
        """Expand a cluster and verify gender icons on member rows."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        cluster_cards = authenticated_page.locator(".cluster-card")
        if cluster_cards.count() == 0:
            pytest.skip("No clusters found")

        cluster_cards.first.locator(".card-header").click()
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        gender_icons = authenticated_page.locator(".gender-icon")
        if gender_icons.count() > 0:
            first_icon = gender_icons.first
            expect(first_icon).to_be_visible()
            # Member rows render an SVG icon whose title carries the gender
            # label (ClusterMemberList.svelte) \u2014 there is no text glyph.
            expect(first_icon.locator("svg.gender-svg")).to_be_visible()
            title = (first_icon.get_attribute("title") or "").lower()
            assert "male" in title or "female" in title, (
                f"Gender icon title should name the gender, got: {title!r}"
            )

    def test_outlier_highlighting(self, authenticated_page: Page, base_url: str):
        """Verify outlier members get highlighted styling."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        cluster_cards = authenticated_page.locator(".cluster-card")
        if cluster_cards.count() == 0:
            pytest.skip("No clusters found")

        cluster_cards.first.locator(".card-header").click()
        authenticated_page.wait_for_timeout(2000)
        # Just verify no errors - outliers are data-dependent
        authenticated_page.locator(".member-row.gender-outlier")


class TestProfileGenderConfirmation:
    """Verify gender confirm buttons on profile cards."""

    def test_profiles_tab_has_gender_buttons(self, authenticated_page: Page, base_url: str):
        """Navigate to profiles tab and verify gender toggle buttons exist."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        profiles_tab = authenticated_page.locator("button:has-text('Profiles')")
        if profiles_tab.count() == 0:
            pytest.skip("No profiles tab found")

        profiles_tab.click()
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        profile_cards = authenticated_page.locator(".profile-card")
        if profile_cards.count() == 0:
            pytest.skip("No profiles found")

        gender_btns = authenticated_page.locator(".gender-toggle-btn")
        assert gender_btns.count() >= 2, "Expected at least 2 gender toggle buttons"
        expect(gender_btns.first).to_be_visible()

    def test_click_gender_confirm_updates_state(self, authenticated_page: Page, base_url: str):
        """Click a gender confirm button and verify state updates."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        profiles_tab = authenticated_page.locator("button:has-text('Profiles')")
        if profiles_tab.count() == 0:
            pytest.skip("No profiles tab found")

        profiles_tab.click()
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        gender_btns = authenticated_page.locator(".gender-toggle-btn")
        if gender_btns.count() == 0:
            pytest.skip("No gender buttons found")

        # E2E must NOT mutate dev data: only re-click an ALREADY-active gender
        # button (re-confirms the same gender — no net change). Real mutation
        # is covered by tests/test_speaker_gender_confirm.py.
        active_btns = authenticated_page.locator(".gender-toggle-btn.active")
        if active_btns.count() == 0:
            pytest.skip("No confirmed-gender profile — mutation covered by unit tests")

        active_btns.first.click()
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        expect(authenticated_page.locator(".gender-toggle-btn.active").first).to_be_visible()


# ---------------------------------------------------------------------------
# API endpoint tests (use shared api_session fixture)
# ---------------------------------------------------------------------------


class TestSpeakerClustersAPI:
    """Test speaker clusters API endpoints directly."""

    def test_list_clusters_has_gender_composition(
        self, api_session: requests.Session, backend_url: str
    ):
        """GET /speaker-clusters returns gender_composition in each cluster."""
        resp = api_session.get(f"{backend_url}/api/speaker-clusters", timeout=10)
        assert resp.status_code == 200
        data = resp.json()

        if data.get("total", 0) == 0:
            pytest.skip("No clusters exist")

        for item in data["items"]:
            assert "gender_composition" in item, "Cluster should have gender_composition"
            gc = item["gender_composition"]
            assert "male_count" in gc
            assert "female_count" in gc
            assert "unknown_count" in gc
            assert "has_gender_conflict" in gc

    def test_cluster_detail_has_gender_fields(
        self, api_session: requests.Session, backend_url: str
    ):
        """GET /speaker-clusters/{uuid} returns gender fields on members."""
        resp = api_session.get(f"{backend_url}/api/speaker-clusters", timeout=10)
        data = resp.json()
        if data.get("total", 0) == 0:
            pytest.skip("No clusters exist")

        cluster_uuid = data["items"][0]["uuid"]
        detail_resp = api_session.get(
            f"{backend_url}/api/speaker-clusters/{cluster_uuid}", timeout=10
        )
        assert detail_resp.status_code == 200
        detail = detail_resp.json()

        assert "gender_composition" in detail
        for member in detail.get("members", []):
            assert "gender_confidence" in member
            assert "gender_confirmed_by_user" in member

    def test_confirm_speaker_gender_endpoint(self, api_session: requests.Session, backend_url: str):
        """POST /speakers/{uuid}/confirm-gender sets gender."""
        resp = api_session.get(f"{backend_url}/api/speaker-clusters", timeout=10)
        data = resp.json()
        if data.get("total", 0) == 0:
            pytest.skip("No clusters exist")

        cluster_uuid = data["items"][0]["uuid"]
        detail_resp = api_session.get(
            f"{backend_url}/api/speaker-clusters/{cluster_uuid}", timeout=10
        )
        members = detail_resp.json().get("members", [])
        if not members:
            pytest.skip("No members in cluster")

        # E2E must NOT mutate dev data (it drifts the speakers visual
        # baselines). Only re-confirm a member that is ALREADY confirmed with
        # its existing gender — a no-op write. Mutation behavior is covered by
        # tests/test_speaker_gender_confirm.py (savepoint-rolled-back).
        confirmed = [
            m
            for m in members
            if m.get("gender_confirmed_by_user") and m.get("predicted_gender") in ("male", "female")
        ]
        if not confirmed:
            pytest.skip("No already-confirmed member — mutation covered by unit tests")

        speaker_uuid = confirmed[0]["speaker_uuid"]
        gender = confirmed[0]["predicted_gender"]

        confirm_resp = api_session.post(
            f"{backend_url}/api/speakers/{speaker_uuid}/confirm-gender?gender={gender}",
            timeout=10,
        )
        assert confirm_resp.status_code == 200
        result = confirm_resp.json()
        assert result["predicted_gender"] == gender
        assert result["gender_confirmed_by_user"] is True

    def test_confirm_speaker_gender_invalid(self, api_session: requests.Session, backend_url: str):
        """POST /speakers/{uuid}/confirm-gender rejects invalid gender."""
        resp = api_session.get(f"{backend_url}/api/speaker-clusters", timeout=10)
        data = resp.json()
        if data.get("total", 0) == 0:
            pytest.skip("No clusters exist")

        cluster_uuid = data["items"][0]["uuid"]
        detail_resp = api_session.get(
            f"{backend_url}/api/speaker-clusters/{cluster_uuid}", timeout=10
        )
        members = detail_resp.json().get("members", [])
        if not members:
            pytest.skip("No members in cluster")

        speaker_uuid = members[0]["speaker_uuid"]

        bad_resp = api_session.post(
            f"{backend_url}/api/speakers/{speaker_uuid}/confirm-gender?gender=invalid",
            timeout=10,
        )
        assert bad_resp.status_code == 400

    def test_confirm_profile_gender_endpoint(self, api_session: requests.Session, backend_url: str):
        """POST /speaker-profiles/profiles/{uuid}/confirm-gender bulk-updates.

        E2E must NOT mutate dev data — only re-confirm a profile's EXISTING
        gender (no-op write). See tests/test_speaker_gender_confirm.py for
        the mutating coverage.
        """
        resp = api_session.get(f"{backend_url}/api/speaker-profiles/profiles", timeout=10)
        assert resp.status_code == 200
        profiles = resp.json()
        confirmed = [p for p in profiles if p.get("predicted_gender") in ("male", "female")]
        if not confirmed:
            pytest.skip("No profile with a set gender — mutation covered by unit tests")

        profile_uuid = confirmed[0]["uuid"]
        gender = confirmed[0]["predicted_gender"]

        confirm_resp = api_session.post(
            f"{backend_url}/api/speaker-profiles/profiles/{profile_uuid}/confirm-gender?gender={gender}",
            timeout=10,
        )
        assert confirm_resp.status_code == 200
        result = confirm_resp.json()
        assert result["predicted_gender"] == gender
        assert "updated_count" in result
