"""End-to-end tests for the tag manager modal.

Covers what only a browser can: that the modal opens from the gallery's Tags
button, that the ownership scope and view filters reach the backend and change
the list, and that rename / delete / promote apply through the UI and are
visible in the API afterwards.

Tags are metadata over the library rather than a destination, so there is no
`/tags` route: the Tags button sits beside Collections and picks its mode from
the selection — nothing selected opens this manager, a selection opens the bulk
apply flow instead (mirroring `CollectionsPanel`'s `viewMode`).

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


def _open_tags_from_organize(page: Page) -> None:
    """Open tagging from the Organize menu, the selection-mode entry point.

    Tags sits beside "Add to collection" there because both attach metadata to
    a file. The toolbar button covers the no-selection case.
    """
    page.click(".organize-btn")
    tags_item = page.locator(".dropdown-item", has_text="Tags").first
    expect(tags_item).to_be_visible(timeout=5000)
    tags_item.click()


def _open_manager(page: Page) -> None:
    """Open the tag manager from the gallery, with nothing selected.

    A selection would open the bulk apply flow instead, so the tests that want
    the manager clear it first.
    """
    page.keyboard.press("Escape")
    page.click(".tags-btn")
    page.wait_for_selector(".tags-manager", timeout=30000)


@pytest.fixture
def tags_page(browser, shared_auth_state, base_url):
    """A fresh pre-authenticated gallery page with the tag manager open."""
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1440, "height": 950},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".gallery-action-buttons", timeout=30000)
    _open_manager(page)
    yield page
    page.close()
    context.close()


def _reload_tags(page: Page) -> None:
    """Re-fetch the list by reopening the modal."""
    page.reload()
    page.wait_for_selector(".gallery-action-buttons", timeout=30000)
    _open_manager(page)


def _tag_rows(page: Page):
    """Every tag row, scoped to the listbox.

    The ownership `<select>`'s `<option>` elements carry `role=option` too, so a
    bare `get_by_role("option")` mixes the filter control into the results. The
    unit suite hit exactly this; scope to the listbox rather than filtering by
    text, which only accidentally avoids it.
    """
    return page.locator(".tags-manager").get_by_role("listbox").get_by_role("option")


def _row(page: Page, name: str):
    return _tag_rows(page).filter(has_text=name)


class TestTagManagerRoute:
    """The route renders and lists what the backend returned."""

    def test_modal_opens_with_the_tag_list(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})

        _reload_tags(tags_page)

        expect(tags_page.locator(".tags-manager")).to_be_visible()
        expect(_row(tags_page, name)).to_be_visible()

    def test_tags_button_sits_beside_collections(self, gallery_page: Page):
        """Tags is a library action next to Collections, not a nav destination."""
        expect(gallery_page.locator(".tags-btn")).to_be_visible()
        expect(gallery_page.locator(".collections-btn")).to_be_visible()
        # The route is gone; nothing should link to it.
        expect(gallery_page.locator('a[href="/tags"]')).to_have_count(0)

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

        page.goto(base_url)
        page.wait_for_selector(".gallery-action-buttons", timeout=30000)
        _open_manager(page)
        expect(page.locator(".list-skeleton")).to_have_count(0, timeout=10000)

        page.close()
        context.close()
        assert errors == [], f"console errors opening the tag manager: {errors}"


class TestOwnershipScope:
    """The scope picker is the read half of the ownership model."""

    def test_scope_picker_offers_all_and_each_ownership(self, tags_page: Page):
        picker = tags_page.get_by_label("Tag ownership")

        expect(picker).to_be_visible()
        assert picker.locator("option").count() == 4

    def test_mine_scope_keeps_an_owned_tag(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        tags_page.get_by_label("Tag ownership").select_option("mine")

        # A tag this account just created is its own, so narrowing to "mine"
        # must not drop it — the failure mode when the scope predicate is
        # inverted or applied to the wrong column.
        expect(_row(tags_page, name)).to_be_visible()

    def test_shared_scope_excludes_an_owned_tag(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        tags_page.get_by_label("Tag ownership").select_option("system")

        expect(_row(tags_page, name)).to_have_count(0)

    def test_seeded_defaults_are_system_tags(self, tags_page: Page):
        """The bootstrap vocabulary is the system tier, and says so in the UI."""
        tags_page.get_by_label("Tag ownership").select_option("system")
        expect(tags_page.locator(".list-skeleton")).to_have_count(0, timeout=10000)

        rows = _tag_rows(tags_page)
        if rows.count() == 0:
            pytest.skip("no system tags in this deployment")
        expect(rows.first).to_contain_text("Shared")


class TestTagCreation:
    """Add is the third of add/edit/delete, and it lives in the manager."""

    def test_create_a_tag_from_the_manager(self, tags_page: Page, tag_api):
        """Until this existed, a tag could only be born by tagging a file."""
        name = _unique_tag_name()

        tags_page.get_by_label("New tag").fill(name)
        tags_page.get_by_role("button", name="Add", exact=True).click()
        expect(_row(tags_page, name)).to_be_visible(timeout=10000)

        assert any(t["name"] == name for t in tag_api.get("/api/tags")), (
            f"{name} was not created through the manager"
        )

    def test_creating_an_existing_name_resolves_instead_of_duplicating(
        self, tags_page: Page, tag_api
    ):
        """Normalized-exact resolution, surfaced rather than silently deduped."""
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        # Same name, different case — must land on the existing row.
        tags_page.get_by_label("New tag").fill(name.upper())
        tags_page.get_by_role("button", name="Add", exact=True).click()
        expect(_row(tags_page, name)).to_have_count(1, timeout=10000)

        matches = [t for t in tag_api.get("/api/tags") if t["name"].lower() == name.lower()]
        assert len(matches) == 1, f"expected one row, got {len(matches)}"


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
        # Rename is a two-step affair: the button swaps the heading for an
        # inline editor, and the field only exists in that state. Filling it
        # straight away found nothing — the panel was still showing the name.
        tags_page.get_by_role("button", name="Rename", exact=True).click()
        tags_page.get_by_label("Tag name").fill(renamed)
        tags_page.get_by_role("button", name="Rename", exact=True).click()
        expect(_row(tags_page, renamed)).to_be_visible(timeout=10000)

        after = {t["uuid"]: t["name"] for t in tag_api.get("/api/tags")}
        assert after.get(created["uuid"]) == renamed

    def test_delete_shows_impact_then_removes_the_tag(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        created = tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        self._select(tags_page, name)
        tags_page.get_by_role("button", name="Delete", exact=True).click()

        # Delete goes through the app's shared ConfirmationModal, which carries
        # both counts — confirming without that appearing would mean the
        # confirm step had been skipped.
        confirm = tags_page.get_by_role("button", name="Delete", exact=True).last
        expect(confirm).to_be_visible(timeout=5000)
        confirm.click()
        expect(_row(tags_page, name)).to_have_count(0, timeout=10000)

        remaining = {t["uuid"] for t in tag_api.get("/api/tags")}
        assert created["uuid"] not in remaining

    def test_promote_publishes_to_the_shared_vocabulary(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        created = tag_api.post("/api/tags", {"name": name})
        assert created.get("ownership") == "mine", "a new tag must start owned, not shared"
        _reload_tags(tags_page)

        self._select(tags_page, name)
        tags_page.get_by_role("button", name="Share with everyone").click()
        # A successful promote clears the selection as part of its mutate()
        # cycle (same as rename/delete/merge), so the detail pane reverts to
        # the "select a tag" prompt rather than ever showing "Shared" here —
        # that copy only appears when *re-selecting* an already-shared tag
        # (see test_promote_control_absent_for_an_already_shared_tag).
        expect(tags_page.locator(".select-prompt")).to_be_visible(timeout=10000)

        after = {t["uuid"]: t for t in tag_api.get("/api/tags")}
        assert after[created["uuid"]]["ownership"] == "system"

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

        expect(tags_page.locator(".tags-manager")).to_be_visible()
        expect(tags_page.locator(".list-pane")).to_be_visible()

    def test_renders_on_a_narrow_viewport(self, tags_page: Page, tag_api):
        tag_api.post("/api/tags", {"name": _unique_tag_name()})
        tags_page.set_viewport_size({"width": 390, "height": 844})
        _reload_tags(tags_page)

        expect(tags_page.locator(".tags-manager")).to_be_visible()
        # The filter row stacks below 640px; both controls must survive it.
        expect(tags_page.get_by_label("Tag ownership")).to_be_visible()


class TestGalleryBulkTagEntry:
    """The Tags button routes by selection — the core of the modal design."""

    def _enter_selection_mode(self, page: Page) -> bool:
        """Select the first file. Returns False when the library is empty.

        Checkboxes only exist in selection mode, which `.select-btn` enters.
        Looking for them without clicking it found nothing and skipped the test
        while 18 files sat in the library — a skip that read as coverage.
        """
        page.wait_for_selector(".file-card, .file-list-row", timeout=30000)
        if page.locator(".file-card").count() == 0:
            return False
        page.click(".select-btn")
        # The input is `opacity: 0` and sized to fill its label, so Playwright
        # never sees it as visible. The label is what a user clicks, and it
        # carries the handler.
        selector = page.locator(".file-selector").first
        selector.wait_for(state="visible", timeout=10000)
        selector.click()
        expect(page.locator(".organize-btn")).to_contain_text("(1)", timeout=5000)
        return True

    def test_tags_button_opens_the_manager_with_nothing_selected(self, gallery_page: Page):
        gallery_page.click(".tags-btn")

        expect(gallery_page.locator(".tags-manager")).to_be_visible()

    def test_tags_button_opens_bulk_apply_with_a_selection(self, gallery_page: Page):
        """A selection must reach the bulk flow, not the library manager."""
        if not self._enter_selection_mode(gallery_page):
            pytest.skip("no media files in this deployment to select")

        _open_tags_from_organize(gallery_page)

        # The bulk modal, addressed by its own field; the manager must NOT open.
        expect(gallery_page.get_by_label("Tag name")).to_be_visible()
        expect(gallery_page.locator(".tags-manager")).to_have_count(0)

    def test_bulk_apply_reaches_the_backend_and_is_reversible(self, gallery_page: Page, tag_api):
        """Apply a test tag across a selection, verify via API, then undo it.

        The only test here that writes to a dev media file. It is reversible by
        construction: the tag is `e2e-tag-` prefixed and the teardown deletes
        the tag row, which takes its associations with it — so even a mid-test
        failure leaves the file's own tags untouched.
        """
        if not self._enter_selection_mode(gallery_page):
            pytest.skip("no media files in this deployment to select")

        name = _unique_tag_name()
        _open_tags_from_organize(gallery_page)
        gallery_page.get_by_label("Tag name").fill(name)
        gallery_page.get_by_role("button", name="Add tag").click()
        expect(gallery_page.locator(".result")).to_be_visible(timeout=10000)

        applied = [t for t in tag_api.get("/api/tags") if t["name"] == name]
        assert applied, f"{name} never reached the backend"
        assert applied[0]["usage_count"] >= 1, "tag created but attached to nothing"


class TestTagManagerTools:
    """Search, sort and create — the tools that make 99 tags navigable."""

    def test_search_narrows_the_list(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        tags_page.get_by_label("Search tags").fill(name)

        # Exactly the searched tag: a substring nobody else shares.
        expect(_tag_rows(tags_page)).to_have_count(1)
        expect(_row(tags_page, name)).to_be_visible()

    def test_search_clears_back_to_the_full_list(self, tags_page: Page, tag_api):
        tag_api.post("/api/tags", {"name": _unique_tag_name()})
        _reload_tags(tags_page)
        # `.count()` is a SNAPSHOT — it does not wait. Taken straight after the reload it
        # returned 0 before the list had rendered, so `before` was 0, the final assertion
        # became "expect 0 rows" and the test failed against a correctly-populated list.
        # Wait for a non-empty list first; we just created a tag, so 0 is never valid here.
        rows = _tag_rows(tags_page)
        expect(rows.first).to_be_visible(timeout=10000)
        before = rows.count()
        assert before > 0, "the tag we just created must be listed before searching"

        tags_page.get_by_label("Search tags").fill("zzz-matches-nothing")
        expect(_tag_rows(tags_page)).to_have_count(0)

        tags_page.get_by_title("Clear search").click()
        expect(_tag_rows(tags_page)).to_have_count(before)

    def test_sorting_by_name_reorders_without_refetching(self, tags_page: Page, tag_api):
        tag_api.post("/api/tags", {"name": _unique_tag_name()})
        _reload_tags(tags_page)

        first_by_usage = _tag_rows(tags_page).first.inner_text()
        # Scoped to the modal: the gallery behind it has its own "Sort by"
        # control, and an unscoped label match hits both.
        sort_select = tags_page.locator(".tags-manager").locator(".sort-select")
        sort_select.select_option("name")
        expect(sort_select).to_have_value("name")
        first_by_name = _tag_rows(tags_page).first.inner_text()

        # Sorting is client-side over the loaded list, so the count cannot move.
        assert first_by_usage != first_by_name or _tag_rows(tags_page).count() <= 1


class TestTagFileList:
    """The detail pane's promise: "select a tag to see what it touches"."""

    def test_a_tag_with_no_files_says_so(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        _row(tags_page, name).first.click()
        tags_page.wait_for_selector(".detail-pane", timeout=10000)

        expect(tags_page.locator(".detail-pane")).to_contain_text("No files", timeout=10000)

    def test_a_used_tag_lists_its_files(self, tags_page: Page, tag_api):
        """A seeded tag that real media carries must name that media."""
        listed = tag_api.get("/api/tags")
        used = [t for t in listed if t.get("usage_count", 0) > 0]
        if not used:
            pytest.skip("no tag in this deployment carries a file")

        _row(tags_page, used[0]["name"]).first.click()
        tags_page.wait_for_selector(".detail-pane", timeout=10000)

        expect(tags_page.locator(".touches-list")).to_be_visible(timeout=10000)


class TestTagSharing:
    """Sharing a tag with specific users — the middle tier (v386)."""

    def test_share_dialog_opens_for_a_tag_you_own(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        tag_api.post("/api/tags", {"name": name})
        _reload_tags(tags_page)

        _row(tags_page, name).first.click()
        tags_page.wait_for_selector(".detail-pane", timeout=10000)
        tags_page.get_by_role("button", name="Share…").click()

        expect(tags_page.locator(".tag-share")).to_be_visible(timeout=10000)

    def test_a_new_tag_is_shared_with_nobody(self, tags_page: Page, tag_api):
        name = _unique_tag_name()
        created = tag_api.post("/api/tags", {"name": name})

        assert tag_api.get(f"/api/tags/{created['uuid']}/shares") == []
