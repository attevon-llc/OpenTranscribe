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
import uuid

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
    from conftest import TEST_ADMIN_EMAIL  # type: ignore[import-not-found]
    from conftest import TEST_ADMIN_PASSWORD  # type: ignore[import-not-found]
except ImportError:
    TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
    TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "admin")  # noqa: S105


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


@pytest.fixture(scope="session")
def api_token(api_session: requests.Session) -> str:
    """The bearer token behind ``api_session``, for helpers that take a raw token."""
    return str(api_session.headers["Authorization"]).removeprefix("Bearer ")


@pytest.fixture
def owned_profile(api_session: requests.Session, backend_url: str):
    """A speaker profile this test OWNS, deleted however the test ends.

    The gender-confirm tests used to re-confirm an ambient profile's existing gender,
    reasoning that re-sending the same value is a no-op. It is not:
    ``confirm_profile_gender`` also runs an **unconditional bulk UPDATE** of every
    linked ``Speaker`` row's ``predicted_gender`` and ``gender_confirmed_by_user``,
    so any member that disagreed with the profile was silently coerced — across every
    file that speaker appears in. Owning the profile makes the write unambiguous.

    Yields:
        The created profile's UUID.
    """
    # `name`/`description` are bare function params on the handler, so FastAPI treats
    # them as QUERY parameters — a JSON body is silently ignored here.
    resp = api_session.post(
        f"{backend_url}/api/speaker-profiles/profiles",
        params={"name": f"e2e-gender-{uuid.uuid4().hex[:8]}"},
        timeout=15,
    )
    assert resp.status_code in (200, 201), f"Create profile failed: {resp.status_code} {resp.text}"
    profile = resp.json()
    profile_uuid = str(profile["uuid"])
    try:
        yield profile_uuid
    finally:
        # In a `finally`: a cleanup on the happy path is the one that does not run
        # when the assertion above it fails.
        try:
            api_session.delete(
                f"{backend_url}/api/speaker-profiles/profiles/{profile_uuid}", timeout=15
            )
        except requests.RequestException:
            pass


@pytest.fixture
def owned_speaker(
    owned_media_factory, api_token: str, api_session: requests.Session, backend_url: str
):
    """A diarized ``Speaker`` row belonging to a file this test uploaded.

    A speaker only exists after real diarization, so this uploads the committed 10 s
    clip and reads its speakers back. Deleting the file cascades its speakers
    (``MediaFile.speakers`` is ``cascade="all, delete-orphan"``), so the file teardown
    in ``owned_media_factory`` is the whole cleanup.

    Read from ``GET /api/speakers?file_uuid=`` rather than the file detail's own
    ``speakers`` array: that array is a display projection carrying only
    ``profile_name`` / ``profile_status``, with no identifier to address a speaker by.

    Returns:
        The first speaker payload of the uploaded file.
    """
    media = owned_media_factory(api_token)
    resp = api_session.get(
        f"{backend_url}/api/speakers", params={"file_uuid": media["uuid"]}, timeout=30
    )
    assert resp.status_code == 200, f"Could not list speakers: {resp.text[:300]}"
    payload = resp.json()
    speakers = payload if isinstance(payload, list) else payload.get("items", [])
    assert speakers, f"diarization produced no speakers for {media['uuid']}"
    return dict(speakers[0])


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
        """Verify gender chips appear on cluster cards when gender data exists.

        Was conditional-only: every assertion sat inside ``if gender_chips.count() > 0``,
        so the test passed silently on a stack with no confirmed gender data. The dev
        library always carries confirmed-gender clusters, so the precondition is asserted
        directly rather than skipped past \u2014 a stack that regresses to zero chips fails
        loudly here instead of reporting green having checked nothing.
        """
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        cluster_cards = authenticated_page.locator(".cluster-card")
        if cluster_cards.count() == 0:
            pytest.skip("No clusters found - need transcribed media with speakers")

        gender_chips = authenticated_page.locator(".gender-chip")
        assert gender_chips.count() > 0, (
            "No gender chips found to check \u2014 need at least one cluster with a "
            "confirmed gender prediction (total_with_gender > 0)"
        )
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

    def test_no_chips_when_no_gender_predictions(
        self,
        authenticated_page: Page,
        base_url: str,
        api_session: requests.Session,
        backend_url: str,
    ):
        """Verify gender chips render on exactly the clusters with gender data — no more.

        SpeakerClusterCard.svelte renders ``.gender-chip`` iff
        ``cluster.gender_composition.total_with_gender > 0``. Cross-checks the DOM
        against the same ``/api/speaker-clusters?page=1&per_page=20`` listing the page
        itself fetches on initial load (routes/speakers/+page.svelte): the number of
        rendered ``.gender-chip`` elements must equal the number of clusters the API
        reports with a nonzero gender composition — a chip on a cluster the backend says
        has no gender data would be exactly the bug this test is named for.
        """
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        cluster_cards = authenticated_page.locator(".cluster-card")
        if cluster_cards.count() == 0:
            pytest.skip("No clusters found")

        resp = api_session.get(
            f"{backend_url}/api/speaker-clusters", params={"page": 1, "per_page": 20}, timeout=10
        )
        assert resp.status_code == 200, f"Could not list clusters: {resp.text[:300]}"
        items = resp.json().get("items", [])
        expected_chip_count = sum(
            1
            for item in items
            if (item.get("gender_composition") or {}).get("total_with_gender", 0) > 0
        )
        actual_chip_count = authenticated_page.locator(".gender-chip").count()
        assert actual_chip_count == expected_chip_count, (
            f"Expected {expected_chip_count} gender chip(s) (clusters with "
            f"total_with_gender > 0 per the API) but the page rendered {actual_chip_count}"
        )


