# frontend/src/components/gallery

## Purpose

Thin presentational children of `routes/+page.svelte` (the home gallery coordinator), split out
of that page. They render the file grid/list, header, filters, sort, and bulk-action toolbar.

## Key files

- `GalleryHeader.svelte` — title row: count chip, sort dropdown, view toggle, action buttons.
- `GalleryFilterPanel.svelte` — collapsible filter sidebar (wraps `FilterSidebar.svelte`).
- `GalleryGrid.svelte` — switches between `VirtualGrid`/`VirtualList`; empty/skeleton/loading states.
- `VirtualGrid.svelte` / `VirtualList.svelte` — virtualized renderers (thumbnail cache, prefetch).
- `GalleryActionButtons.svelte` — bulk select/reprocess/delete toolbar (reads `galleryStore`).
- `BulkTagModal.svelte` — adds or removes one tag across the selection. Reached from the
  Organize menu, and from the **Tags** button when files are selected.
- The **Tags** button (`GalleryActionButtons`) sits beside Collections and picks its mode
  from the selection, exactly as Collections does: a selection opens this modal, nothing
  selected opens `components/tags/TagManagerModal.svelte`. There is no `/tags` route —
  a tag is metadata over the library, not a destination.
- `GallerySortDropdown.svelte`, `GalleryViewToggle.svelte`, `GalleryCountChip.svelte` — small controls.

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
  the TS type allows it. `BulkTagModal` therefore scopes its *remove* suggestions to the selection
  only when something actually supplies them, and otherwise offers every tag and says so.
- **Bulk tag results are outcomes, not booleans.** `already_present` / `not_present` are
  *successful* no-ops; only `failed` is a failure. Report changed and unchanged separately, and
  never let one refused file read as a failed batch.
- **A supplied tag name may not be the applied one** — tags resolve by normalized-exact match, so
  `Interview` applies the existing `interview` across the whole selection. Any surface that
  submits a typed name must name the tag that was actually applied.
