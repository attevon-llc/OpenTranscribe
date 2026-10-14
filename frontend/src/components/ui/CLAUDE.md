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

- `BaseModal.svelte` — **the one modal chrome** (backdrop, header with title + close, body, footer
  slot, scroll lock, focus trap, Escape, full-screen under 768px). Every dialog is built on it; a
  component that draws its own dimmed fixed overlay fails `components/modalChrome.convention.test.ts`
  (its `ALLOWED` list holds the only exceptions, each with a reason: the settings shell, the two
  consent banners, drawer/popover scrims). Width is `size="sm|md|lg|xl|2xl"` (480/600/720/960/1200),
  not a pixel prop; `titleId` is only for a caller that needs a stable `aria-labelledby`. The footer
  normalises every `<button>` to one size, radius and weight whatever class vocabulary the caller uses
  (`btn`, `modal-button`, ad hoc) — colour stays the caller's; order is dismiss first, primary last;
  a form in the body is submitted from the footer with `<button type="submit" form="the-form-id">`.
  Its `zIndex` prop defaults to the shared `--z-modal` tier (H5, see `src/styles/theme.css`).
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
- `MenuIcon.svelte` — the icon in front of a menu item or button label (glyph data in
  `$lib/ui/menuIcons.ts`: Feather/Lucide shapes, 14px default, 2px round `currentColor` strokes,
  `aria-hidden`). **Every `.dropdown-item` carries an icon** (gallery Organize/Process set the
  convention; `components/menuIcons.convention.test.ts` scans all markup and fails on a bare one).
  One distinct glyph per transcript export format. Add a glyph to `MENU_ICONS`; do not paste SVG.
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
    reason). The input stays native (keyboard, RTL direction) but the track is drawn from
    `--range-fill` (0..1, set inline by the component) because the native accent-color fill stops at
    the thumb centre and left a grey cap at the maximum; `--range-dir` mirrors it in RTL. The look is a
    rounded grey track, blue fill, and an **oval thumb with a white ring and soft shadow**
    (`--slider-thumb-width` > `--slider-thumb-size`, `--slider-thumb-border`), defined once for
    `::-webkit-slider-thumb` and `::-moz-range-thumb` together. The gallery's dual-thumb duration slider
    (`svelte-range-slider-pips`) is skinned from the same tokens by adding `slider-dual` to its wrapper (`styles/range-slider-dual.css`, imported by
    `FilterSidebar`, kept out of the root layout's static bundle budget);
    the library redefines `--slider-accent` on `.rangeSlider`, so that block reads `--dual-accent`.
    `--slider-thumb-size` doubles as the unit the media players read: keep it defined.
- `DateRangePicker.svelte` — the ONE date-range control (gallery filter sidebar and File Status
  filters). A field-styled trigger opens a **popover**: a `position: fixed` layer portaled to
  `<body>` (so it never grows the card it sits in and a scrolling sidebar cannot clip it), placed by
  `$lib/popoverPosition.ts` (below the trigger, flips above when it does not fit, clamps to the
  viewport, aligns to the trigger's end edge in RTL, becomes a bottom sheet under 560px). It holds
  the typed From/To `type="date"` fields (the keyboard path) and the calendar; Escape, an outside
  click or completing a range closes it and focus returns to the trigger. Wraps
  `@svelte-plugins/datepicker` with `alwaysShow` + `isOpen`, whose day buttons must be un-styled from
  the global `button` rule (`:where()` reset in the component). Do not add a second range picker.
- `SettingsSkeleton.svelte` — the loading placeholder every settings panel and `LazySection` uses
  (`variant` form / rows / chips / dashboard on the shared shimmer; optional `label` makes it a status region).
- `EmptyState.svelte` — icon -> title -> description -> actions, spaced by the
  `--empty-state-*` tokens. The default slot IS the action row (`.empty-actions`, flex, wraps);
  callers put buttons there with no margin of their own (issue #1243 item 16: a caller-spaced
  "Open Review" sat 0px under its description).
- `Spinner.svelte`, `ProgressBar.svelte`, `SkeletonLoader.svelte`, `ListRowSkeleton.svelte`,
  `CardGridSkeleton.svelte`, `TagInput.svelte`.

## Conventions / patterns

- **Form fields are not styled per component.** `src/styles/form-elements.css` paints every
  text-like input, textarea and select (see `src/styles/CLAUDE.md`); a primitive sets layout and
  size only. `styles/formFields.test.ts` fails on a re-painted field. A wrapper that draws the box
  around a bare embedded input (`SearchBar`, `TagInput`, the chat composer) uses the `--field-*`
  tokens, not literals.
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
