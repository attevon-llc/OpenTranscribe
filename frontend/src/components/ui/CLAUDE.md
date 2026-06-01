# src/components/ui — shared UI primitives

## Purpose

Small, reusable, theme-aware, accessible building blocks. Prefer these over hand-rolling
the same pattern in a feature component. Import via `$components/ui/...`. Every primitive
supports light/dark via CSS custom properties and ships with a colocated `*.test.ts`.

## Catalog

- `BaseModal.svelte` — modal shell (backdrop, header/body/footer slots, scroll lock).
- `ConfirmationModal.svelte` — confirm/cancel dialog; use instead of hand-rolled `{#if show}+BaseModal`.
- `Tabs.svelte` — `role=tablist` with roving-tabindex keyboard nav (arrows/Home/End), badges;
  `bind:activeId` + `change` event. `TabItem` type exported from its module script.
- `Dropdown.svelte` — trigger + menu; `aria-haspopup`/`aria-expanded`, closes on Escape and
  click-outside (uses `$lib/actions/clickOutside`); `bind:open`, slots `trigger` + default(menu).
- `Avatar.svelte` — initials (via `getInitials`) or image; `size` sm/md/lg.
- `Badge.svelte` — semantic status pill (`default|success|warning|error|info`).
- `ExpandableSection.svelte` — accessible collapsible (`aria-expanded`/`aria-controls`, chevron, slide).
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
- Adoption of these primitives across feature components happens during the split phases —
  see the plan. Don't mass-migrate without verifying no visual change.
