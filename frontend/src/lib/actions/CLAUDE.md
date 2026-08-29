# src/lib/actions — Svelte actions

## Purpose

Reusable `use:` actions for DOM behaviors that don't belong in a component. Import via
`$lib/actions/...`.

## Key files

- `clickOutside.ts` — dispatches a `click_outside` CustomEvent when a click lands outside
  the node. Capture-phase document listener, so it works even when inner handlers call
  `stopPropagation`.

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
