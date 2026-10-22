# src/lib/actions — Svelte actions

## Purpose

Reusable `use:` actions for DOM behaviors that don't belong in a component. Import via
`$lib/actions/...`.

## Key files

- `clickOutside.ts` — dispatches a `click_outside` CustomEvent when a click lands outside
  the node. Capture-phase document listener, so it works even when inner handlers call
  `stopPropagation`.

- `anchoredPanel.ts` — places every dropdown/menu/popover panel (portal to `<body>`, fixed, flips above,
  caps height, clamps, RTL, `data-side`). It also marks the panel `data-floating-panel`, which
  `styles/form-elements.css` uses to keep rows inside it flat (no global button shadow). Built on `$lib/popoverPosition.ts`; rule and usage in
  `components/ui/CLAUDE.md`. `portal.ts` is the bare portal it generalises.

## Conventions / patterns

- Usage:
  ```svelte
  <div use:clickOutside on:click_outside={close}>…</div>
  <div use:clickOutside={{ enabled: open, ignore: [triggerEl] }} on:click_outside={close}>…</div>
  ```
- Options: `enabled` (default true — make the action inert without unmounting),
  `ignore` (elements whose clicks should NOT count as outside, e.g. the trigger button so
  the opening click doesn't immediately re-close the panel). The action exposes `update()`
  so options stay reactive.
- The event name `click_outside` is typed in `src/svelte.d.ts` (HTMLAttributes augmentation).

## How it connects

- Consumed by `$components/ui/SortDropdown.svelte` (gallery + search sort), `UserDropdown`, and
  `SegmentSpeakerDropdown`.

## Gotchas

- Capture-phase is intentional — don't switch to bubble phase.
- Ship new actions with a colocated `*.test.ts` (see `clickOutside.test.ts` for the
  jsdom dispatch pattern).
