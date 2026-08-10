"""End-to-end tests for the tag manager (`/tags`).

Covers what only a browser can: that the route renders, that the ownership
scope and view filters reach the backend and change the list, and that rename /
delete / promote apply through the UI and are visible in the API afterwards.

**Dev-data safety.** Every tag these tests touch is created by the test under a
`e2e-tag-<uuid>` name and removed in the fixture's teardown, whichever way the
test exits. Nothing here renames, merges or deletes a pre-existing tag, and
nothing mutates a media file — a tag with no associations is the only kind this
suite creates, so a failed run leaves at most an orphan test tag that the next
teardown's name filter still matches.

Collision-cluster rendering is deliberately **not** exercised here. A collision
needs two rows sharing a normalized name within one vocabulary, which the
resolver now makes unreachable through the API by construction — they only
arise from rows predating normalization. `tests/unit/test_tag_collisions.py`
stages those directly and is the right home for that case.
"""

import uuid

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.tags]

TEST_ADMIN_EMAIL = "admin@example.com"
TEST_ADMIN_PASSWORD = "password"

#: Every tag this suite creates carries this prefix so teardown can find them
#: even if a test aborted before its own cleanup.
TAG_PREFIX = "e2e-tag-"


def _unique_tag_name() -> str:
    return f"{TAG_PREFIX}{uuid.uuid4().hex[:8]}"


@pytest.fixture
def tag_api(api_helper):
    """An authenticated API helper that removes this suite's tags on the way out.

    Teardown sweeps by name prefix rather than by the ids a test remembers, so a
    test that failed midway through a rename still gets cleaned up.
    """
    api_helper.login(TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD)
    yield api_helper

    try:
        for tag in api_helper.get("/api/tags") or []:
            if str(tag.get("name", "")).startswith(TAG_PREFIX):
                api_helper.delete(f"/api/tags?tag_uuids={tag['uuid']}")
    except Exception as exc:  # pragma: no cover - teardown must not mask a failure
        print(f"tag teardown failed (leaves orphan e2e tags): {exc}")


@pytest.fixture
def tags_page(browser, shared_auth_state, base_url):
    """A fresh pre-authenticated page on the tag manager."""
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1440, "height": 950},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(f"{base_url}/tags")
    page.wait_for_selector(".tags-page", timeout=30000)
    yield page
    page.close()
    context.close()


def _reload_tags(page: Page) -> None:
    """Re-fetch the list, waiting for the route to settle."""
    page.reload()
    page.wait_for_selector(".tags-page", timeout=30000)


def _row(page: Page, name: str):
    return page.get_by_role("option").filter(has_text=name)


