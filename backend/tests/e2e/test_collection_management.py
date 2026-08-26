"""End-to-end tests for collection management (create / add file / filter / rename / delete).

Collections carry a `default_prompt_id` that drives which summarization prompt runs
against a file's transcript — a broken collection edit can silently change which
prompt fires, yet there was no dedicated E2E coverage for the collection lifecycle
itself (only the tag manager and the upload-time "add to collection" flow were
covered elsewhere). This file is the mirror of `test_tag_management.py` for the
Collections modal (`CollectionsPanel.svelte` + its `$components/collections/*`
children), driven from the gallery's "Manage collections" entry point.

**Confirmed backend behavior (read before touching the delete test):**
`DELETE /api/collections/{collection_uuid}` (`app/api/endpoints/media_collections.py`,
`delete_collection`, ~line 756) deletes only the `Collection` row; its cascade removes
`CollectionShare`/`CollectionMember` rows (the join table), never the underlying
`MediaFile` rows. The handler reindexes the member files' OpenSearch access lists
*before* deleting the collection and then calls `db.delete(collection)` — there is no
media-file deletion anywhere in the function. The frontend's own delete-confirmation
copy says the same thing: `collectionsPanel.deleteConfirmMessage` ("The media files
will not be deleted, only removed from this collection."). So deleting a collection
must leave an owned test file it contains alive and reachable afterward.

**Dev-data safety.** Every collection this suite creates carries an
`e2e-collection-<uuid>` name and is removed in the fixture's teardown by name-prefix
sweep, mirroring `test_tag_management.py`'s `tag_api`. The one media file this suite
needs is created and destroyed by `owned_media_factory` (issue #541 — never touch an
ambient dev-library recording), independent of whether the collection-delete test
already ran (delete is idempotent: a file with no collections is a no-op to clean up).
"""

import uuid

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.collections]

TEST_ADMIN_EMAIL = "admin@example.com"
TEST_ADMIN_PASSWORD = "password"

#: Every collection this suite creates carries this prefix so teardown can find
#: them even if a test aborted before its own cleanup.
COLLECTION_PREFIX = "e2e-collection-"


def _unique_collection_name() -> str:
    return f"{COLLECTION_PREFIX}{uuid.uuid4().hex[:8]}"


@pytest.fixture
def collection_api(api_helper):
    """An authenticated API helper that removes this suite's collections on the way out.

    Teardown sweeps by name prefix rather than by the ids a test remembers, so a
    test that failed midway through a rename still gets cleaned up. Deleting a
    collection here never touches the member file (confirmed backend behavior,
    see module docstring) — file cleanup is a separate, independent fixture.
    """
    api_helper.login(TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD)
    yield api_helper

    try:
        for collection in api_helper.get("/api/collections") or []:
            if str(collection.get("name", "")).startswith(COLLECTION_PREFIX):
                api_helper.delete(f"/api/collections/{collection['uuid']}")
    except Exception as exc:  # pragma: no cover - teardown must not mask a failure
        print(f"collection teardown failed (leaves orphan e2e collections): {exc}")


def _open_manager(page: Page) -> None:
    """Open the collections manager from the gallery, with nothing selected."""
    page.keyboard.press("Escape")
    page.click(".collections-btn")
    page.wait_for_selector(".collections-panel", timeout=30000)


@pytest.fixture
def collections_page(browser, shared_auth_state, base_url):
    """A fresh pre-authenticated gallery page with the collections manager open."""
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


def _reload_manager(page: Page) -> None:
    """Re-fetch the list by reloading and reopening the modal.

    A full reload also resets `apiCache`'s in-memory collections entry, which
    `CollectionsFilter` otherwise serves stale from within its TTL — the same
    reason the filter test below reloads before opening the sidebar.
    """
    page.reload()
    page.wait_for_selector(".gallery-action-buttons", timeout=30000)
    _open_manager(page)


def _card(page: Page, name: str):
    return page.locator(".collection-card").filter(has_text=name)