class TestGenderIconsOnMemberRows:
    """Verify gender icons appear on expanded cluster member rows."""

    def test_expand_cluster_shows_gender_icons(self, authenticated_page: Page, base_url: str):
        """Expand a cluster and verify gender icons on member rows.

        Was conditional-only: every assertion sat inside ``if gender_icons.count() > 0``.
        The dev library's clusters always carry at least one member with a confirmed
        gender (``ClusterMemberList.svelte`` renders ``.gender-icon`` per
        ``member.predicted_gender``), so the precondition is asserted directly.
        """
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        cluster_cards = authenticated_page.locator(".cluster-card")
        if cluster_cards.count() == 0:
            pytest.skip("No clusters found")

        cluster_cards.first.locator(".card-header").click()
        # expect() polls; count() does not (same trap as test_profiles_tab_has_gender_buttons
        # above) \u2014 wait for the member list to actually render before counting icons, or the
        # count is taken against the pre-expand DOM and reads 0 regardless of data.
        expect(authenticated_page.locator(".member-row").first).to_be_visible(timeout=10000)
        gender_icons = authenticated_page.locator(".gender-icon")
        assert gender_icons.count() > 0, (
            "No gender icons found to check \u2014 need at least one expanded cluster member "
            "with a predicted_gender"
        )
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
        """Verify outlier members get highlighted styling when a cluster carries one."""
        authenticated_page.goto(f"{base_url}/speakers")
        authenticated_page.wait_for_load_state("networkidle")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        cluster_cards = authenticated_page.locator(".cluster-card")
        if cluster_cards.count() == 0:
            pytest.skip("No clusters found")

        cluster_cards.first.locator(".card-header").click()
        # Expanding a cluster must render its members — the one thing true regardless
        # of whether THIS particular cluster happens to carry a gender conflict.
        member_rows = authenticated_page.locator(".member-row")
        expect(member_rows.first).to_be_visible(timeout=10000)

        # Outliers are data-dependent (ClusterMemberList.svelte only runs outlier
        # analysis for a cluster with `hasGenderConflict`), so their absence is a
        # legitimate outcome. When they ARE present, they must actually render
        # highlighted and visible, not just resolve as a locator.
        outlier_rows = authenticated_page.locator(".member-row.gender-outlier")
        if outlier_rows.count() > 0:
            expect(outlier_rows.first).to_be_visible()