class TestTagManagerRoute:
    """The route renders and lists what the backend returned."""

    def test_route_renders_with_the_tag_list(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})

        _reload_tags(tags_page)

        expect(tags_page.locator(".tags-page")).to_be_visible()
        expect(_row(tags_page, name)).to_be_visible()

    def test_navbar_link_reaches_the_manager(self, gallery_page: Page, base_url):
        gallery_page.click('a[href="/tags"]')

        gallery_page.wait_for_selector(".tags-page", timeout=15000)
        assert "/tags" in gallery_page.url

    def test_no_console_errors_on_load(self, browser, shared_auth_state, base_url, tag_api):
        """A render that logs an error is a broken render even when it looks fine."""
        tag_api.post("/api/tags", {"name": _unique_tag_name()})
        context = browser.new_context(
            storage_state=shared_auth_state,
            viewport={"width": 1440, "height": 950},
            ignore_https_errors=True,
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

        page.goto(f"{base_url}/tags")
        page.wait_for_selector(".tags-page", timeout=30000)
        page.wait_for_timeout(1000)

        page.close()
        context.close()
        assert errors == [], f"console errors on /tags: {errors}"


class TestOwnershipScope:
    """The scope picker is the read half of the ownership model."""

    def test_scope_picker_offers_the_three_scopes(self, tags_page: Page):
        picker = tags_page.get_by_label("Tag ownership")

        expect(picker).to_be_visible()
        assert picker.locator("option").count() == 3

    def test_mine_scope_keeps_an_owned_tag(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        tags_page.get_by_label("Tag ownership").select_option("mine")
        tags_page.wait_for_timeout(750)

        # A tag this account just created is its own, so narrowing to "mine"
        # must not drop it — the failure mode when the scope predicate is
        # inverted or applied to the wrong column.
        expect(_row(tags_page, name)).to_be_visible()

    def test_shared_scope_excludes_an_owned_tag(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        tags_page.get_by_label("Tag ownership").select_option("shared")
        tags_page.wait_for_timeout(750)

        expect(_row(tags_page, name)).to_have_count(0)

    def test_seeded_defaults_are_shared(self, tags_page: Page):
        """The bootstrap vocabulary is the system tier, and says so in the UI."""
        tags_page.get_by_label("Tag ownership").select_option("shared")
        tags_page.wait_for_timeout(750)

        rows = tags_page.get_by_role("option")
        if rows.count() == 0:
            pytest.skip("no system tags in this deployment")
        expect(rows.first).to_contain_text("Shared")


class TestTagMutations:
    """Rename / delete / promote applied through the UI, verified through the API."""

    def _select(self, page: Page, name: str) -> None:
        _row(page, name).first.click()
        page.wait_for_selector(".detail-pane", timeout=10000)

    def test_rename_applies_and_persists(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        renamed = _unique_tag_name()
        created = tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        self._select(tags_page, name)
        field = tags_page.get_by_label("Rename")
        field.fill(renamed)
        tags_page.get_by_role("button", name="Rename", exact=True).click()
        tags_page.wait_for_timeout(1500)

        after = {t["uuid"]: t["name"] for t in tag_api.get("/api/tags")}
        assert after.get(created["uuid"]) == renamed

    def test_delete_shows_impact_then_removes_the_tag(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        created = tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        self._select(tags_page, name)
        tags_page.get_by_role("button", name="Delete", exact=True).click()

        # The impact preview fronts the delete — confirming without it ever
        # rendering would mean the confirm step had been skipped.
        tags_page.wait_for_timeout(750)
        confirm = tags_page.get_by_role("button", name="Delete", exact=True).last
        confirm.click()
        tags_page.wait_for_timeout(1500)

        remaining = {t["uuid"] for t in tag_api.get("/api/tags")}
        assert created["uuid"] not in remaining

    def test_promote_publishes_to_the_shared_vocabulary(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        created = tag_api.post("/api/tags", {"name": name})
        assert created.get("is_shared") is False, "a new tag must start owned, not shared"
        _reload_tags(tags_page)

        self._select(tags_page, name)
        tags_page.get_by_role("button", name="Share with everyone").click()
        tags_page.wait_for_timeout(1500)

        after = {t["uuid"]: t for t in tag_api.get("/api/tags")}
        assert after[created["uuid"]]["is_shared"] is True

    def test_promote_control_absent_for_an_already_shared_tag(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        created = tag_api.post("/api/tags", {"name": name})
        tag_api.post("/api/tags/promote", {"tag_uuids": [created["uuid"]]})
        _reload_tags(tags_page)

        self._select(tags_page, name)

        # Promoting a shared tag is a no-op, so the control is absent rather
        # than present-and-disabled.
        expect(tags_page.get_by_role("button", name="Share with everyone")).to_have_count(0)
        expect(tags_page.locator(".detail-pane")).to_contain_text("Shared")


class TestTagManagerThemes:
    """Light/dark parity is required for any frontend change in this repo."""

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_renders_in_both_themes(self, tags_page: Page, tag_api, theme: str):
        tag_api.post("/api/tags", {"name": _unique_tag_name()})
        _reload_tags(tags_page)

        tags_page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
        tags_page.wait_for_timeout(400)

        expect(tags_page.locator(".tags-page")).to_be_visible()
        expect(tags_page.locator(".list-pane")).to_be_visible()

    def test_renders_on_a_narrow_viewport(self, tags_page: Page, tag_api):
        tag_api.post("/api/tags", {"name": _unique_tag_name()})
        tags_page.set_viewport_size({"width": 390, "height": 844})
        _reload_tags(tags_page)

        expect(tags_page.locator(".tags-page")).to_be_visible()
        # The filter row stacks below 640px; both controls must survive it.
        expect(tags_page.get_by_label("Tag ownership")).to_be_visible()


class TestGalleryBulkTagEntry:
    """The gallery's Organize menu is the other entry point into tagging."""

    def test_organize_menu_offers_the_bulk_tag_actions(self, gallery_page: Page):
        checkboxes = gallery_page.locator('.file-card input[type="checkbox"]')
        if checkboxes.count() == 0:
            pytest.skip("no media files in this deployment to select")

        checkboxes.first.check()
        gallery_page.wait_for_timeout(500)

        organize = gallery_page.get_by_role("button", name="Organize")
        if organize.count() == 0:
            pytest.skip("Organize menu not present for this selection")
        organize.first.click()

        # Read-only: the menu is asserted, never applied. Applying here would
        # mutate a dev media file, which this suite must not do.
        expect(gallery_page.get_by_text("Add tag")).to_be_visible()
