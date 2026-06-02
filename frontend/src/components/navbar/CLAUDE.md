# frontend/src/components/navbar

## Purpose

Thin presentational children of `Navbar.svelte` (the coordinator), split out of that file.

## Key files

- `NavbarBrand.svelte` — logo button (imports the logo asset for Vite); dispatches `about`.
- `UserDropdown.svelte` — the signed-in user menu (settings, Flower link, logout). Exports a
  module-context `NavbarUser` interface; dispatches `open` / `openSettings` / `logout`.

## Conventions / patterns

- Import via `$components`; i18n via `$t`; assets imported (not string paths) for Vite processing.
- Children take props + `createEventDispatcher`; `Navbar.svelte` owns auth state and routing.

## How it connects

- Parent: `$components/Navbar.svelte`. Flower URL via `$lib/utils/url`.

## Gotchas

- `UserDropdown.svelte` keeps the **E2E-guarded selectors** `.user-button`, `.dropdown-menu`,
  `.dropdown-item` — renaming breaks Playwright tests.
- It uses its **own bubble-phase `document` click-outside listener** (added on mount, removed on
  destroy) rather than the shared `clickOutside` action — keep that listener so the menu closes
  correctly. `:global(a.dropdown-item ...)` rules style anchor items inside the menu.