class TestProfileGenderConfirmation:
    """Verify gender confirm buttons on profile cards."""

    def test_profiles_tab_has_gender_buttons(
        self, owned_profile: str, authenticated_page: Page, base_url: str
    ):
        """Navigate to profiles tab and verify gender toggle buttons exist.

        Takes ``owned_profile`` so there is guaranteed to be a card to look at. It
        previously skipped on "No profiles found" — and did, on this stack, despite a
        real profile existing: ``.profile-card`` was queried with ``count()``, which
        does **not** wait, so it read the DOM before the profiles tab had rendered.
        A test that skips whenever it is early asserts nothing and reports success.
        """
        authenticated_page.goto(f"{base_url}/speakers")
        # `loadProfiles()` prefers a single-use prefetch cache that may predate the
        # profile created above; one reload guarantees a live `listProfiles()`.
        authenticated_page.reload()
        authenticated_page.wait_for_load_state("networkidle")

        profiles_tab = authenticated_page.locator("button:has-text('Profiles')")
        expect(profiles_tab).to_be_visible(timeout=10000)
        profiles_tab.click()

        # expect() polls; count() does not. That difference is the whole bug above.
        cards = authenticated_page.locator(".profile-card")
        expect(cards.first).to_be_visible(timeout=10000)

        gender_btns = authenticated_page.locator(".gender-toggle-btn")
        expect(gender_btns.first).to_be_visible(timeout=10000)
        assert gender_btns.count() >= 2, "Expected male and female toggles on a profile card"

    def test_click_gender_confirm_updates_state(
        self,
        owned_profile: str,
        owned_speaker: dict,
        api_session: requests.Session,
        authenticated_page: Page,
        base_url: str,
        backend_url: str,
    ):
        """Click the gender toggle on a profile this test created and see it stick.

        The Profiles tab's ``.gender-toggle-btn`` is **profile**-level, so it drives the
        same bulk-update endpoint as the API test above — it was never the no-op the old
        comment claimed. It also used to click whichever already-active button happened
        to be on screen, and skip when none was, so on a stack with no confirmed profile
        it asserted nothing at all.

        ⚠️ The profile must have a MEMBER for the toggle to render active.
        ``GET /speaker-profiles/profiles`` does not report
        ``SpeakerProfile.predicted_gender`` at all — it reports the most common
        ``predicted_gender`` among the profile's linked speakers. So confirming a gender
        on a member-less profile persists to the column and is invisible everywhere the
        UI looks. Filed separately; this test works with the read model as it is.
        """
        # Give the profile a member, so the derived gender has something to derive from.
        assign = api_session.post(
            f"{backend_url}/api/speaker-profiles/speakers/{owned_speaker['uuid']}/assign-profile",
            params={"profile_uuid": owned_profile},
            timeout=30,
        )
        assert assign.status_code == 200, f"could not assign speaker: {assign.text[:300]}"

        seed = api_session.post(
            f"{backend_url}/api/speaker-profiles/profiles/{owned_profile}/confirm-gender",
            params={"gender": "female"},
            timeout=10,
        )
        assert seed.status_code == 200, f"could not seed profile gender: {seed.text[:300]}"
        assert seed.json()["updated_count"] == 1, "the member should have been bulk-updated"

        listing = api_session.get(f"{backend_url}/api/speaker-profiles/profiles", timeout=10).json()
        profile = next(p for p in listing if p["uuid"] == owned_profile)
        assert profile["predicted_gender"] == "female", (
            "the API must report the gender before the UI can render it as active"
        )
        name = profile["name"]

        authenticated_page.goto(f"{base_url}/speakers")
        # `loadProfiles()` serves `apiCache.get('speakers:profiles')` in preference to a
        # live fetch, and that prefetch may predate the profile just created above — in
        # which case the card never renders and the assertion below fails against
        # correct code. The cache is single-use (`invalidate` immediately after read),
        # so one reload guarantees the second load calls `listProfiles()` for real.
        authenticated_page.reload()
        authenticated_page.wait_for_load_state("networkidle")
        profiles_tab = authenticated_page.locator("button:has-text('Profiles')")
        expect(profiles_tab).to_be_visible(timeout=10000)
        profiles_tab.click()

        # Scope to OUR card by name, so the click cannot land on someone else's profile.
        card = authenticated_page.locator(".profile-card", has_text=name)
        expect(card).to_be_visible(timeout=10000)

        active = card.locator(".gender-toggle-btn.active")
        expect(active.first).to_be_visible(timeout=10000)
        active.first.click()

        expect(card.locator(".gender-toggle-btn.active").first).to_be_visible()


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

    def test_confirm_speaker_gender_endpoint(
        self, owned_speaker: dict, api_session: requests.Session, backend_url: str
    ):
        """POST /speakers/{uuid}/confirm-gender sets gender on a speaker we own.

        This used to re-confirm an ambient cluster member's existing gender and call
        that a no-op. Even where the write really is value-preserving, it is a write to
        somebody's real row — and it skipped entirely when no confirmed member existed,
        so whether it tested anything depended on the state of the dev library.
        Owning the speaker means the assertion is unconditional and the value can
        actually be *changed*, which is what the endpoint is for.
        """
        speaker_uuid = owned_speaker["uuid"]

        confirm_resp = api_session.post(
            f"{backend_url}/api/speakers/{speaker_uuid}/confirm-gender?gender=female",
            timeout=10,
        )
        assert confirm_resp.status_code == 200, f"confirm-gender failed: {confirm_resp.text[:300]}"
        result = confirm_resp.json()
        assert result["predicted_gender"] == "female"
        assert result["gender_confirmed_by_user"] is True

        # The opposite value, so "always returns what you sent" cannot pass.
        confirm_resp = api_session.post(
            f"{backend_url}/api/speakers/{speaker_uuid}/confirm-gender?gender=male",
            timeout=10,
        )
        assert confirm_resp.status_code == 200
        assert confirm_resp.json()["predicted_gender"] == "male"

    def test_confirm_speaker_gender_invalid(
        self, owned_speaker: dict, api_session: requests.Session, backend_url: str
    ):
        """POST /speakers/{uuid}/confirm-gender rejects an invalid gender.

        Validation is refused before any write, so this could not corrupt an ambient
        speaker — but it previously *skipped* whenever the dev library held no
        clusters, meaning it provided no coverage on a fresh stack. Using the owned
        speaker makes it unconditional.
        """
        bad_resp = api_session.post(
            f"{backend_url}/api/speakers/{owned_speaker['uuid']}/confirm-gender?gender=invalid",
            timeout=10,
        )
        assert bad_resp.status_code == 400

    def test_confirm_profile_gender_endpoint(
        self, owned_profile: str, api_session: requests.Session, backend_url: str
    ):
        """POST /speaker-profiles/profiles/{uuid}/confirm-gender bulk-updates.

        Acts on a profile this test created. The previous version re-confirmed an
        ambient profile's existing gender and described that as a no-op write — but the
        handler *also* bulk-updates every linked ``Speaker``'s ``predicted_gender`` and
        ``gender_confirmed_by_user`` unconditionally, so it was never a no-op for the
        members. ``updated_count`` is asserted here rather than merely present, which is
        what makes the fan-out visible at all.
        """
        confirm_resp = api_session.post(
            f"{backend_url}/api/speaker-profiles/profiles/{owned_profile}/confirm-gender",
            params={"gender": "female"},
            timeout=10,
        )
        assert confirm_resp.status_code == 200, f"confirm failed: {confirm_resp.text[:300]}"
        result = confirm_resp.json()
        assert result["predicted_gender"] == "female"
        # A freshly created profile has no members, so the bulk UPDATE must touch
        # nothing. On an ambient profile this number was never checked at all.
        assert result["updated_count"] == 0


