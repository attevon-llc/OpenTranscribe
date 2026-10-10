# frontend/src/components/gallery

## Purpose

Thin presentational children of `routes/+page.svelte` (the home gallery coordinator), split out
of that page. They render the file grid/list, header, filters, sort, and bulk-action toolbar.

## Key files

- `GalleryHeader.svelte` — the sticky two-row toolbar (issue #747): row 1 is the
  filename/title search (left) plus the primary action trio (right); row 2 is the
  count chip + select/selection tools (left) plus sort dropdown + view toggle
  (right). Row 2 is deliberately **unconditional** — never gated on `files.length`
  — see Gotchas.
- `GalleryFilterPanel.svelte` — collapsible filter sidebar (wraps `FilterSidebar.svelte`,
  rendered with `showSearchField={false}` since #747 moved filename/title search into the
  toolbar above).
- `GalleryGrid.svelte` — switches between `VirtualGrid`/`VirtualList`; empty/skeleton/loading
  states. Its empty state takes a `filtersActive` prop so a filtered-to-zero result set reads
  "no files match", not "your library is empty" (#747).
- `VirtualGrid.svelte` / `VirtualList.svelte` — virtualized renderers (thumbnail cache, prefetch).
- `GalleryPrimaryActions.svelte` — the row-1-right trio (Add media, Collections, Tags),
  normal mode only (reads `galleryStore`).
- `GallerySelectionActions.svelte` — row-2-left: "Select items" in normal mode, expanding in
  place into the full select-all/process/organize/delete/cancel toolbar in selection mode
  (reads `galleryStore`). Split out of the old `GalleryActionButtons.svelte` by #747 so each
  half sits in the row it belongs to; there is no single "gallery action buttons" component
  any more, only these two.
- `BulkTagModal.svelte` — adds or removes one tag across the selection. Reached from the
  Organize menu, and from the **Tags** button when files are selected.
- The **Tags** button (`GalleryActionButtons`) sits beside Collections and picks its mode
  from the selection, exactly as Collections does: a selection opens this modal, nothing
  selected opens `components/tags/TagManagerModal.svelte`. There is no `/tags` route —
  a tag is metadata over the library, not a destination.
- Sort control is `$components/ui/SortDropdown.svelte` (shared with search, H2), consumed
  directly by `GalleryHeader.svelte`. `GalleryViewToggle.svelte`, `GalleryCountChip.svelte` —
  small controls.

## Conventions / patterns

- Import via `$components/gallery/...`; shared selection/view state from `$stores/gallery`; i18n via `$t`.
- Children take props + `createEventDispatcher`; the page owns fetching and pagination.

## How it connects

- Parent/coordinator: `src/routes/+page.svelte`. Stores: `$stores/gallery`. Thumbnails:
  `$lib/thumbnailCache`; detail prefetch: `$lib/prefetch`.

## Gotchas

- **Filtering, sorting, and pagination are SERVER-driven**: the page builds `URLSearchParams`
  (`page`, `page_size`, `sort_by`, `sort_order`, `search`, `tag`, `speaker`, date/duration ranges)
  and refetches. Don't add client-side array filtering/sorting — emit a change event and let the
  page re-query.
- Virtual renderers manage their own scroll windowing — keep `scrollContainer` wiring intact.
- **The gallery listing carries no per-file tags.** `tags` lives on the `MediaFileDetail` schema,
  not the `MediaFile` one the paginated list returns, so `file.tags` is undefined here even though
  the TS type allows it. `BulkTagModal` therefore scopes its _remove_ suggestions to the selection
  only when something actually supplies them, and otherwise offers every tag and says so.
- **Bulk tag results are outcomes, not booleans.** `already_present` / `not_present` are
  _successful_ no-ops; only `failed` is a failure. Report changed and unchanged separately, and
  never let one refused file read as a failed batch.
- **A supplied tag name may not be the applied one** — tags resolve by normalized-exact match, so
  `Interview` applies the existing `interview` across the whole selection. Any surface that
  submits a typed name must name the tag that was actually applied.
- **E2E-guarded selectors owned here** (`backend/tests/e2e/test_gallery_actions.py`,
  `test_tag_management.py`, `test_collection_management.py`, `test_chat.py`,
  `test_auth_buttons.py`, `test_visual_regression.py`): `.file-card` / `.file-list-row`
  (`.file-card.selected` when checked) come from `VirtualGrid.svelte` / `VirtualList.svelte`
  inside `GalleryGrid.svelte`. `.upload-btn`, `.collections-btn`, `.tags-btn` are
  `GalleryPrimaryActions.svelte` (row 1 right, normal mode only). `.select-btn` / `.select-all-btn`
  (enter/exit selection mode), `.organize-btn`, `.process-btn`, `.delete-btn`, `.cancel-btn`, the
  Organize/Process dropdowns' `.dropdown-menu` / `.dropdown-item`, and
  `[data-testid="gallery-chat-with-selected"]` are all `GallerySelectionActions.svelte` (row 2
  left). Its `.dropdown-menu`/`.dropdown-item` are a **different** DOM subtree than the
  identically-named classes in `navbar/UserDropdown.svelte` — don't assume a rename there is safe
  here, or vice versa. `.toolbar-row-left` / `.toolbar-row-right` (one pair per row — there are
  two of each, scope by `.toolbar-row-top`/`.toolbar-row-bottom`) and the outer `.gallery-toolbar`
  are `GalleryHeader.svelte`; none of the three are gated on file count (issue #747 — see below).
  ⚠️ **`.gallery-action-buttons` is the single most load-bearing selector in the whole e2e
  suite**, not just gallery's own tests: `conftest.py`'s `authenticated_page`/`gallery_page`
  fixtures `wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)` as the
  "authenticated app shell finished painting" signal, and `test_login.py` asserts on its
  visibility/absence to confirm login succeeded or failed. It now lives on
  `GallerySelectionActions.svelte`'s wrapper (unconditional — present as a lone "Select items"
  button in normal mode, expanding in place in selection mode) rather than on the single
  `GalleryActionButtons.svelte` that used to hold both modes. Renaming it breaks essentially every
  other e2e file, not just this folder's.
  ⚠️ **The old `.gallery-header-right` file-count gate is GONE (issue #747), and so is the
  selector.** `GalleryHeader.svelte`'s row 2 (count chip, sort, view toggle) used to be wrapped in
  `{#if files.length > 0}` — which hid the sort control and view toggle on a filtered-to-zero
  result and made `gallery.noFilesMatch` unreachable. Row 2 is now unconditional. The e2e
  "file list has actually landed" readiness signal that gate used to double as is now
  `[data-testid="gallery-files-loaded"]`, rendered by `GalleryGrid.svelte` only in its real-content
  branch (not skeleton/empty/error) — `conftest.py`'s `gallery_page` fixture waits on this, not on
  `.gallery-header-right`. See `backend/tests/CLAUDE.md`'s e2e section and `conftest.py` itself.
