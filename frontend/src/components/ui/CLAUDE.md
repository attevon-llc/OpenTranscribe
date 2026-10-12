# src/components/ui — shared UI primitives

## Purpose

Small, reusable, theme-aware, accessible building blocks. Prefer these over hand-rolling
the same pattern in a feature component. Import via `$components/ui/...`. Every primitive
supports light/dark via CSS custom properties and ships with a colocated `*.test.ts`.

## Catalog

Everything below lives directly under `src/components/ui/`. Note `ConfirmationModal.svelte`
does **not** — it lives at `src/components/ConfirmationModal.svelte`, one level up, alongside
the other feature components; it's listed here anyway since it's a shared confirm/cancel
primitive in the same spirit as this folder.

- `BaseModal.svelte` — modal shell (backdrop, header/body/footer slots, scroll lock). Its
  `zIndex` prop defaults to the shared `--z-modal` z-index tier (H5, see
  `src/styles/theme.css`'s `:root` block for the full scale).
- `ConfirmationModal.svelte` (`src/components/`, not `ui/`) — confirm/cancel dialog; use instead
  of hand-rolled `{#if show}+BaseModal`.
- `Tabs.svelte` — `role=tablist` with roving-tabindex keyboard nav (arrows/Home/End), badges;
  `bind:activeId` + `change` event. `TabItem` type exported from its module script.
- `SortDropdown.svelte` — the shared sort-field-and-direction control (gallery + search sort
  bars consolidated into one primitive, H2); takes a `sortOptions` prop (`SortOption[]`, with an
  optional `noDirection` entry for fixed-order fields like "relevance"), `ariaLabelKey`, and an
  `align: 'left' | 'right'` prop for menu placement. The direction arrow points **down =
  descending, up = ascending** (a `noDirection` option is always down); the glyph is drawn
  pointing that way, not rotated by CSS, and the same semantics apply on `/search`. Hand-rolls its own menu with
  `$lib/actions/clickOutside` (there is no generic `Dropdown.svelte` primitive — see Gotchas).
- `Badge.svelte` — semantic status pill (`default|success|warning|error|info`).
- `Chip.svelte` — compact removable/selectable pill (tags, filters).
- `MetadataChips.svelte` — renders a `Chip` row from a metadata object/list.
- `CopyButton.svelte` — click-to-copy control with a transient "copied" state.
- `SearchableSelect.svelte` — debounced async combobox (`fetchFn`/`getLabel` props, arrow-key nav).
- `SearchBar.svelte` — the shared search-input shell (icon, clear button, debounce).
- `ConnectionStatusBanner.svelte` — global banner for WebSocket/API connectivity loss.
- `ExpandableSection.svelte` — accessible collapsible (`aria-expanded`/`aria-controls`, chevron, slide).
- `RangeSlider.svelte` — the ONE slider. Label + live value badge + native `<input type="range">`
  - hint (+ optional muted "System default: X" via `defaultValue`). Props: `id label value(bind)
min max step disabled hint decimals unit format valueText defaultValue class`; extra attributes
    (`data-testid`, `aria-*`, `dir`) land on the native input; `on:input`/`on:change` forward the
    native events. **Contract: every slider uses `ui/RangeSlider`; its styling lives in
    `src/styles/range-slider.css`** (tokens `--slider-*`, `.range-input`). Raw `type="range"` in a
    template, or `::-webkit-slider-thumb`/`::-moz-range-thumb` rules outside that stylesheet and the
    two Plyr player files, fail `rangeSliderStructure.test.ts` (allowlist entries need a written
    reason). The look is the native slider tinted with `accent-color` on purpose: fill, thumb,
    keyboard and RTL direction come from the browser. The gallery's dual-thumb filter sliders
    (`svelte-range-slider-pips` in `FilterSidebar.svelte`) are intentionally separate (owner decision).
- `Spinner.svelte`, `ProgressBar.svelte`, `SkeletonLoader.svelte`, `ListRowSkeleton.svelte`,
  `CardGridSkeleton.svelte`, `EmptyState.svelte`, `TagInput.svelte`.

## Conventions / patterns

- **A11y is mandatory**: roles, `aria-*`, keyboard support, visible `:focus-visible`.
- **No hardcoded English** — user-visible text comes from props the caller translates via i18n,
  or from the i18n store. Don't bake copy into a primitive.
- Theme with `var(--primary-color)`, `var(--surface-color)`, `var(--border-color)`, etc.;
  never hardcode hex. Buttons reuse the global classes in `src/styles/form-elements.css`.
- Export shared types from a `<script context="module" lang="ts">` block (instance `<script>`
  cannot `export interface`).

## Gotchas

- Components using Svelte transitions (`slide`/`fade`) need the `Element.animate` stub in
  `src/test-setup.ts` to be unit-testable (jsdom lacks the Web Animations API).
- **There is no generic `Dropdown.svelte` primitive.** One existed (trigger + menu,
  `aria-haspopup`, Escape-to-close, click-outside) but had zero consumers — `UserDropdown`,
  `SegmentSpeakerDropdown`, and the sort dropdowns all hand-roll their own menu logic (with
  `$lib/actions/clickOutside` directly) — and migrating four live, working dropdowns onto an
  untested-in-production primitive was judged higher risk than the duplication it would have
  removed. It was deleted rather than adopted (H2). Don't re-add one speculatively; build it
  only alongside a real third-plus consumer that actually needs it.