class TestSpeakerRenamePropagationAcrossFiles:
    """A rename that updates a shared profile must repaint EVERY linked file (issue #432).

    ``_handle_update_profile_action`` (``app/api/endpoints/speakers.py``) rewrites
    ``display_name`` for every ``Speaker`` row linked to a profile, synchronously, in
    the same request that renames one of them — see
    ``tests/api/test_rename_propagation_dispatch.py`` for the dispatch-side coverage
    and ``tests/api/test_speaker_rename_service_sites.py`` for the six service-layer
    writers. Nothing in this repo previously drove that path through the real UI
    across two distinct files: this uploads two owned clips, forces their diarized
    speakers onto one owned profile (real voice-similarity matching is not needed —
    ``assign-profile`` is the same explicit linking action the Suggestions UI drives),
    renames one speaker from File A's transcript editor with the "Update Profile
    Globally" decision, and asserts the new name repaints on File B's transcript
    **after a fresh page load** — proving the propagation reached Postgres, not just
    File A's in-memory optimistic state.
    """

    def test_rename_via_transcript_editor_propagates_to_other_file(
        self,
        authenticated_page: Page,
        base_url: str,
        backend_url: str,
        api_session: requests.Session,
        api_token: str,
        owned_media_factory,
        owned_profile: str,
    ) -> None:
        # Two independently uploaded clips -> two independently diarized speakers.
        media_a = owned_media_factory(api_token)
        media_b = owned_media_factory(api_token)

        def _first_speaker(file_uuid: str) -> dict:
            resp = api_session.get(
                f"{backend_url}/api/speakers", params={"file_uuid": file_uuid}, timeout=30
            )
            assert resp.status_code == 200, f"list speakers failed: {resp.text[:300]}"
            payload = resp.json()
            speakers = payload if isinstance(payload, list) else payload.get("items", [])
            assert speakers, f"diarization produced no speakers for {file_uuid}"
            return dict(speakers[0])

        speaker_a = _first_speaker(media_a["uuid"])
        speaker_b = _first_speaker(media_b["uuid"])

        # Explicitly link both speakers to the one owned profile — the same action
        # the Suggestions UI performs, without depending on real cross-file voice
        # matching to land two clips of the same fixture on the same profile.
        for speaker in (speaker_a, speaker_b):
            assign_resp = api_session.post(
                f"{backend_url}/api/speaker-profiles/speakers/{speaker['uuid']}/assign-profile",
                params={"profile_uuid": owned_profile},
                timeout=15,
            )
            assert assign_resp.status_code == 200, (
                f"assign-profile failed for {speaker['uuid']}: {assign_resp.text[:300]}"
            )

        new_name = f"E2E Propagated {uuid.uuid4().hex[:8]}"

        # Rename speaker_a via File A's real transcript editor, choosing the
        # "Update Profile Globally" decision so the propagation actually fires.
        authenticated_page.goto(f"{base_url}/files/{media_a['uuid']}")
        authenticated_page.wait_for_load_state("networkidle")
        authenticated_page.wait_for_selector(".transcript-segment", timeout=25000)

        edit_btn = authenticated_page.locator(".edit-speakers-button")
        if edit_btn.count() == 0:
            pytest.skip("File has no diarization (no Edit Speakers affordance)")
        edit_btn.click()
        expect(authenticated_page.locator(".speaker-editor-container")).to_be_visible(timeout=10000)

        name_input = authenticated_page.locator(f'input[data-speaker-id="{speaker_a["uuid"]}"]')
        expect(name_input).to_be_visible(timeout=10000)
        name_input.fill(new_name)

        save_btn = authenticated_page.locator(".save-speakers-button")
        expect(save_btn).to_be_enabled(timeout=5000)
        save_btn.click()

        # speaker_a is linked to owned_profile, so the confirmation dialog must
        # appear; picking anything else (or letting it time out) would silently
        # test the wrong path.
        update_globally_btn = authenticated_page.locator(
            '.modal-overlay button:has-text("Update Profile Globally")'
        )
        expect(update_globally_btn).to_be_visible(timeout=10000)
        update_globally_btn.click()

        # Confirms the write reached Postgres for File A itself before checking
        # propagation to File B.
        expect(
            authenticated_page.locator(".transcript-display").get_by_text(new_name).first
        ).to_be_visible(timeout=10000)

        # The proof: a FRESH load of the OTHER file must show the new name too.
        # No stub, no optimistic client state carries across this navigation — the
        # only way `new_name` can appear here is a real Postgres write to
        # speaker_b's `display_name` in the same request that renamed speaker_a.
        authenticated_page.goto(f"{base_url}/files/{media_b['uuid']}")
        authenticated_page.wait_for_load_state("networkidle")
        authenticated_page.wait_for_selector(".transcript-segment", timeout=25000)

        expect(
            authenticated_page.locator(".segment-speaker").filter(has_text=new_name).first
        ).to_be_visible(timeout=15000)