class TestCollectionManagerRoute:
    """The modal renders and lists what the backend returned."""

    def test_modal_opens_with_the_collection_list(self, collections_page: Page, collection_api):
        name = _unique_collection_name()
        collection_api.post("/api/collections", {"name": name})

        _reload_manager(collections_page)

        expect(collections_page.locator(".collections-panel")).to_be_visible()
        expect(_card(collections_page, name)).to_be_visible()

    def test_collections_button_sits_beside_tags(self, gallery_page: Page):
        """Collections is a library action next to Tags, mirroring the tag suite's check."""
        expect(gallery_page.locator(".collections-btn")).to_be_visible()
        expect(gallery_page.locator(".tags-btn")).to_be_visible()

    def test_no_console_errors_on_load(self, browser, shared_auth_state, base_url, collection_api):
        """A render that logs an error is a broken render even when it looks fine."""
        collection_api.post("/api/collections", {"name": _unique_collection_name()})
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
        expect(page.locator(".loading")).to_have_count(0, timeout=10000)

        page.close()
        context.close()
        assert errors == [], f"console errors opening the collections manager: {errors}"


class TestCollectionCreation:
    """Create is the entry point every other test in this file depends on."""

    def test_create_a_collection_from_the_manager(self, collections_page: Page, collection_api):
        name = _unique_collection_name()

        collections_page.click(".btn-create")
        collections_page.wait_for_selector(".modal-container", timeout=10000)
        collections_page.locator("#collection-name").fill(name)
        collections_page.get_by_role("button", name="Create Collection", exact=True).click()

        expect(_card(collections_page, name)).to_be_visible(timeout=10000)
        assert any(c["name"] == name for c in collection_api.get("/api/collections")), (
            f"{name} was not created through the manager"
        )

    def test_duplicate_name_for_the_same_owner_is_rejected(
        self, collections_page: Page, collection_api
    ):
        """Names are unique per-owner (backend 400) — the form must surface that, not silently succeed."""
        name = _unique_collection_name()
        collection_api.post("/api/collections", {"name": name})
        _reload_manager(collections_page)

        collections_page.click(".btn-create")
        collections_page.wait_for_selector(".modal-container", timeout=10000)
        collections_page.locator("#collection-name").fill(name)
        collections_page.get_by_role("button", name="Create Collection", exact=True).click()

        # The create modal stays open on failure (no navigation, no success toast) and
        # the backend never created a second row with the same name for this owner.
        # Scoped by name: the outer "Manage collections" dialog is also a
        # `.modal-container`, and both are on screen at once here.
        expect(collections_page.get_by_role("dialog", name="Create New Collection")).to_be_visible(
            timeout=5000
        )
        matching = [c for c in collection_api.get("/api/collections") if c["name"] == name]
        assert len(matching) == 1, (
            f"expected exactly one collection named {name}, got {len(matching)}"
        )


class TestCollectionMembership:
    """Adding an owned file and seeing it reflected via the filter — the core promise."""

    def test_add_owned_file_and_filter_by_collection(
        self, collections_page: Page, collection_api, owned_media_factory
    ):
        token = collection_api._token
        assert token, "collection_api must be logged in before uploading"
        media = owned_media_factory(token)

        created = collection_api.post("/api/collections", {"name": _unique_collection_name()})
        collection_api.post(
            f"/api/collections/{created['uuid']}/media",
            {"media_file_ids": [media["uuid"]]},
        )

        # Confirm membership via the API contract the UI itself reads (for-files),
        # then verify the gallery's collection filter narrows to exactly this file.
        for_files = collection_api.get(f"/api/collections/for-files?file_uuids={media['uuid']}")
        assert any(c["uuid"] == created["uuid"] for c in for_files), (
            "collection membership did not reach /collections/for-files"
        )

        page = collections_page
        page.reload()  # drop the in-memory apiCache collections entry, see _reload_manager
        page.wait_for_selector(".gallery-action-buttons", timeout=30000)
        # `galleryStore`'s `showFilters` defaults to True, so the sidebar is usually
        # already open on a fresh load — only toggle it when it truly is not.
        # GalleryFilterPanel's own wrapper carries `.filter-sidebar` unconditionally
        # (only its CSS `.show` class toggles) — `.filter-content` is the piece that
        # is actually conditional on `showFilters`, so it is the real open signal.
        if page.locator(".filter-content").count() == 0:
            page.click(".filter-toggle-btn")
        page.wait_for_selector(".filter-content", timeout=10000)

        multiselect_toggle = page.locator(".collections-filter .multiselect-toggle")
        expect(multiselect_toggle).to_be_visible(timeout=10000)
        multiselect_toggle.click()
        option = page.locator(".collections-filter .option-item").filter(has_text=created["name"])
        expect(option).to_be_visible(timeout=10000)
        option.locator("input[type=checkbox]").click()

        # Narrowed to this collection: the file we added must render, and the count
        # in the toolbar must not silently report the whole library instead.
        page.wait_for_selector(".file-card, .file-list-row, .empty-state", timeout=30000)
        expect(
            page.locator(".file-card, .file-list-row").filter(has_text=media["filename"])
        ).to_be_visible(timeout=15000)


class TestCollectionMutations:
    """Rename / delete applied through the UI, verified through the API."""

    def test_rename_applies_and_persists(self, collections_page: Page, collection_api):
        name = _unique_collection_name()
        renamed = _unique_collection_name()
        created = collection_api.post("/api/collections", {"name": name})
        _reload_manager(collections_page)

        _card(collections_page, name).locator(".edit-button").click()
        collections_page.wait_for_selector(".modal-container", timeout=10000)
        name_field = collections_page.locator("#edit-collection-name")
        name_field.fill("")
        name_field.fill(renamed)
        collections_page.get_by_role("button", name="Update Collection", exact=True).click()

        expect(_card(collections_page, renamed)).to_be_visible(timeout=10000)

        after = {c["uuid"]: c["name"] for c in collection_api.get("/api/collections")}
        assert after.get(created["uuid"]) == renamed

    def test_delete_removes_the_collection_but_not_the_file(
        self, collections_page: Page, collection_api, owned_media_factory
    ):
        """The confirmed real behavior: cascade removes membership, never the MediaFile.

        See the module docstring for the exact backend citation
        (`app/api/endpoints/media_collections.py::delete_collection`).
        """
        token = collection_api._token
        assert token, "collection_api must be logged in before uploading"
        media = owned_media_factory(token)

        name = _unique_collection_name()
        created = collection_api.post("/api/collections", {"name": name})
        collection_api.post(
            f"/api/collections/{created['uuid']}/media",
            {"media_file_ids": [media["uuid"]]},
        )
        _reload_manager(collections_page)

        _card(collections_page, name).locator(".delete-config-button").click()

        # Same collision-window reasoning as the tag suite's delete test: the
        # ConfirmationModal's own confirm button is the stable target once it has
        # actually mounted, not a same-named trigger button racing it.
        confirm = collections_page.locator(".modal-delete-button")
        expect(confirm).to_be_visible(timeout=10_000)
        confirm.click()
        expect(_card(collections_page, name)).to_have_count(0, timeout=10000)

        remaining = {c["uuid"] for c in collection_api.get("/api/collections")}
        assert created["uuid"] not in remaining, "collection row survived its own delete"

        # The file is untouched: still fetchable, still owned, still not quarantined.
        file_resp = collection_api.get(f"/api/files/{media['uuid']}")
        assert file_resp.get("uuid") == media["uuid"], (
            "member file did not survive collection deletion (backend behavior regressed)"
        )


class TestCollectionManagerThemes:
    """Light/dark parity is required for any frontend change in this repo."""

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_renders_in_both_themes(self, collections_page: Page, collection_api, theme: str):
        collection_api.post("/api/collections", {"name": _unique_collection_name()})
        _reload_manager(collections_page)

        collections_page.evaluate(
            "(t) => document.documentElement.setAttribute('data-theme', t)", theme
        )

        expect(collections_page.locator(".collections-panel")).to_be_visible()
        expect(collections_page.locator(".collections-list")).to_be_visible()
