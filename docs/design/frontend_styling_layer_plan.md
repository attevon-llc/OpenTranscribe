# Frontend master styling layer: design tokens, shared components, migration plan

Plan only. Written 2026-11-01 against `chore/repo-cleanup` @ `96e8fd85b`. Nothing here is
implemented yet. This file is the single source for the work: a fresh implementer with no
conversation history should be able to execute any lane from it alone.

Context: `docs/design/repo_cleanup_plan.md` section 0b point 3 (frontend duplicate and abstraction
pass: shared components, design tokens with light/dark parity, verified in a real browser on a
fresh stack, 12 locales in parity, `ar` is RTL) and point 2 (long Svelte stylesheets are solved by
a master styling layer and shared components, not by splitting components). The inventory that
motivated it is `~/ot-cleanup-work/audit/F_frontend_findings.md`, section "Inventory for the
upcoming frontend wave". Every number below was re-measured for this plan (section 2); several
audit figures were stale or wrong.

---

## 1. Decisions for the owner

Every visible change in this plan traces to one of these. If there is no answer, the default
applies. Each lane that applies a decision names it in its commit body.

**Status (2026-11-01):** D1 to D15 accepted by the cleanup lead at the recommended defaults (owner direction: industry best practice, keep the app working). The visible changes they cause (danger/warning button fills, dark focus ring, control borders, single tint blue, dark elevation) are listed per lane and are verified by the before/after screenshot tool.

| # | Decision | Default (recommended) | Why / measured evidence |
|---|---|---|---|
| D1 | Literal colours move onto role tokens even where the token's **dark** value differs from the literal (dark mode changes; light mode does not, or changes only where D-listed). | **Yes**, per surface lane, each lane's computed-style diff is its record. | 132 of the 215 files with colour literals have no dark override, so they render light-mode colours in dark mode today. Example: `#6b7280` text on the dark card is 3.0:1; `--text-secondary` dark is 9.85:1. |
| D2 | One tint blue: after the zero-diff freeze (lane 1b), point `--accent-rgb` at `--primary-color-rgb` (59,130,246 becomes 37,99,235). | **Yes**, one token edit in lane 3.0, reviewed once across all screens. | 188 `rgba(59,130,246,α)` literals (after the fallback strip) sit beside 171 uses of `--primary-color-rgb`. The tint difference at α ≤ 0.25 is about #ebf2fe vs #e9effd on white. |
| D3 | Solid danger and warning fills must carry their label at AA. Danger fill becomes `--error-solid` (#dc2626) and its hover `--error-solid-hover` (#b91c1c). Warning fill keeps amber `#f59e0b` with **dark** text `--text-on-warning` (#1e293b). | **Yes** (lane 2.0b). | White on `#ef4444` (`.btn-danger`, `.modal-delete-button`, used by 20 confirm dialogs) is 3.76:1. White on `#f59e0b` (`.btn-warning`, `.modal-warning-button`) is 2.15:1. White on `#dc2626` is 4.83:1, and `#1e293b` on `#f59e0b` is 6.81:1. The alternative for warning is `#b45309` with white text (5.02:1), which shifts the hue to brown. |
| D4 | Focus ring colour gets a dark value: `--focus-ring-color` is #2563eb in light and #60a5fa in dark. | **Yes** (lane 2.0b). | `button:focus-visible` outlines with `--primary-color`, which is 2.83:1 on the dark card. That is below 3:1 (SC 1.4.11). #60a5fa is 5.75:1. |
| D5 | One switch: 44x24 px, `role="switch"`, logical positioning, and an off-track that meets 3:1. | **Yes** (lane 2.1). | 23 hand-rolled toggles use 7 sizes (36x20 x11, 44x24 x4, 48x24 x2, 2.75x1.5rem x2, 28x16 x2, 32x18, 30x16). Only `privacy/PrivacySwitchCard` has `role="switch"` and mirrors in RTL. A 20 px tall control is under the 24 px target minimum (SC 2.5.8). The off-track `#e5e7eb` on white is 1.24:1. |
| D6 | Control boundaries reach 3:1: field border and switch off-track use `--control-border` (#838fa1 light, 3.28:1 on white; #7c8aa0 dark, 4.18:1 on the card). | **Yes** (lane 2.0b). | `--input-border` #d1d5db is 1.47:1 on white. Text inputs are identified only by that border. |
| D7 | Spacing and type literals in `px` become `rem` tokens. They are pixel-identical at the default 16 px root but scale with the user's browser font-size setting. | **Yes** (lanes 1d/1e). | WCAG 1.4.4. Screenshots at defaults are unchanged. Only users with a non-default font size see a difference. |
| D8 | Snap off-scale values to the nearest step: spacing (645 within 1 px), font size (621 within 1 px), radius 3px/5px. | **Yes**, separate per-surface lanes after Phase 3 (Phase 4). | Values like `0.6rem`, `0.85rem` and `0.78rem` are near-duplicates, not design intent (section 2). |
| D9 | Guard implementation: a stdlib Python ratchet in `scripts/guards/` vs stylelint. | **Python ratchet** (lane 0.3). | It reuses `_baseline.py`, the hook pattern and the unit-test pattern of the 12 existing guards, and adds no npm dependency. Stylelint would need `stylelint`, `postcss-html` and a config, plus its own baseline mechanism. |
| D10 | Deprecated token aliases (`--text-muted`, `--color-text`, `--bg-secondary`, ...) stay **defined** in `theme.css`; core stops using them. | **Keep definitions**; revisit only after checking the managed-edition overlay. | `$lib/cloud` is replaced at image build by a private overlay that may reference aliases. Deleting one would silently unstyle that edition (an undefined `var()` with no fallback is invalid at computed-value time). |
| D11 | Ship `routes/a11y-fixtures/ui-kit/+page.svelte` (every shared component in every state) in the normal build. | **Yes**, following the existing `a11y-fixtures/status-badges` precedent: authenticated, no backend calls, own lazy route chunk. | Without it, live-progress, busy and error states cannot be screenshotted or axe-scanned deterministically on a fresh stack. |
| D12 | One form-field layout: settings/modals use label 13 px / 500 / `--text-color`, gap 6 px. Auth pages use `size="lg"` (label 14 px, gap 8 px). | **Yes** (lane 2.2). | 39 local `.form-group` rules fall into about 4 families (section 5.3). Watch-source and email modals render labels in `--text-secondary` at 13.6 px and will change to the canonical label. |
| D13 | Dark-mode elevation: shadow tokens get stronger dark values (alpha raised, as `--card-shadow` already does: 0.1 light, 0.3 dark). | **Yes**, one token edit in lane 3.0 after the freeze. | During Phase 1 every shadow token carries the same value in both themes, so the migration stays pixel-identical. |
| D14 | `ConfirmationModal`'s `.confirm-button:hover` repaints `#2563eb`, the same as its rest colour (no hover feedback). Fix it to `--primary-hover`. | **Yes** (lane 2.5a). | Hover-only change. |
| D15 | Sequencing against Wave 10 (component splits): Phase 0 and Phase 1 land **before** any Wave 10 lane opens. Phases 2 to 5 take a file only when no other lane holds it, and a Wave 10 file is restyled after its split. | **Yes**. | Phase 1 is mechanical and touches 300+ files. A concurrent split of the same file would conflict. |

No decision is needed for: removing the 613 dead colour fallbacks (provably pixel-identical,
section 3.11), adding tokens (additive), adding `role="progressbar"` to `ui/ProgressBar` (no pixel
change), or replacing native `confirm()` in `LockedAccountsPanel` (already decided as F-9).

---

## 2. Measured baseline (2026-11-01, `96e8fd85b`)

Scripts (scratch, not in the repo): `~/ot-cleanup-work/styling_measure/{measure.py,raw.py,roles.py,fallbacks.py,coverage.py}`.
Run each as `python3 -I <script> /mnt/nvm/repos/transcribe-app-wt-cleanup` (`measure.py` writes
JSON to stdout). Scope: tracked `frontend/src/**/*.svelte` `<style>` blocks plus
`frontend/src/**/*.css`, comments stripped, `styles/theme.css` excluded for colours.
**Re-run them before every lane and record the numbers in the lane's commit body.** These counts
will drift.

| Measure | Value | Audit said | Note |
|---|---|---|---|
| Numeric colour literals (hex + `rgb/rgba/hsl` with numeric channels) | **1,895** (1,004 hex + 891 rgb) in **215** files | 2,071 | The audit also counted `rgba(var(...))` tints, which are token-based. |
| of which `var(--defined-token, <literal>)` fallbacks | **613** (+2 pointing at the undefined `--color-error`) in 142 files | not measured | The fallback never applies, because every referenced token is declared on `:root`. Stripping them is pixel-identical. |
| `rgba(59,130,246,α)` (not the theme primary) | **204** in 67 files (188 after the fallback strip, 60 files) | 206 | `#3b82f6` is the theme's `--info-color` (light), not `--primary-color` (#2563eb). |
| `rgba(239,68,68,α)` | 109 | 73 | |
| Files with any `[data-theme='dark']` rule | 84 (235 rules) | 78 / 217 | 132 files have literals and no dark rule. |
| Literals inside dark-scoped rules | 311 | | |
| `box-shadow` declarations | 440; 121 distinct; 123 `none`; 25 `var()` | 444 / 129 | |
| Radius tokens | 1,205 (31 distinct); on the scale (2/4/6/8/10/12/16 px) **89%** exactly | 44 distinct | 999px x41, 50% x105, 3px x37 (off-scale), 5px x8 |
| Spacing length tokens (padding/margin/gap) | 4,892 non-zero; **86%** exactly on the 4 px scale; 645 within 1 px; 28 further | 416 distinct (full values) | Top off-scale: 0.6rem 139, 0.4rem 100, 0.35rem 87, 0.3rem 57, 1.2rem 36 |
| `font-size` | 1,854 decls, 72 distinct; 149 already `var(--font-size-*)`; **62%** exactly on the existing scale + 16 px; 621 within 1 px | 78 distinct | Top off-scale: 0.8rem 170, 0.85rem 109, 0.9rem 77, 0.95rem 56 |
| `font-weight` | 500 x364, 600 x288, 400 x27, `var()` x124, `bold`/700 x4 | | |
| Transition/animation durations | 0.2s 270, 0.15s 155, 0.3s 45, 0.12s 10, 0.25s 5 (long loops: 1.5s 16, 1s 11, 2s 10) | | |
| `z-index` | 82 decls; 8 already tokens | ~95 | |
| Physical direction properties (`margin-left/right`, `padding-*`, `border-*`, `left/right`, `text-align/float left/right`) | **400** in 136 files | | Top: Navbar 16, VideoPlayer 16, TranscriptSegmentList 14, PlyrMiniPlayer 12, SummaryDisplay 11 |
| Svelte syntax | 275 files `export let`, **0** files `$props()` | | New components use the legacy syntax (`export let`, slots, `createEventDispatcher`) to match. |

**The audit was wrong on one point:** `theme.css` **does** have a type scale (`--font-size-2xs ...
--font-size-2xl`, weights, line heights, `--measure-prose`; issue #1243, already used 149 + 124
times). It has no spacing, radius or motion scale.

Duplicated patterns, re-counted (file lists in Appendix A):

| Pattern | Files | Existing shared piece |
|---|---|---|
| Local button classes (`.btn-primary/-secondary/-danger`, `.cancel-/save-/submit-/delete-button`) | 40 | Global `.btn*` and `.modal-*-button` in `styles/form-elements.css` |
| Two spellings of the small button: `btn-sm` (27 markup uses, defined locally) and `btn-small` (11, global) | | `.btn-small` |
| Local `btn-ghost` (14), `btn-link` (6), `icon-btn/icon-button` (21), `btn-danger-outline` (5 files) | | none |
| Local `.form-group` | 39 | none (`form-elements.css` styles the controls only) |
| Hand-rolled toggle switches | 23 (+ PrivacySwitchCard, the accessible one) | none |
| `<ConfirmationModal>` with hand-rolled state | 23 (+ `ConfirmationModalTestHost`) of 29; native `confirm()` in `LockedAccountsPanel` x2 | `lib/confirmDialog.ts` (5 users) |
| Local `.progress-bar/.progress-fill` | 9 (incl. `ui/ProgressBar` itself) | `ui/ProgressBar` (6 users, no `role="progressbar"`) |
| Live progress card (spinner + count + bar + ETA + stop) | 4: `settings/ReindexLiveProgress`, `EmbeddingMigrationSettings`, `AutoLabelSettings`, `SpeakerAttributeBulkPanel` (each still has its own `formatEta`) | none |
| Local `.error-message/.error-text` | 15 | none |
| Local `.status-badge` | 8 | `ui/Badge` (14 users), `settings/StatusChip` (12) |
| Local `.empty-state` | 7 (excluding `ui/EmptyState`) | `ui/EmptyState` (29 users) |
| Local `.banner` | 6 | `ui/ConnectionStatusBanner`, `ui/AuthMailNotice` (purpose-built) |
| Local `.spinner` / `@keyframes spin` | 5 | `ui/Spinner` (84 users) |
| Local `.sr-only` / `.visually-hidden` | 10 definitions | none global |

Colour literals outside stylesheets: `lib/utils/speakerColors.ts` (150, data palette, already
allowlisted by `noPurple.test.ts`), `lib/components/ClassificationBanner.svelte` (14, regulatory
CUI colours), `ThemeToggle.svelte` markup (12 SVG attributes), `LoginBanner` (9),
`AISuggestionsDropdown` (6), `Toast` (4), `WaveformPlayer` (3, canvas: needs
`getComputedStyle`-read tokens). These are handled in lane 3.9, not by the stylesheet guard.

---

## 3. Token set

### 3.1 Architecture

- **One token source: `frontend/src/styles/theme.css`.** This keeps the convention
  `frontend/CLAUDE.md` already documents ("Tokens live in `src/styles/theme.css`") and the tests
  that read that path (`primary-contrast.test.ts`, `typography.test.ts`, `UploadTray.test.ts`).
  - Colour roles: light on `:root`, dark on `[data-theme='dark']`.
  - Scales (space, radius, type, z, motion): **once** on `:root`, theme-independent. "Parity" for
    a scale token means it is deliberately not redefined in dark, and a test enforces that.
  - Shadows: declared in **both** blocks, because elevation reads differently on dark surfaces
    (D13).
- **Component tokens stay beside their component's global stylesheet.** `--field-*` stays in
  `form-elements.css`, `--slider-*` in `range-slider.css`, and the new `--switch-*` goes in
  `ui/Toggle.svelte` scoped CSS. They must be defined in terms of role tokens, never literals
  (the guard enforces this).
- **No palette tier** (no `--blue-500`). A literal-to-palette pass would be pixel-identical but
  buys nothing the ratchet guard does not already give (new literals blocked, existing ones
  frozen per file). It would also double the churn: each value would be touched once to the
  palette, then again to a role. Components reference **roles** only.
- **Freeze then flip** for the two cases where a whole family must move at once (D2 accent tint,
  D13 dark shadows). Phase 1 introduces the token with values equal to today's literal in **both**
  themes, so the migration is pixel-identical. Phase 3 then changes the token value in one commit,
  and that commit is the one reviewed diff.
- Section order inside `theme.css` (append new sections; do not reorder existing ones, because
  the tests slice by selector): z-index (exists), colour roles (exists; add the new roles next to
  their family), type (exists; add `--font-size-base`), **space (new)**, **radius (new)**,
  **elevation (new)**, **motion (new)**, **focus & controls (new)**, aliases (exists; add the
  deprecation header).

### 3.2 Colour roles (canonical names; light / dark)

"Exists" means the token is already in `theme.css` with these values: keep it. "New" means add it.
Canonical names were chosen by measured usage (`--text-secondary` 712 uses, `--border-color` 653,
`--text-color` 561, `--primary-color` 445, `--primary-on-surface` 257, `--surface-color` 249).

**Surfaces and text**

| Token | Light | Dark | Status | Use |
|---|---|---|---|---|
| `--background-color` | #f8fafc | #0f172a | exists | page |
| `--surface-color` | #ffffff | #1e293b | exists | cards, modals, menus |
| `--surface-secondary` | #f1f5f9 | #1e293b | exists | sunken panels, table headers |
| `--hover-color` | #f1f5f9 | #334155 | exists | row/list hover |
| `--button-hover` | rgba(0,0,0,.05) | rgba(255,255,255,.1) | exists | translucent hover on any surface |
| `--text-color` | #1e293b | #f8fafc | exists | body text |
| `--text-secondary` | #64748b | #cbd5e1 | exists | secondary/meta text (4.76:1 on white, 9.85:1 on dark card) |
| `--text-tertiary` | #94a3b8 | #94a3b8 | exists (alias of `--color-text-tertiary`) | **disabled and placeholder only**: 2.56:1 on white fails AA for text |
| `--text-on-tint` | #475569 | #cbd5e1 | exists | secondary text on any status tint |
| `--text-on-solid` | #ffffff | #ffffff | **new** | text/icons on `--primary-color`, `--*-solid` fills and media |
| `--text-on-warning` | #1e293b | #1e293b | **new** | text on the amber warning fill (D3) |

**Borders and controls**

| Token | Light | Dark | Status | Use |
|---|---|---|---|---|
| `--border-color` | #e5e7eb | #334155 | exists | dividers, card outlines (decorative, no 3:1 requirement) |
| `--border-hover` | #cbd5e1 | #475569 | exists | hovered card outline |
| `--border-light` | #f1f5f9 | #293548 | exists | hairline inside a card |
| `--control-border` | #838fa1 | #7c8aa0 | **new** (D6) | boundary of an input, select, switch off-track, checkbox (3.28:1 / 4.18:1) |
| `--focus-ring-color` | #2563eb | #60a5fa | **new** (D4) | every `:focus-visible` outline (5.17:1 / 5.75:1) |
| `--focus-ring` | `0 0 0 3px rgba(var(--primary-color-rgb), .25)` | same | **new**; `--field-focus-ring` becomes `var(--focus-ring)` | soft halo in addition to the outline |

**Primary / accent**

| Token | Light | Dark | Status |
|---|---|---|---|
| `--primary-color` | #2563eb | #2563eb | exists (fill only; white text 5.17:1) |
| `--primary-hover` | #1d4ed8 | #1d4ed8 | exists |
| `--primary-on-surface` | #1d4ed8 | #93c5fd | exists (blue text/icon on a surface) |
| `--primary-light` | #dbeafe | #1e3a8a | exists (selected-row fill) |
| `--primary-color-rgb` | 37, 99, 235 | 37, 99, 235 | exists |
| `--accent-rgb` | 59, 130, 246 | 59, 130, 246 | **new**, frozen. Lane 3.0 (D2) changes it to `var(--primary-color-rgb)` in both blocks. |

**Status families.** Each family has the same six roles. Add the missing ones as new tokens and
leave the existing ones in place.

| Family | `-color` (icon, dot, border) | `-solid` (fill with label) | `-solid-hover` | `-text` (text on surface or own tint) | `-bg` (tint) | `-border` | `-color-rgb` |
|---|---|---|---|---|---|---|---|
| error | #ef4444 / #f87171 (exists) | #dc2626 / #dc2626 (exists `--error-solid`) | **new** #b91c1c / #b91c1c | **new** `--error-text` = `var(--color-error-text)` (#b91c1c / #f87171) | exists `--danger-bg` 0.1; **new** canonical `--error-bg` | exists 0.3 | exists |
| warning | #f59e0b / #fbbf24 (exists) | **new** #f59e0b / #f59e0b, label `--text-on-warning` (D3) | **new** #d97706 / #d97706 | exists `--warning-text` (#b45309 / #fbbf24) | exists 0.1 (dark 0.15 via `--color-warning-bg`) | exists 0.3 | exists |
| success | #10b981 / #34d399 (exists) | **new** #047857 / #047857 (white 5.48:1) | **new** #065f46 | **new** `--success-text` = `var(--color-success-text)` (#047857 / #6ee7b7) | exists | exists | exists |
| info | #3b82f6 / #60a5fa (exists) | **new** `var(--primary-color)` | **new** `var(--primary-hover)` | **new** `--info-text` = `var(--color-info-text)` (#1d4ed8 / #93c5fd) | exists `--color-info-bg`; **new** canonical `--info-bg` | exists `--color-info-border`; **new** `--info-border` | **new** 59,130,246 / 96,165,250 |

Rule for implementers: `-color` never carries text. Text uses `-text`. A fill with a label uses
`-solid` and `--text-on-solid` (or `--text-on-warning`).

### 3.3 Spacing (new; theme-independent; rem, 4 px base)

| Token | Value | px @16 | Replaces exactly (rem / px) | Measured uses |
|---|---|---|---|---|
| `--space-0-5` | 0.125rem | 2 | 0.125rem, 2px | 73 + 66 |
| `--space-1` | 0.25rem | 4 | 0.25rem, 4px | 308 + 159 |
| `--space-1-5` | 0.375rem | 6 | 0.375rem, 6px | 180 + 130 |
| `--space-2` | 0.5rem | 8 | 0.5rem, 8px | 939 + 249 |
| `--space-2-5` | 0.625rem | 10 | 0.625rem, 10px | 89 + 79 |
| `--space-3` | 0.75rem | 12 | 0.75rem, 12px | 595 + 151 |
| `--space-3-5` | 0.875rem | 14 | 0.875rem, 14px | 23 + 35 |
| `--space-4` | 1rem | 16 | 1rem, 16px | 587 + 98 |
| `--space-5` | 1.25rem | 20 | 1.25rem, 20px | 63 + 34 |
| `--space-6` | 1.5rem | 24 | 1.5rem, 24px | 164 + ... |
| `--space-8` | 2rem | 32 | 2rem, 32px | 76 + ... |
| `--space-10` | 2.5rem | 40 | 2.5rem, 40px | 12 + ... |
| `--space-12` | 3rem | 48 | 3rem, 48px | 12 + ... |
| `--space-16` | 4rem | 64 | 4rem, 64px | few |

Raw values that stay allowed (the guard does not count them): `0`, `auto`, `1px`, `-1px`
(hairlines and border compensation), percentages, `em` (glyph-relative), and anything inside
`calc()`/`min()`/`max()`/`clamp()` that already uses a token. A negative step is written
`calc(-1 * var(--space-N))`.

Snap table for Phase 4 (D8), nearest step, ties go up: 0.6rem → `--space-2-5`, 0.4rem →
`--space-1-5`, 0.35rem → `--space-1-5`, 0.3rem → `--space-1`, 1.2rem → `--space-5`, 0.2rem →
`--space-1`, 0.15rem → `--space-0-5`, 0.1rem → `--space-0-5`, 0.7rem → `--space-3`, 0.85rem →
`--space-3-5`, 0.45rem → `--space-2`, 3px → `--space-1`, 0.65rem → `--space-2-5`, 0.55rem →
`--space-2`, 0.9rem → `--space-3-5`, 0.8rem → `--space-3`.

### 3.4 Radius (new; theme-independent; px on purpose, because radii should not scale with text)

| Token | Value | Replaces exactly | Uses |
|---|---|---|---|
| `--radius-xs` | 2px | 2px, 0.125rem | 21 |
| `--radius-sm` | 4px | 4px, 0.25rem | 190 + 5 |
| `--radius-md` | 6px | 6px, 0.375rem | 283 + 1 |
| `--radius-lg` | 8px | 8px, 0.5rem (= `--field-radius`, which becomes `var(--radius-lg)`) | 241 + 3 |
| `--radius-xl` | 10px | 10px (global `button`, `.modal-button`) | 105 |
| `--radius-2xl` | 12px | 12px, 0.75rem | 39 + 1 |
| `--radius-3xl` | 16px | 16px | 6 |
| `--radius-pill` | 999px | 999px, 9999px (identical for any box under 1998 px tall; the A/B run proves it) | 41 + 2 |
| `--radius-round` | 50% | 50% | 105 |

Phase 4 snaps: 3px → `--radius-sm` (37), 5px → `--radius-md` (8), 20px/24px on pill-shaped
chips → `--radius-pill` (only where the box height is ≤ 2x the radius; otherwise
`--radius-3xl`), 34px (`.theme-toggle-slider`) → `--radius-pill`.

### 3.5 Type (exists, issue #1243; one addition)

Keep `--font-size-2xs` (11px) `xs` (12) `sm` (13) `md` (14) `body` (15) `lg` (17) `xl` (20) `2xl` (24),
the weights and the line heights. **Add `--font-size-base: 1rem` (16px)**: 1rem x54 + 16px x13 are
exact uses with no step today. Also add `--line-height-none: 1` (22 uses) and
`--line-height-normal: 1.5` (83 uses, the `app.html` body default).

Exact mapping (Phase 1, lane 1e): 0.6875rem/11px → `2xs`, 0.75rem/12px → `xs`, 0.8125rem/13px → `sm`,
0.875rem/14px → `md`, 0.9375rem/15px → `body`, 1rem/16px → `base`, 1.0625rem/17px → `lg`,
1.25rem/20px → `xl`, 1.5rem/24px → `2xl`. Weights: 400 → `--font-weight-regular`, 500 →
`medium`, 600 → `semibold`. `bold`/700 (4 uses) → `semibold` is a rule from #1243 and a visible
change, so it lands in Phase 4.

Phase 4 snaps (D8): 0.8rem → `sm`, 0.85rem → `md`, 0.9rem → `md`, 0.95rem → `body`, 0.7rem → `2xs`,
0.78rem → `xs`, 0.72rem → `xs`, 0.82rem → `sm`, 0.76rem → `xs`, 0.87rem → `md`, 0.775rem → `xs`,
0.65rem/0.625rem/10px/0.6rem → `2xs` (raises sub-11 px text to 11 px), 1.1rem → `lg`,
1.125rem/18px → `lg`. Line height 1.4 → `--line-height-snug` (1.45), 1.2 → `--line-height-tight`
(1.3).

### 3.6 Elevation (new; both themes)

Values were chosen to equal the most frequent literals exactly (section 3.11), so Phase 1 is
pixel-identical. The proposed dark values are applied in lane 3.0 (D13).

| Token | Light (= Phase 1 dark) | Dark after D13 | Replaces exactly | Uses |
|---|---|---|---|---|
| `--shadow-xs` | 0 1px 3px rgba(0,0,0,0.05) | 0 1px 3px rgba(0,0,0,0.25) | `.btn-secondary` rest | 2+ |
| `--shadow-sm` | 0 1px 3px rgba(0,0,0,0.1) | 0 1px 3px rgba(0,0,0,0.3) | same literal; `--card-shadow` becomes `var(--shadow-sm)` in lane 3.0 | 7 (+9 `--card-shadow`) |
| `--shadow-md` | 0 2px 4px rgba(0,0,0,0.1) | 0 2px 4px rgba(0,0,0,0.3) | global `button` rest | 6 |
| `--shadow-hover` | 0 4px 8px rgba(0,0,0,0.1) | 0 4px 8px rgba(0,0,0,0.35) | button hover lift | 6 |
| `--shadow-lg` | 0 4px 12px rgba(0,0,0,0.15) | 0 4px 12px rgba(0,0,0,0.4) | menus, popovers | 6 |
| `--shadow-xl` | 0 4px 20px rgba(0,0,0,0.15) | 0 4px 20px rgba(0,0,0,0.45) | `.modal`, `.dialog` | 1+ |
| `--shadow-thumb` | 0 1px 2px rgba(0,0,0,0.2) | 0 1px 2px rgba(0,0,0,0.4) | switch/slider knobs | 8 |
| `--shadow-primary` | 0 2px 4px rgba(var(--accent-rgb),0.2) | same expression | primary button rest | 30 (+20 `--primary-color-rgb` twins after D2) |
| `--shadow-primary-hover` | 0 4px 8px rgba(var(--accent-rgb),0.25) | same | primary hover | 22 (+17 twins) |
| `--shadow-danger` | 0 2px 4px rgba(var(--error-color-rgb),0.2) | same | danger button (lane 2.0b) | 12 |
| `--shadow-danger-hover` | 0 4px 8px rgba(var(--error-color-rgb),0.3) | same | | 5+5 |
| `--shadow-none` | none | none | not needed; `none` stays allowed | |

The other ~100 distinct shadows (1 to 5 uses each) snap to the nearest step in Phase 4.

### 3.7 Z-index (exists, H5)

Keep `--z-base 1`, `--z-sticky 10`, `--z-dropdown 1000`, `--z-navbar 1200`, `--z-modal 1300`,
`--z-toast 9999`, `--z-critical 10000`, `--z-max 99999`. Phase 1 replaces only exact tier values.
Other values (2 x5, 5 x2, 20 x4, 25, 29, 30, 100 x3, 999 x2, 1001, 1099, 1100, 99997, 99998 x2)
are relative stacking. They become `calc(var(--z-<tier>) + n)` in lane 3.10, with a
stacking check (menu-in-modal, toast-over-modal, fullscreen player).

### 3.8 Motion (new; theme-independent)

| Token | Value | Replaces exactly | Uses |
|---|---|---|---|
| `--duration-fast` | 0.15s | 0.15s, 150ms | 155 |
| `--duration-base` | 0.2s | 0.2s, 200ms | 271 |
| `--duration-slow` | 0.3s | 0.3s, 300ms | 45 |
| `--ease-standard` | ease | | 339 |
| `--ease-emphasized` | cubic-bezier(0.16, 1, 0.3, 1) | | 2 |

Phase 4 snaps the off-scale transition durations: 0.12s, 0.1s and 0.08s → `--duration-fast`;
0.25s and 180ms → `--duration-base`. Only **transition** durations are tokenised and guarded. Loop animations (spinners, shimmer,
pulse: ≥ 0.6 s) keep their literal duration beside their `@keyframes`. Reduced motion is already
global (`animations.css` `@media (prefers-reduced-motion: reduce)` sets every duration to
0.001 ms); tokens do not change under it. Easing keywords stay allowed raw. The two easing tokens
exist for new code and are not guarded.

### 3.9 Aliases (deprecated; D10)

Add a header to the existing "Compatibility aliases" section saying these names are deprecated
for core use. Map each to its canonical name for lane 6.1:

`--text-muted`, `--text-light`, `--color-text-secondary`, `--text-secondary-color`,
`--text-color-secondary`, `--secondary-color` → `--text-secondary`. `--text-primary`,
`--color-text` → `--text-color`. `--color-border` → `--border-color`. `--card-background`,
`--card-bg`, `--bg-primary`, `--color-surface` → `--surface-color`. `--bg-secondary`,
`--background-secondary`, `--color-bg-secondary` → `--surface-secondary`. `--hover-bg`,
`--bg-hover`, `--color-bg-hover`, `--card-hover` → `--hover-color`. `--color-primary`, `--accent`,
`--accent-color`, `--primary-border` → `--primary-color`. `--primary-dark`, `--color-primary-dark`,
`--primary-color-dark` → `--primary-on-surface`. `--danger-color` → `--error-color`. `--danger-bg`,
`--error-background`, `--error-light`, `--error-color-light` → `--error-bg`.
`--warning-background`, `--warning-light` → `--warning-bg`. `--success-color-light`,
`--color-success-bg` → `--success-bg`. `--color-text-tertiary` → `--text-tertiary`.

Only pure `var(--canonical)` aliases, or aliases with identical light **and** dark values, may be
collapsed in Phase 6 (pixel-identical). Check each one in `theme.css` before collapsing it.
`--bg-primary` also exists in `app.html` with a **different** value; see section 8, R9.

### 3.10 Contrast targets and how they are checked

Target: WCAG 2.2 AA in both themes.

| Criterion | Target | Applies to | Checked by |
|---|---|---|---|
| 1.4.3 text | ≥ 4.5:1 (≥ 3:1 only for `--font-size-2xl` headings at weight 600) | every `--*text*` token on every surface it is documented for, including tints **composited** over the surface | `src/styles/contrast-pairs.test.ts` (static) and axe `color-contrast` on scanned surfaces |
| 1.4.11 non-text | ≥ 3:1 | `--focus-ring-color`, `--control-border`, switch on/off track vs surface, solid fills that are the only boundary | `contrast-pairs.test.ts` |
| 2.4.7 / 2.4.13 focus | visible `:focus-visible` outline of 2 px `--focus-ring-color` with 2 px offset on every interactive primitive | Button, Toggle, FormField controls, `.btn*` | `ui-kit` fixture: keyboard-focus each control, screenshot; vitest asserts the class carries the rule |
| 2.5.8 target size | ≥ 24x24 CSS px | Toggle (44x24), `.btn-sm` (min-height 28 px), icon buttons (min 28x28) | `ui-kit` fixture and a vitest size assertion on the computed `min-height` rule in source |
| 1.4.1 use of colour | status never colour-only: Badge, Toggle and Banner carry text or icon | components | review checklist in each component lane |

`contrast-pairs.test.ts` design:
- It parses `theme.css` with a small resolver in `src/styles/themeTokens.ts`. The resolver
  extracts the `:root` and `[data-theme='dark']` blocks, resolves `var()` chains, parses hex,
  `rgb(a)` and `rgba(var(--x-rgb), α)`, and composites alpha over a named surface.
  `primary-contrast.test.ts` keeps its assertions and is migrated to import the resolver, so
  there is one parser, not two. Name the helper so knip's `project` glob does not report it as
  unused: it is imported by tests only, so either list it in `knip.json` `ignore`, or keep it
  next to the tests and import it from at least one `.test.ts`. Check `npm run deadcode` after.
- The pair table is data, one row per pair: `[fg, bg, composite?, min, themes, reason]`. Minimum
  rows:
  - text on surfaces: `text-color` and `primary-on-surface` on `surface-color`,
    `background-color`, `hover-color` and `surface-secondary`; `text-secondary` on
    `surface-color` and `background-color` only (4.76:1 / 4.55:1 light). On `hover-color` it is
    4.34:1 light, the documented near-miss, so secondary text on a hover or sunken surface uses
    `text-on-tint` (6.92:1). That rule goes into `styles/CLAUDE.md` and gets its own row.
  - status text on tints: `{error,warning,success,info}-text` on their own `-bg` composited over
    `surface-color`.
  - `text-on-tint` on each status `-bg`.
  - labels on solids: `text-on-solid` on `primary-color`, `primary-hover`,
    `{error,success,info}-solid` and `-solid-hover`; `text-on-warning` on `warning-solid` and
    `warning-solid-hover`.
  - non-text (3:1): `focus-ring-color` on `surface-color` and `background-color`;
    `control-border` on `surface-color` and `background-color`.
- Red first: the implementer runs it in an `ot-redcheck.` tree (root `CLAUDE.md`) with one
  token deliberately broken (e.g. `--error-solid: #ef4444`) and pastes the failing line into the
  lane's commit body.
- **Known current failures become rows in lane 0.2 only once the tokens they need exist.** The
  old classes (`.btn-danger` on `#ef4444`, `.btn-warning`) are caught by axe on the `ui-kit`
  fixture in lane 0.1. Those findings are recorded in `backend/tests/e2e/a11y-allowlist.txt` as
  `ui-kit::<theme>::color-contrast::<n>  # BACKLOG lane 2.0b` and deleted in lane 2.0b. That is a
  measured red before the fix.

### 3.11 Mapping table: most frequent literals → tokens

Class: **E** = identical value in both themes, so pixel-identical (Phase 1). **L** = identical in
light, dark changes (D1, Phase 3). **N** = near value, both themes change (Phase 3). **F** = a dead
`var(--token, literal)` fallback, stripped in lane 1a (pixel-identical). Counts are declarations;
"where" lists the heaviest files.

| Literal | Total | Role split | Target by role | Class | Heaviest files |
|---|---|---|---|---|---|
| `#ef4444` | 76 | text 39, bg 26, border 8 | 39 F; text → `--error-text`; fill with label → `--error-solid` (D3); dot/icon/border → `--error-color` | F / N / L | settings/ASRSettings 6, settings/CustomVocabularySettings 5, speakers/SpeakerClusterCard 5, Navbar 4, chat/ChatTraceNode 3 |
| `#dc2626` | 73 | text 38, bg 23, border 12 | 37 F; bg/border → `--error-solid` | F / **E** | upload/MediaRecordPanel 5, SpeakerClusterCard 4, tags/TagBulkSummary 4, Navbar 3, groups/GroupMemberList 3 |
| | | | text → `--error-text` | N | |
| `#2563eb` | 71 | bg 62, text 5, border 4 | bg/border → `--primary-color` (57 after the fallback strip) | **E** | SpeakerClusterCard 4, FileUploader 3, settings/LLMConfigModal 3, settings/QuarantinePanel 3 |
| | | | text → `--primary-on-surface` | N | |
| `#f59e0b` | 54 | text 23, bg 18, border 13 | 34 F; text → `--warning-text`; fill → `--warning-solid` + `--text-on-warning`; icon/border → `--warning-color` | F / N / L | speakers/ClusterMemberList 6, SpeakerClusterCard 6, SegmentSpeakerDropdown 4, transcript/SpeakerEditorPanel 4 |
| `#6b7280` | 53 | text 34, bg 8, border 7 | 32 F; text → `--text-secondary`; border → `--control-border` or `--border-hover` by role | F / N | settings/MediaSourcesSettings 6, transcript/TranscriptSegmentList 4, speakers/ClustersTab 3, routes/search 3 |
| `rgba(59,130,246,.2)` | 50 | shadow 31, dark-bg 8, border 6 | `rgba(var(--accent-rgb),.2)`; whole-value shadows → `--shadow-primary` | **E** | CommentSection 4, SelectiveReprocessModal 3, styles/form-elements.css 3 |
| `#3b82f6` | 49 | border 40 | 38 F; focus borders → `--focus-ring-color`; accent borders → `--info-color` | F / L | FileUploader 6, SelectiveReprocessModal 4, MediaSourcesSettings 4, SpeakerClusterCard 4 |
| `rgba(0,0,0,.1)` | 45 | shadow 38 | shadow tokens where the whole value matches (3.6) | **E** | form-elements.css 3, AISuggestionsDropdown 2, SpeakerStats 2 |
| `#10b981` | 40 | text 24, bg 11 | 17 F; text → `--success-text` (2.54:1 → 5.48:1); dot/icon → `--success-color` | F / N / L | Navbar 5, UserManagementTable 4, ASRSettings 4, speakers/SpeakerInboxItem 4 |
| `rgba(59,130,246,.1)` | 34 | bg 27 | `rgba(var(--accent-rgb),.1)` | **E** | CommentSection 3, CollectionsEditor 2, SelectiveReprocessModal 2 |
| `#fff` / `#ffffff` / `white` | 34 + 13 + named | text 223 (all spellings) | text → `--text-on-solid` | **E** | 103 files |
| | | bg 13 + named bg 32 | knob/thumb → `--switch-thumb` (#fff both, E); panel → `--surface-color` (L) | E / L | |
| `rgba(239,68,68,.2)` | 33 | shadow 13, border 12, dark-bg 7 | shadows → `--shadow-danger`; tints/borders → `rgba(var(--error-color-rgb),α)` | L | settings/LLMSettings 3, ConfirmationModal 2, Navbar 2 |
| `#e5e7eb` | 32 | border 23 | 30 F; → `--border-color` | F / L | SpeakerClusterCard 6, ClusterMemberList 4 |
| `rgba(59,130,246,.25)` | 24 | shadow 22 | `--shadow-primary-hover` / accent | **E** | form-elements.css 3, CommentSection 2, groups/GroupsOverview 2 |
| `#d97706` | 24 | text 14 | text → `--warning-text`; fill hover → `--warning-solid-hover` | N / E | ASRSettings 3, SpeakerClusterCard 2, form-elements.css 2 |
| `rgba(239,68,68,.1)` | 22 | bg 20 | `--error-bg` | L | UploadProgress 2, fileStatus/TasksGrid 2, notifications/NotificationShell 2 |
| `rgba(0,0,0,.05)` | 22 | bg 15 | hover wash → `--button-hover` | L | navbar/UserDropdown 4 |
| `#fbbf24` | 22 | **dark-scoped** text 20 | the base rule takes `--warning-text`, whose dark value is #fbbf24, and the dark rule is **deleted** | E in dark | settings/SystemStatisticsPanel 3, fileStatus/FileDetailModal 2 |
| `#b45309` | 21 | text 16 | `--warning-text` | L | settings/SearchSettings 4, settings/transcription/formControls.css 3 |
| `rgba(245,158,11,.12)` | 18 | bg 18 | `rgba(var(--warning-color-rgb),.12)` | L | ASRSettings 3, mfa/MfaEnrollment 2, settings/OIDCSettings 2 |
| `#16a34a` | 17 | text 13 | `--success-text` | N | BackupSettings 2, DirectorySyncSettings 2, MediaMirrorSettings 2, ui/CopyButton 2 |
| `#9ca3af` | 16 | text 14 | all 16 are fallbacks | F | |
| `#60a5fa` | 15 | dark-scoped text 12 | dark rule deleted where the base uses `--primary-on-surface` (#93c5fd, N) or `--info-color` (E) | N / E | |
| `#e0e0e0` | 15 | | all 15 are fallbacks | F | |
| `#1a1a1a`, `#111827` | 12, 11 | text | `--text-color` | N | |

Exact non-colour mappings applied in Phase 1 (whole-value or per length token, by property): the
tables in 3.3, 3.4, 3.5, 3.6 (exact column only), 3.7 (tier values only) and 3.8.

---

## 4. How components consume tokens, utilities, and the guard

### 4.1 Rules (go into `frontend/src/styles/CLAUDE.md`, about 30 lines, and a 5-line pointer in `frontend/CLAUDE.md` Theming)

1. A component's `<style>` uses role and scale tokens only: `var(--surface-color)`,
   `padding: var(--space-2) var(--space-3)`, `border-radius: var(--radius-md)`,
   `box-shadow: var(--shadow-sm)`, `transition: background-color var(--duration-base) ease`.
2. Tints: `rgba(var(--<family>-color-rgb), α)` or `rgba(var(--accent-rgb), α)`. Never an `rgba`
   with numeric channels.
3. No `var(--token, <literal>)` fallbacks. Every token is declared on `:root`, and a fallback
   only hides a typo (an undefined token with a fallback renders the literal in both themes).
4. Dark mode comes from the token. A component writes a `:global([data-theme='dark'])` override
   only when the role genuinely differs (for example, an element that is a surface in light and
   media-black in dark), with a one-line reason.
5. Logical properties only (`padding-inline-start`, `inset-inline-end`, `text-align: start`,
   `border-inline-start`, `margin-inline`). Centring with `left: 50%; transform:
   translateX(-50%)` is direction-neutral and stays allowed.
6. The existing rules stand:
   - `:global([data-theme='dark'])`, never `.dark`.
   - No colours on `button:<pseudo-class>` in global CSS (#746).
   - Fields are painted only by `form-elements.css` (`formFields.test.ts`).
   - Text is never `--primary-color` (`consistency.test.ts`).
   - No purple (`noPurple.test.ts`).
7. Canvas and inline-SVG colour attributes read tokens at runtime with
   `getComputedStyle(document.documentElement).getPropertyValue('--x')`, and re-read on theme
   change (`theme` store).

### 4.2 Utilities (minimal)

One new file, `frontend/src/styles/utilities.css`, imported in `routes/+layout.svelte` right after
`form-elements.css`. It holds exactly one class: `.sr-only` (the standard clip-rect
visually-hidden pattern). It replaces the 10 local `.sr-only`/`.visually-hidden` copies (Appendix
A), and `search/+page.svelte`'s and `ProcessingJobCard.svelte`'s `.visually-hidden` markup is
renamed to `.sr-only`. No layout or spacing utility classes: the codebase is component-scoped CSS,
and utility classes would be a second styling idiom.

### 4.3 Guard: `scripts/guards/check_style_literals.py` (D9)

A stdlib Python ratchet, modelled on `scripts/guards/check_suppressions.py` and using
`scripts/guards/_baseline.py` (same CLI: default check, `--tighten`, `--write-baseline --reason
TEXT [--force]`, `--root`, `--baseline`; exit 0 pass, 1 findings, 2 unusable input).

- **Scope:** tracked files from `git ls-files frontend/src`:
  - `*.svelte`: the `<style>` block contents, plus `style="..."` attributes and
    `style:prop="..."` directives with literal values.
  - `*.css`.
  - Excluded: `*.test.ts`, `frontend/src/styles/theme.css` (the token source) and custom-property
    declarations inside `:root` / `[data-theme='dark']` blocks of `frontend/src/styles/*.css`
    (component token sources: `--field-*`, `--slider-*`).
  - Comments are stripped first. An unreadable file exits 2.
- **Categories:** the key is `<category>::<path>`, the value is the occurrence count.

  | Category | Counts | Does not count |
  |---|---|---|
  | `colour` | hex, `rgb/rgba/hsl/hsla(` with a numeric first channel, named colours (`white`, `black`, `red`, ...) in any declaration, including custom-property definitions outside the token sources | `transparent`, `currentColor`, `inherit`, `initial`, `unset`, `none`, `rgba(var(...), α)` |
  | `fallback` | `var(--x, <anything>)` | |
  | `shadow` | `box-shadow`/`text-shadow` values other than `none`, `inherit`, or a value made only of `var(--...)` references | |
  | `radius` | a length other than `0` in `border-radius` / `border-*-*-radius` | |
  | `spacing` | a length token other than `0`, `1px`, `-1px`, `%`, `em`, `auto` in `padding*`, `margin*`, `gap`, `row-gap`, `column-gap` | |
  | `font-size` | any length | |
  | `font-weight` | numeric or `bold` | |
  | `z-index` | any integer other than `0`, `-1`, `auto` | |
  | `duration` | a time in `transition`/`transition-duration` | animation durations |
  | `physical` | `margin-left/right`, `padding-left/right`, `border-left/right(-*)`, `left`/`right` with a value other than `50%`, `text-align: left/right`, `float: left/right` | |
- **Baseline** `scripts/guards/baselines/style_literals.txt`: one `category::path<TAB>count<TAB>reason`
  per line, written once with `--write-baseline --reason "styling layer: pre-migration literal,
  burn-down planned in docs/design/frontend_styling_layer_plan.md"`. Permanent entries get
  their own reason:
  - `colour::frontend/src/lib/components/ClassificationBanner.svelte`: "regulatory CUI banner
    colours (same exemption as noPurple.test.ts)".
  - Plyr/VideoPlayer `physical` entries: "third-party player chrome is LTR by design", if that
    holds after lane 5.x review.
- **Each lane runs `--tighten`** and commits the shrunk baseline in the same commit (the guard
  fails on a value that fell without the baseline being lowered).
- **Hook** in `.pre-commit-config.yaml`, ratchet section, commit stage:
  `id: guard-style-literals`, `entry: python3 scripts/guards/check_style_literals.py`,
  `language: system`, `pass_filenames: false`,
  `files: ^(frontend/src/.*\.(svelte|css)|scripts/guards/.*)$`.
  `backend/tests/unit/test_precommit_stage_ci_parity.py` must stay green (commit-stage hooks are
  run by CI).
- **Unit test** `backend/tests/unit/test_guard_style_literals.py`, in the style of
  `test_guard_suppressions.py`. Every category needs a must-fire fixture and a must-stay-clean
  fixture. Clean fixtures: `rgba(var(--x-rgb), .2)`, `0`, `1px`, `transparent`, a literal inside
  a comment, a literal in `theme.css`, `left: 50%`. It also covers baseline shrink, stale entry,
  new entry and exit 2 on unreadable input. Write the test first and watch it fail against a stub.
- Run `python3 scripts/audit-tests.py backend/tests` to 0 open findings before handing over.

### 4.4 Convention tests to add or extend (vitest, `frontend/src/styles/`)

- `contrast-pairs.test.ts` (3.10).
- `token-parity.test.ts` (new):
  1. Every colour token on `:root` either has a dark declaration, resolves only through `var()`
     to tokens that do, or is listed in `THEME_INVARIANT` with a reason (`--primary-color`,
     `--primary-hover`, `--error-solid`, `--text-on-solid`, `--text-on-warning`, `--accent-rgb`
     while frozen, ...).
  2. No `--space-*`, `--radius-*`, `--font-size-*`, `--z-*` or `--duration-*` token is
     redeclared in the dark block.
  3. Every `--shadow-*` token is declared in both blocks.
  4. Every `var(--x)` used anywhere in `frontend/src` resolves to a declared token. This
     replaces the safety the fallbacks pretended to give. It would have caught the undefined
     `--color-error` found by this plan.
- Extend `consistency.test.ts`: no `var(--token, literal)` anywhere (it moves to zero in lane 1a;
  allowlist with staleness check, the file's existing pattern).

---

## 5. Shared components

General rules for every component:
- Legacy Svelte syntax (`export let`, slots, `createEventDispatcher` or callback props, matching
  neighbours). `{...$$restProps}` on the root interactive element (so `data-testid` and `aria-*`
  pass through).
- No hardcoded English: every string is a prop the caller translates.
- Tokens only, logical properties only, a colocated `*.test.ts`, and a catalog entry in
  `components/ui/CLAUDE.md`.
- Import path `$components/ui/<Name>.svelte`.
- **Keep the class names that tests select on** (counts: `btn-primary` e2e 6 / vitest 27,
  `modal-delete-button` e2e 5, `cancel-button` e2e 2 and vitest 2, `save-button` e2e 1,
  `toggle-input` vitest 4, `toggle-switch` vitest 1, `progress-fill` e2e 2 and vitest 3,
  `status-badge` e2e 4, `error-message` e2e 2). Or change the selector in the same lane and run
  that test.

### 5.1 Buttons: global classes are the styling API; `ui/Button.svelte` is the behaviour API

Global classes in `frontend/src/styles/form-elements.css` (the existing section "Settings Page
Buttons"):

| Class | Change |
|---|---|
| `.btn` | Base: inline-flex, gap `--space-1-5`, padding `--space-2` `--space-4`, `--radius-lg`, `--font-size-md`, medium weight; `:focus-visible` outline 2px `--focus-ring-color`, offset 2px. |
| `.btn-sm` | **Canonical small** (27 uses). Padding `--space-1` `--space-2-5`, `--font-size-xs`, min-height 28px. `.btn-small` (11 uses) is renamed in markup to `.btn-sm` and its rule deleted. No alias. |
| `.btn-lg` | New. The modal/footer size (today's `.modal-button`: padding 0.6rem 1.2rem, `--radius-xl`, 0.95rem, min-width 100px). Snapping its off-scale padding and size is Phase 4. |
| `.btn-primary`, `.btn-secondary`, `.btn-danger`, `.btn-warning` | Colours from tokens; danger and warning per D3. |
| `.btn-success` | New: `--success-solid`. |
| `.btn-ghost` | New (14 local copies): transparent, `--text-color`, hover `--button-hover`. |
| `.btn-link` | New (6): no padding or background, `--primary-on-surface`, underline on hover. |
| `.btn-danger-outline` | New (5): transparent, `--error-text`, 1px `--error-border`, hover `--error-bg`. |
| `.btn-icon` | New (21 `icon-btn/icon-button`): square, min 28x28, padding `--space-1-5`, ghost colours, combinable with `.btn-sm`. |

The `.modal-*-button` family stays only as long as callers use it. Lane 2.3c moves
`ConfirmationModal` and the other 23 `modal-button` markup uses to `btn btn-lg btn-<variant>`,
then deletes the family. BaseModal's footer normaliser already sizes any `<button>`.

`ui/Button.svelte` exists for duplicated **behaviour**, not looks. Use it where a file hand-rolls
busy or icon-only logic. Plain buttons keep `<button class="btn btn-primary">`.

```
props:   variant: 'primary'|'secondary'|'danger'|'warning'|'success'|'ghost'|'link'|'danger-outline' = 'secondary'
         size: 'sm'|'md'|'lg' = 'md'
         type: 'button'|'submit'|'reset' = 'button'     (never the browser default 'submit')
         disabled = false
         busy = false        -> aria-disabled="true" + aria-busy="true", click swallowed, focus kept
                                (the a0ca7755f pattern), ui/Spinner size="small" before the label
         iconOnly = false    -> adds .btn-icon; requires ariaLabel (dev console.error if missing)
         ariaLabel = ''      form = ''  title = ''
slots:   default (label), icon (leading; MenuIcon by convention)
events:  on:click (forwarded; not fired while busy or disabled)
renders: <button class="btn btn-{variant} {size==='sm'?'btn-sm':size==='lg'?'btn-lg':''}" ...>
tests:   variant/size classes; busy swallows click and keeps focus; type defaults to 'button';
         restProps reach the element
```

### 5.2 `ui/Toggle.svelte` (D5)

Built from `privacy/PrivacySwitchCard.svelte`'s switch (the only accessible one), which then
consumes the component itself.

```
props:   checked (bindable), id = auto-generated if empty, label = '' (visible text, optional)
         labelledby = '', describedby = '' (ids; for card layouts whose title lives elsewhere)
         disabled = false, busy = false (aria-disabled, change ignored), size: 'md' (only size)
         labelPosition: 'end'|'start' = 'end'
events:  on:change -> detail { checked }  (also native change bubbles through restProps-free root)
renders: <label class="switch"> <input type="checkbox" role="switch" class="switch-input" ...>
         <span class="switch-track" aria-hidden="true"></span> {label}</label>
CSS:     track 44x24, --radius-pill, off: --surface-color fill + 1px --control-border (D6), on:
         --primary-color; thumb 18x18 --switch-thumb (#fff both themes) with --shadow-thumb,
         positioned with inset-inline-start (3px -> 23px), so it mirrors in RTL; focus-visible
         outline on the track (2px --focus-ring-color); disabled/busy opacity .5
tokens:  --switch-thumb: #ffffff (scoped, theme-invariant, listed in THEME_INVARIANT)
tests:   role=switch, aria-checked follows checked, busy ignores change, keyboard Space toggles,
         RTL: computed inset-inline-start (source assertion like typography.test.ts)
```

Vitest selectors: four tests select `.toggle-input` and one `.toggle-switch`. Update them to
`[role="switch"]` in the lane that migrates their component.

### 5.3 `ui/FormField.svelte` + global `.form-field*` classes (D12)

The measured families of the 39 local `.form-group` rules:

| Family | Files | Today |
|---|---|---|
| A settings compact | ASRConfigModal, GroupCreateModal, GroupMappingForm, SettingsModal, UserProfileSettings, SearchSettings, DownloadSettings, GroupMappingTester | column, gap 0.375rem; label 0.8125rem/500/`--text-color` |
| B auth pages | routes/login, register, forgot-password, reset-password, accept-invite, verify-email | column, gap 0.5rem; label 0.9rem/500 |
| C watch/email modals | WatchSourceModal (27 groups), EmailConfigModal (16), AuthMailDesignation, WatchSourceEmailLinksModal, GlobalWatchSettingsForm | column, gap 4px (8px), label 0.85rem `--text-secondary` |
| D auth-config panels | LDAPSettings (14), OIDCSettings (16), SAMLSettings (16), PKISettings, ProxySettings, LocalAuthSettings, PasswordPolicySettings | block, margin-bottom 1rem, label block 0.875rem |
| E misc | LinkIdentityModal, AudioExtractionSettings, LanguageSettings, OrganizationContextSettings, PromptSettings, RedactionPolicySettings, UserManagementTable, ContentRedactionSettings, CollectionFormModal, LLMConfigModal, SessionSettings, SCIMTokenSettings, LLMBedrockFields | various |

Global classes (new section "Form field layout" in `form-elements.css`, zero-specificity
`:where()` like the field rules):
- `.form-field`: flex column, gap `--space-1-5`.
- `.form-field-label`: `--font-size-sm`, medium, `--text-color`.
- `.form-field-hint`: `--font-size-xs`, `--text-secondary`.
- `.form-field-error`: `--font-size-xs`, `--error-text`.
- `.form-field-lg`: gap `--space-2`, label `--font-size-md`.
- `.form-row`: flex wrap, gap `--space-4`; each `.form-field` inside gets `flex: 1 1 14rem`. This
  covers family D's side-by-side fields.

The vertical rhythm between fields belongs to the parent (`gap` on the form or section), never
to `margin-bottom` on the field.

```
props:   id (required; the control's id), label, hint = '', error = '', required = false,
         size: 'md'|'lg' = 'md'
slot:    default, with slot props { describedby: string, invalid: boolean }
         usage: <FormField id="ldap-host" label={$t(...)} hint={$t(...)} error={hostError} let:describedby let:invalid>
                  <input id="ldap-host" aria-describedby={describedby} aria-invalid={invalid} bind:value={host} />
                </FormField>
renders: <div class="form-field" class:form-field-lg> <label for={id} class="form-field-label">{label}
         {#if required}<span aria-hidden="true"> *</span>{/if}</label> <slot/> hint <p id="{id}-hint">,
         error <p id="{id}-error" class="form-field-error" role="alert"> (only when error)
required: the caller also sets `required`/`aria-required` on the control. No new i18n key: the
          visible asterisk is aria-hidden and the native required state is announced.
tests:   label for=id; describedby lists hint and error ids that exist; role=alert only with error
```

A checkbox or radio row (label wrapping the input) keeps its markup and uses `.form-field` only
for spacing.

### 5.4 `ui/LiveProgressCard.svelte` + `ui/ProgressBar.svelte` fixes

`ProgressBar` changes (lane 2.4a, no pixel change except the fallback strip already done in 1a):
- Add `role="progressbar"`, `aria-valuemin=0`, `aria-valuemax=100`, `aria-valuenow`
  (omitted when indeterminate) and an `ariaLabel` prop.
- Add `tone: 'primary'|'success'|'warning'|'error' = 'primary'`, replacing the free-form `color`
  prop. Callers passing `color` are migrated in the same lane. `grep` for them first: there are 6
  users.
- Fill `var(--primary-color)` (today `--accent-color`, an alias of it: identical) and track
  `var(--color-bg-tertiary)` (today `--bg-tertiary`, an alias of it: identical). Both fallbacks
  are already gone after lane 1a. No pixel change.

`LiveProgressCard`:

```
props:   title (translated string), state: 'starting'|'running'|'stopping' = 'running'
         done: number|null = null, total: number|null = null, percent: number|null = null (0-100; null = indeterminate)
         etaSeconds: number|null = null, remainingLabel = '' (translated, e.g. $t('upload.remaining'))
         message = '', stopLabel = '', stoppingLabel = '', onStop: (() => void)|null = null (no button if null)
renders: surface card (--surface-secondary bg, 1px --border-color, --radius-md, padding --space-3):
         header row [Spinner small + title | "done / total"], ProgressBar (determinate or indeterminate),
         details row [message | "<formatEta> <remainingLabel>"], actions row [Button variant=danger-outline
         size=sm busy={state==='stopping'}] ; aria-live="polite" region on the count/percent text
uses:    formatEta from $lib/utils/formatting.ts (finding F-3: add it there if Wave 6 has not;
         delete the 4 local copies; 1 shared unit test with the current outputs as golden values)
```

Migrates (lane 2.4b):
- `settings/ReindexLiveProgress.svelte`: becomes a 10-line adapter or is deleted, with
  `SearchSettings` using the card directly. Prefer deletion if it has one caller.
- `settings/EmbeddingMigrationSettings.svelte`, `settings/AutoLabelSettings.svelte`,
  `settings/SpeakerAttributeBulkPanel.svelte`.

Then (lane 2.4c) the local bars move to `ProgressBar` where the shape matches: `UploadManager`,
`UploadProgress`, `fileStatus/TasksGrid`, `speakers/ClustersTab`, `FileHeader`, `SearchSettings`.
`SystemStatisticsPanel`'s usage bars are **meters**, not progress: they keep their markup, get
`role="meter"` and switch to tokens.

### 5.5 Confirm dialogs: `ConfirmationModal` + `createConfirmDialog` adoption

`components/ConfirmationModal.svelte` (lane 2.5a):
- Replace `confirmButtonClass`/`cancelButtonClass` with `variant: 'primary'|'danger'|'warning' =
  'primary'`. Current caller values: `modal-delete-button` x20, `confirm-button delete-confirm` x5,
  `confirm-button` x3, `modal-warning-button` x3, `btn-danger` x1, `btn-primary` x1. Map them to
  danger 26, primary 4, warning 3.
- It renders `class="btn btn-lg btn-{variant}"` and `data-testid="confirm-dialog-confirm"` /
  `"confirm-dialog-cancel"`.
- Delete the component's local button CSS (it holds the D14 hover bug).
- Update the e2e selectors in the same lane: `test_tag_management.py:295`,
  `test_collection_management.py:289`, `test_gallery_actions.py:948` (`.modal-delete-button`
  becomes `[data-testid="confirm-dialog-confirm"]`). Run those three e2e files on the fresh stack.

`lib/confirmDialog.ts` (lane 2.5a):
- Change `request(title, message, onConfirm, options?: { confirmText?, onCancel?, variant? })`.
  The positional `confirmText`/`onCancel` go (5 callers, migrated in the same lane).
- Add a `variant` store.
- Apply F-10 (catch a rejected async callback; test with an `unhandledRejection` spy).

Adoption (lanes 2.5b-d). Each file gets one `createConfirmDialog()` and one
`<ConfirmationModal>`. Only one confirm is open at a time on a screen, so multi-dialog screens
collapse to one instance with per-request title, message and variant.
- 2.5b settings: ActiveSessionsPanel, ASRSettings, GroupMappingSettings, HuggingFaceTokenCard,
  LLMConfigModal, LLMSettings, localModels/ActivateModelDialog, OrphanedDataPanel (x3),
  PromptSettings, PyannoteCredentialForm, SCIMTokenSettings, SearchSettings,
  WatchSourcesSettings, watchSources/WatchSourceFilesModal, and **LockedAccountsPanel**
  (native `confirm()` x2 → dialog, F-9). F-9 needs a title key; reuse an existing
  `settings.lockedAccounts.*` title if one exists, otherwise add one in all 12 locales,
  translated, then run `npm run check:i18n`.
- 2.5c: FileHeader, FileUploader, groups/GroupDetailPanel, groups/GroupMemberList,
  sharing/CurrentSharesList, supportAccess/SupportAccessBanner, tags/TagDetailPanel.
- 2.5d (after their Wave 10 splits): `routes/files/[id]/+page.svelte`, `routes/speakers/+page.svelte`.

Stop if a dialog's confirm has extra content beyond title and message (a checkbox, a typed
confirmation): that is a `BaseModal` dialog, not a confirm. `PurgeConfirmDialog` is one. Leave it.

### 5.6 Smaller consolidations (lane 2.6)

- **`ui/Banner.svelte`** (new):
  - props `tone: 'info'|'success'|'warning'|'error'|'neutral'`, `title = ''`,
    `dismissible = false`, `dismissLabel = ''`, `live: 'off'|'polite'|'assertive' = 'off'`;
    events `on:dismiss`; slots default and `actions`.
  - Icon via `MenuIcon` by tone (no emoji). Tint `--{tone}-bg`, border-inline-start 3px
    `--{tone}-color`, text `--text-color`, title `--{tone}-text`.
  - Migrates the 6 local `.banner`s: SupportAccessBanner, PipelineOverview, MediaMirrorSettings,
    DirectorySyncSettings, SearchSettings, BackupSettings. It also takes the block-level uses
    among the 15 `.error-message`s.
  - `ConnectionStatusBanner` and `AuthMailNotice` keep their logic and may render a `Banner`
    inside.
- **Inline errors**: field-level ones use `.form-field-error`. The remaining `.error-message`
  rules go. The two e2e references (`error-message`) are checked before renaming.
- **Status badges**: the 8 local `.status-badge`s (FileDetailModal, TasksGrid, UserManagementTable,
  SystemStatisticsPanel, DirectorySyncSettings, RetentionSettings, MediaMirrorSettings,
  BackupSettings) move to `ui/Badge`. Add a `size: 'sm'|'md'` prop if needed.
  - `TasksGrid` was contrast-tuned in #972 and is scanned by the `status-badges` a11y fixture.
    Migrate it last and re-run that a11y surface.
  - `settings/StatusChip` (label + value) stays a separate component, restyled on the same status
    tokens.
  - The e2e `status-badge` selector (4 uses) is kept as an extra class on Badge's root, or the
    tests are updated.
- **Empty states**: the 7 local `.empty-state`s → `ui/EmptyState`, except `chat/ChatEmptyState`,
  a purpose-built chat greeting. Review it and keep it if so.
- **Spinners**: the local `.spinner`/`@keyframes spin` in `routes/reset-password`,
  `routes/forgot-password`, `transcript/SpeakerEditorPanel`, `transcript/TranscriptActionsBar` →
  `ui/Spinner`. `notifications/TaskStateGlyph` is a state glyph; review it.
- **`.sr-only`**: the global utility (4.2).

### 5.7 `routes/a11y-fixtures/ui-kit/+page.svelte` (D11; lane 0.1, grown by every component lane)

One page, sections in fixed order, no backend calls, deterministic content (no dates, no random).
Each section has a heading and `data-kit-section="<name>"`:
- buttons: every variant x size x {rest, disabled, busy}, plus icon-only.
- toggles: off, on, disabled, busy, with and without a label, label-start.
- form fields: md/lg x {plain, hint, error, required}; input, select and textarea.
- progress: determinate 0/42/100, indeterminate, and a LiveProgressCard in starting, running
  and stopping.
- badges: every variant.
- banners: every tone, with and without title and actions.
- status text on tints: one line per family.
- confirm dialog: the trigger buttons only. The tool opens the dialog in the capture step.

All labels come from existing locale keys (`common.save`, `common.cancel`, `common.delete`, ...) so
the `ar` capture shows real RTL text. Lane 0.1 creates the page with the **existing** primitives
and global classes, so the "before" state of each component lane is captured too. Add the page as
an axe surface `ui-kit` in `backend/tests/e2e/test_a11y.py` (both themes, and `ar` light),
following the `file-status-badges` pattern at `test_a11y.py:280`.

---

## 6. Visual-diff method

### 6.1 Stack

A fresh, seeded, isolated stack. The **lead** starts and stops it (repo plan Wave 10 rule):

```bash
./opentr.sh start dev --fresh styling --port-offset 200 --seed-benchmark --with-mock-llm
# wait until every seeded file has left "processing" (gallery shows them completed)
./opentr.sh stop --fresh styling        # when the wave is done; volumes are kept for reuse
```

Never capture against the shared live stack: library content changes the images (measured: 3.06%
on `chat_trace`, `scripts/e2e/update-visual-baselines.sh` header). Ports with offset N: frontend
5173+N, backend 5174+N, MinIO 5178+N (`backend/tests/e2e/stack_urls.py`). The offset is in
`.fresh/styling.offset`.

### 6.2 The A/B tool (built in lane 0.1)

Files:
- `backend/tests/e2e/_visual_capture.py` (new). It holds `_make_context`, `_stabilize`, `_login`
  and the `_MASKS` table **moved** out of `test_visual_regression.py`, which then imports them.
  One implementation (the reason `_visual_diff.py` already exists). Run
  `pytest backend/tests/e2e/test_visual_regression.py --collect-only` before and after the move:
  the same test ids.
- `backend/tests/e2e/styling_screens.py` (new). The screen manifest (6.3) as Python data:
  `Screen(id, path, auth: bool, steps: list[str], masks: list[str], full_page: bool)`.
- `backend/tests/e2e/styling_ab.py` (new; not `test_*`, so it is not collected). The capture and
  diff CLI.
- `scripts/e2e/styling-ab.sh` (new). The wrapper that builds the two frontends.
- `backend/tests/unit/test_styling_ab.py` (new). Unit tests for the pure parts: manifest ids
  unique and well-formed, the computed-style diff function on two hand-written snapshots, the
  verdict rules, and the heat-map writer on tiny arrays.

Wrapper flow, `scripts/e2e/styling-ab.sh --lane <id> --base <ref> [--after <ref>|WORKTREE]
[--screens a,b|all] [--variants en-light,en-dark,ar-light,ar-dark] --mode zero|intended
[--control a,b] --stack styling`:

1. **Refuse** unless `.fresh/<stack>.offset` exists and is non-zero, and the backend at 5174+N
   answers `/api/health`. Never touch the live stack.
2. Output directory `W=~/ot-cleanup-work/styling/ab/<lane>/` (not `/tmp`, which is cleaned
   mid-session on this host).
3. BEFORE tree: `git archive <base> frontend | tar -x -C $W/before`.
   AFTER tree: the same for `<ref>`. For `WORKTREE`, `rsync -a --exclude node_modules --exclude
   .svelte-kit frontend/ $W/after/frontend/`. That is a snapshot, so later edits by anyone do not
   leak in, and the shared checkout is never touched.
4. In each tree, symlink `frontend/node_modules` to the worktree's
   (`/mnt/nvm/repos/transcribe-app-wt-cleanup/frontend/node_modules`), refusing if
   `package-lock.json` differs between the trees, and copy `frontend/static/fonts/`, which is
   gitignored. `_stabilize` refuses to capture without Poppins.
5. Start two Vite dev servers with Node 26
   (`PATH=$HOME/.nvm/versions/node/v26.8.2/bin:$PATH`), one per tree, under `nice -n 19`:
   `VITE_API_PROXY_TARGET=http://localhost:$((5174+N)) VITE_MINIO_URL=http://localhost:$((5178+N))
   npx vite dev --port <P> --strictPort`, with P = 5291+N (before) and 5292+N (after). Refuse if
   either port is bound. `VITE_API_PROXY_TARGET` exists for exactly this ("a second dev server,
   another checkout, another port", `frontend/vite.config.ts`).
6. Run `nice -n 19 backend/venv/bin/python backend/tests/e2e/styling_ab.py --before
   http://localhost:P1 --after http://localhost:P2 --backend http://localhost:$((5174+N)) ...`.
   Both frontends share one backend, so the data is identical.
7. A `trap` stops **only the two PIDs the wrapper started** (`kill -TERM "$pid"`). It never kills
   by name (root `CLAUDE.md`: no `pkill`/`killall`/`kill -9`).

Capture, per screen x variant, A then B **interleaved** (so relative timestamps cannot drift
between them):
- New browser context: viewport 1280x800 (the visual suite's `VIEWPORT`),
  `reduced_motion="reduce"`. The init script sets `localStorage.theme` and `localStorage.locale`
  (`en` or `ar`; `stores/locale.ts` reads `locale` and sets `dir` before first paint).
- Log in as `admin@example.com` / `password` unless `auth=False`.
- `goto(path)`, run the steps, `_stabilize`, full-page PNG with the masks.
- Computed-style snapshot: `page.evaluate` walks every element outside the masks and records,
  per element keyed by DOM path (`tag:nth-of-type` chain), these properties plus the same for
  `::before`/`::after` when `content` is not `none`:
  - `color`, `background-color`, `border-{top,right,bottom,left}-color` and `-width`,
    `outline-color`, `box-shadow`
  - `border-*-radius`, `padding-*`, `margin-*`, `gap`
  - `font-size`, `font-weight`, `line-height`, `letter-spacing`
  - `z-index`, `opacity`, `transition-duration`, `inset-*`, `width`, `height`

Outputs in `W`:
- `before/`, `after/` and `diff/` PNGs, named `<screen>__<variant>.png`. The diff is the after
  image in greyscale with differing pixels (by `_visual_diff.CHANNEL_NOISE_THRESHOLD`) painted
  magenta.
- `summary.json`.
- A console table with screen, variant, pixel diff %, computed-diff count and verdict.
- For mode `intended`, `changes.txt`: computed diffs aggregated as
  `property: before -> after  (N elements; screens ...)`. **That aggregate goes into the lane's
  commit body: it is the explicit list of visual changes.**

Steps vocabulary (manifest strings, kept tiny): `click:<css>`, `hover:<css>`, `focus:<css>`
(keyboard focus via `locator.focus()` then a Tab/Shift+Tab round trip to trigger
`:focus-visible`), `wait:<css>`, `press:<key>`, `settings:<section-id>` (open the user menu →
Settings, then `.settings-sidebar .nav-item[data-section="<id>"]`), `tab:<id>` (`#tab-<id>`, the
id `ui/Tabs` already renders), `scroll:<css>`.

Lane 0.1 adds the one markup attribute the tool needs: `data-section={item.id}` on the
SettingsModal nav button (`components/SettingsModal.svelte`, the `.nav-item` loop). Today the
only selector is the translated label, which does not work in `ar`. This is no visual change.

### 6.3 Screen manifest (names are stable ids; the lanes refer to them)

Logged out (`auth=False`): `login`, `register`, `forgot-password`, `reset-password`
(`?token=invalid`), `verify-email` (`?token=invalid`), `accept-invite` (`?token=invalid`).

App:
- `gallery`, `gallery-select` (selection mode on), `gallery-filters` (filter sidebar open),
  `upload` (Add Media open).
- `file-detail` (newest completed file, the same query as the visual suite), `file-detail-editor`
  (speaker editor panel open), `file-detail-comments`.
- `search` (`/search?q=<seed term>`), `chat` (mock LLM; one canned exchange).
- `speakers-clusters`, `speakers-profiles`, `speakers-inbox`.
- `file-status`, `notifications` (bell panel open), `user-menu`, `about`.
- `confirm-delete`: select one gallery file → delete → dialog open. The step list ends on the
  open dialog. **Never confirm**: the cancel path only (e2e must not mutate data).
- `ui-kit` (full page; plus `ui-kit-focus`: the `focus:` steps over the buttons and switch
  sections), `status-badges`.

Settings, one screen per `settings:<id>` for each section the seeded admin can see:
- `profile`, `groups`, `processing-pipeline`, `transcription`, `speaker-identification`, `chat`,
  `content-redaction`, `llm-provider`, `ai-prompts`, `organization-context`, `auto-labeling`.
- `download`, `media-sources`, `watch-sources`, `recording`, `audio-extraction`.
- `admin-users`, `authentication`, `audit-logs`, `quarantine`, `takedown-notices`.
- `system-statistics`, `file-status`, `legal`, `data-management`.
- Sub-panels via `tab:`: `authentication` LDAP, OIDC, SAML, PKI, proxy, session, password policy,
  local; `data-management` retention, backup, data-integrity, search-indexing,
  embedding-migration; `transcription` provider, custom vocabulary; `speaker-identification`
  attributes, engine.

The lane 0.1 implementer reads the real tab ids from each settings component and records them in
the manifest. A section that is capability-gated off on the fresh stack is listed in the manifest
with `skip_reason`, not silently dropped.

Variants: `en-light`, `en-dark`, `ar-light`, `ar-dark`. All four always run; captures are cheap.

### 6.4 Acceptance

| Lane mode | Pass rule |
|---|---|
| `zero` (Phases 0, 1, 6 and the `en` variants of Phase 5) | For **every** screen x variant run: pixel diff ≤ 0.01% (`diff_fraction` ≤ 0.0001) **and** 0 computed-style differences. Anything else **stops the lane**: it was not a pixel-identical change. Report the `changes.txt` and the diff PNG to the lead; never widen the threshold. |
| `intended` (Phases 2 to 4, `ar` in Phase 5) | Every line of `changes.txt` is covered by the lane card's "Expected changes" or a named decision (D-number). An uncovered line fails the lane. The **control** screens (manifest screens the lane does not touch, at least `gallery`, `file-detail`, `login`, `settings:profile`) must pass the `zero` rule. The reviewer (Sonnet, or Opus where flagged) opens before, after and diff for every changed screen in all four variants before the lane is accepted. |

The existing visual suite's 0.5% tolerance (`DIFF_TOLERANCE`) is for cross-run baselines and is
too loose here: a 2 px padding change on one button is well under 0.5% of a full page. The
computed-style diff is what makes a "zero" claim falsifiable. Hover, active and focus states are
covered by the `ui-kit-focus` screen and the `hover:`/`focus:` steps, because a static capture
cannot see them.

### 6.5 Self-check and negative control (lane 0.1, then once per phase)

- `--selfcheck`: before = after = the same ref, all screens and variants. It must pass `zero`.
  This measures the noise floor. If any screen is non-zero, fix its masks or steps first. Never
  raise the threshold.
- `--negative-control`: after = same ref, with `.btn { padding-inline: calc(var(--space-4, 1rem)
  + 1px) }` injected through `add_style_tag`. It must **fail** `zero` on every screen that has a
  `.btn`, and `changes.txt` must list `padding-left`/`padding-right`. A tool that cannot fail is
  not evidence (root `CLAUDE.md`).
- Paste both one-line summaries into the lane 0.1 commit body.

### 6.6 Related suites

- `backend/tests/e2e/test_visual_regression.py` (committed baselines, 8 surfaces x 2 themes)
  will go red on intended changes. Re-capture once at the end of Phase 3 and once at the end with
  `./scripts/e2e/update-visual-baselines.sh --reason "styling layer phase N"`. That is the only
  supported path (it refuses the shared stack).
- `backend/tests/e2e/test_a11y.py`: run `-k <surface>` for every surface a lane touches, plus
  `ui-kit` in every component lane. No new allowlist entries, except lane 0.1's measured
  `BACKLOG lane 2.0b` contrast rows, which lane 2.0b deletes.
- `backend/tests/e2e/test_rtl_privacy.py`: run in every Phase 5 lane.
- The `docs-screenshots` skill (`.claude/skills/docs-screenshots/`, `browse.js`) is **not** the
  A/B method. It writes docs images from the live stack. Use it once at the end to refresh docs
  images that show restyled settings and dialogs.

---

## 7. Migration sequence

Every lane is one commit-sized unit that leaves the app working.

**Common verification**, run in this order under `nice -n 19`, one job at a time:
1. In `frontend/`: `npm run check`, then `npm run lint`.
2. `NODE_OPTIONS=--no-experimental-webstorage npx vitest run src/styles src/components/ui <the
   touched files' tests>`.
3. `python3 scripts/guards/check_style_literals.py --tighten`, then without `--tighten` (exit 0).
4. `cd frontend && npm run check:i18n` if any locale file changed.
5. `scripts/e2e/styling-ab.sh` per the card.
6. `test_a11y.py -k <surfaces>` on the fresh stack.

The lead runs the full `npm run test`, `npm run build` (bundle budget) and
`scripts/safe-precommit.sh run --all-files` (both stages) once per batch, on a quiet tree.

Commit discipline: `repo_cleanup_plan.md` §0.3 and §0.5. One writer commits; an implementer in its
own worktree may commit on its lane branch. Conventional commit messages:
- `refactor(styles): ...` for zero-diff lanes.
- `feat(ui): ...` for new primitives.
- `fix(a11y): ...` for contrast or RTL fixes.
- `test(styles): ...` and `build(guards): ...`.

Every commit body records:
- the re-measured counts before and after (section 2 scripts);
- the A/B summary line;
- for `intended` lanes, the `changes.txt` aggregate and the D-numbers applied.

User-visible lanes add a CHANGELOG `### Changed` line.

Card format: **Files** · **Change** · **Mode / expected changes** · **Screens** · **Stop if**.

### Phase 0: infrastructure (no visual change)

**0.1 A/B tool, ui-kit fixture, settings data attribute.**
- Files: `backend/tests/e2e/{_visual_capture.py,styling_screens.py,styling_ab.py}`,
  `backend/tests/e2e/test_visual_regression.py` (import the moved helpers),
  `backend/tests/unit/test_styling_ab.py`, `scripts/e2e/styling-ab.sh`,
  `frontend/src/routes/a11y-fixtures/ui-kit/+page.svelte`, `frontend/src/components/SettingsModal.svelte`
  (`data-section` only), `backend/tests/e2e/test_a11y.py` (`ui-kit` surface),
  `backend/tests/e2e/a11y-allowlist.txt` (measured `BACKLOG lane 2.0b` contrast rows, if axe
  reports them).
- Mode: `--selfcheck` passes; `--negative-control` fails as specified. For the `data-section`
  change: `zero` across all screens (base = the commit before).
- Stop if: the self-check cannot reach zero on some screen after masking (report the screen).

**0.2 Tokens.**
- Files: `frontend/src/styles/theme.css`:
  - add every **new** token in sections 3.2 to 3.8, with Phase 1 values (frozen `--accent-rgb`,
    shadow dark values equal to light);
  - `--field-radius: var(--radius-lg)` and `--field-focus-ring: var(--focus-ring)` in
    `form-elements.css` (identical values);
  - the alias deprecation header.
- Also `frontend/src/styles/themeTokens.ts` (resolver), `contrast-pairs.test.ts`,
  `token-parity.test.ts`, and `primary-contrast.test.ts` moved onto the resolver.
- Mode: `zero` (new tokens unused; the two `--field-*` redefinitions resolve to identical values).
- Red first: both new tests are shown failing in an `ot-redcheck.` tree with one token broken.
  `token-parity` rule 4 fails today on the undefined `--color-error` (2 sites). Fix it here: those
  2 sites currently render their fallback literal; repoint them to the token whose value equals
  that literal. If none exists, the fix moves to the sites' Phase 3 lane and the test gets a
  2-entry allowlist with that reason.
- Stop if: any `contrast-pairs` row fails for a token **this plan does not change**. That is an
  existing defect to report, not a reason to lower the bar.

**0.3 Guard.**
- Files: `scripts/guards/check_style_literals.py`,
  `scripts/guards/baselines/style_literals.txt`, `backend/tests/unit/test_guard_style_literals.py`,
  `.pre-commit-config.yaml` (hook).
- The test is written first and shown red. The baseline is written with `--write-baseline
  --reason ...`.
- Mode: no frontend change.

**0.4 Docs pointer.**
- Files: `frontend/src/styles/CLAUDE.md` (token families, the 7 consumption rules, the guard),
  and `frontend/CLAUDE.md` Theming (5 lines pointing there; the file is at 280/400 lines).
- Combine with 0.2 if preferred.

### Phase 1: pixel-identical codemods (mode `zero`, all screens, all four variants)

The codemod is a scratch tool at `~/ot-cleanup-work/styling/tools/codemod.py`, **not committed**.
Its behaviour is fully specified here, and the commit body records its sha256 and command line.

Rules:
- It edits only `<style>` blocks and `*.css` under `frontend/src`, never `theme.css`, and never
  inside comments.
- It parses declarations (`prop: value;`), including the last declaration of a block without a
  trailing `;`.
- It applies the exact tables only.
- `--dry-run` prints counts per rule and per file. The implementer compares them with section 2
  re-measured, then applies.
- It never touches a value containing `calc(`, `min(`, `max(`, `clamp(` or `var(` (except rule
  R1). It reports how many such values it skipped.

| Lane | Rule | Files | Notes |
|---|---|---|---|
| 1a | R1: `var(--T, <literal>)` → `var(--T)` when `--T` is declared on `:root` in `theme.css`, `form-elements.css` or `range-slider.css` (derive the set by parsing those `:root` blocks; do not reuse this plan's list) | 142 files, 621 colour + 34 length fallbacks | The 2 `--color-error` sites are excluded (handled in 0.2). Split into two commits if the diff is unwieldy: `components/speakers,upload,search` + `routes`, then the rest. |
| 1b | R2 role-aware exact colours: `#2563eb` bg/border → `--primary-color`; `#1d4ed8` bg/border → `--primary-hover`; `#dc2626` bg/border → `--error-solid`; `color: #fff|#ffffff|white` → `--text-on-solid`; `#94a3b8` → `--text-tertiary`. R3: `rgba(59,130,246,α)` → `rgba(var(--accent-rgb),α)`, `rgb(59,130,246)` → `rgb(var(--accent-rgb))` | measured after 1a: 57 + n + 23 + 223 + 2 + 188, about 110 files | The role is decided by the property (`color`/`fill`/`stroke` = text; `background*` = bg; `border*`/`outline*` = border). Applied inside dark-scoped rules too: identical values. |
| 1c | R4 shadows: whole-value matches of the section 3.6 "replaces exactly" strings → tokens. R8 transition durations. R9 z-index tier values (keep `!important`) | about 150 files | Normalise whitespace and `0.2` vs `.2` before matching. |
| 1d | R5 spacing (3.3 exact column; `px` → `rem` per D7) | split by directory: (i) `components/settings/**`, (ii) `components/{speakers,transcript,fileDetail,upload,gallery}/**`, (iii) everything else | Largest lane, about 4,200 tokens. Skip values with `calc()` etc. |
| 1e | R6 radius, R7 font-size and weight (exact columns) | whole tree, two commits (radius, then type) | `9999px` → `--radius-pill`: the A/B proves identity. If any screen differs, revert just that rule and report. |

After each lane: `check_style_literals.py --tighten` shrinks the baseline, and the A/B `zero` run
passes on all screens x 4 variants. Phase 1 runs **before Wave 10 opens** (D15) and right after a
master merge (repo plan 0b point 10), so its conflict window is small.

### Phase 2: shared components and global contrast fixes (mode `intended`)

| Lane | Files | Expected changes | Screens |
|---|---|---|---|
| 2.0a | `form-elements.css`: add `.btn-sm`, `.btn-lg`, `.btn-success`, `.btn-ghost`, `.btn-link`, `.btn-danger-outline`, `.btn-icon`; base `.btn` and variants on tokens. `ui/Button.svelte` + test. ui-kit buttons section | None on existing screens (the new classes are unused; on-token values equal today's after Phase 1). ui-kit: new rows only. | ui-kit, controls |
| 2.0b | `form-elements.css` + `theme.css`: D3 (danger and warning fills and hovers, `.modal-delete-button`/`.modal-warning-button` too), D4 (`button:focus-visible` and field focus use `--focus-ring-color`), D6 (`--field-border: var(--control-border)`). Delete the `BACKLOG lane 2.0b` a11y rows | Danger buttons #ef4444 → #dc2626 (hover → #b91c1c). Warning buttons get dark text. Focus outline in dark becomes #60a5fa. Field borders darken (#d1d5db → #838fa1 light; #475569 → #7c8aa0 dark). | **all** screens (global); Opus reviewer |
| 2.1a | `ui/Toggle.svelte` + test; `privacy/PrivacySwitchCard.svelte` consumes it | PrivacySwitchCard: off-track gains the `--control-border` outline. Otherwise none (it is the 44x24 reference). | `settings:content-redaction` / privacy surfaces, ui-kit |
| 2.1b | Toggles in `settings/{ASRConfigModal,ASRSettings,AudioExtractionSettings,AuditContentAccessToggle,AutoLabelSettings,BackupSettings,ChatSettings,ContentRedactionSettings,CustomVocabularySettings,DirectorySyncSettings,DownloadSettings}` | D5: sizes become 44x24, `role=switch`, RTL mirroring, off-track outline | their `settings:*` screens |
| 2.1c | Toggles in `settings/{EngineSettings,LLMConfigModal,LLMSettings,MediaMirrorSettings,OrganizationContextSettings,PromptSettings,RedactionPolicySettings,RetentionSettings,RetrySettings,SpeakerAttributeSettings}`, `settings/watchSources/WatchSourceCard`, `upload/UploadStepModel` | D5 | their screens + `upload` |
| 2.2a | `form-elements.css` form-field section; `ui/FormField.svelte` + test; auth routes (family B) with `size="lg"` | Auth labels 0.9rem → 0.875rem (D12); gaps unchanged | login, register, forgot-password, reset-password, verify-email, accept-invite |
| 2.2b | Family D: LDAP/OIDC/SAML/PKI/Proxy/LocalAuth/PasswordPolicySettings (**after** any Wave 10 work on them) | Field rhythm from `margin-bottom: 1rem` to the parent's `gap: --space-4` (same distance); label size unchanged | `settings:authentication` tabs |
| 2.2c | Families A, C, E (excluding `UserManagementTable` until its split) | C: labels `--text-secondary` 13.6px → `--text-color` 13px; gap 4px → 6px (D12) | the owning settings screens, `upload`, `confirm-delete` |
| 2.3a-c | Local button CSS deleted in favour of global classes, in 3 lanes over the 40 files (Appendix A): (a) `components/settings/**`, (b) gallery/tags/groups/collections/sharing/Navbar/CommentSection/SpeakerMerge/SelectiveReprocessModal/FileHeader/TranscriptSegmentList, (c) `routes/login`, ConfirmationModal family + the 23 `.modal-button` markup uses; `btn-small` → `btn-sm` (11) | Each local variant that differed from the global one is listed by the computed diff. Typical: radius 6px → 8px, padding 0.4rem → 0.5rem, hover shadow. Keep `.cancel-button`/`.save-button` class names on transcript segment edit buttons (e2e) or update `test_file_detail_transcript.py` | screens per file |
| 2.4a | `ui/ProgressBar.svelte` (role, `tone`, tokens) + test | none (aliases resolve to identical values; the ARIA additions are not visual) | ui-kit, `upload`, `file-status` |
| 2.4b | `ui/LiveProgressCard.svelte` + test; `formatEta` into `$lib/utils/formatting.ts`; the 4 cards (5.4) | Card chrome unified (padding 0.75rem, `--radius-md`, `--surface-secondary`); stop button becomes `.btn-danger-outline .btn-sm` | `settings:data-management` search-indexing / embedding-migration, `settings:auto-labeling`, `settings:speaker-identification` attributes; ui-kit (states) |
| 2.4c | Local progress bars → ProgressBar (5.4 list); `SystemStatisticsPanel` meters | Track height and radius unified (6px, `--radius-pill`) | `upload`, `file-status`, `speakers-clusters`, `file-detail`, `settings:system-statistics` |
| 2.5a | `ConfirmationModal` variant API, `lib/confirmDialog.ts` options + F-10, the 5 existing callers, 3 e2e selector updates | D14 hover; confirm buttons take `.btn-lg` exactly (modal-button values) | `confirm-delete`, ui-kit; e2e: tag management, collection management, gallery actions |
| 2.5b-d | Adoption per 5.5 | none (same dialog); LockedAccountsPanel: native browser dialog → app dialog | the owning screens |
| 2.6 | Banner, inline errors, status badges, empty states, spinners, `.sr-only` (5.6) | Banners gain the inline-start accent bar; badges unify radius and padding | owning screens; `status-badges` a11y surface for TasksGrid |

### Phase 3: semantic colour lanes, dark parity and AA (mode `intended`, D1)

**3.0 Token flips.**
- D2: `--accent-rgb: var(--primary-color-rgb)` in both blocks; then the 37
  `rgba(var(--primary-color-rgb),…)` shadow twins → `--shadow-primary(-hover)`.
- D13: dark shadow values per 3.6; `--card-shadow: var(--shadow-sm)`.
- All screens. Expected: blue tints shift by ΔE of about 1 to 2 (both themes); dark-mode shadows
  deepen.
- Opus reviewer.

Then one lane per surface. In each one:
- every remaining colour literal goes to its role token per 3.11 (role by property, `-text` for
  any text);
- every `:global([data-theme='dark'])` rule made redundant by the token is **deleted** (the A/B
  `en-dark` capture proves equivalence or lists the change);
- `--tighten` the guard.

Order (heaviest first, Wave 10 files after their split):

| Lane | Files (literal count) | Screens |
|---|---|---|
| 3.1 speakers | speakers/SpeakerClusterCard (51), ClusterMemberList (29), SpeakerInboxItem (24), ClustersTab, ProfilesTab, InboxTab, SegmentSpeakerDropdown (21) | speakers-*, file-detail-editor |
| 3.2 navbar + gallery | Navbar (45), navbar/UserDropdown, gallery/GallerySelectionActions (22), VirtualGrid (20), VirtualList, BulkTagModal, QuarantineModal, ui/SortDropdown (20) | gallery*, user-menu, notifications |
| 3.3 upload | upload/upload-shared.css (32), FileUploader (28), upload/MediaRecordPanel (21), UploadManager, UploadProgress | upload |
| 3.4 transcript | transcript/SpeakerEditorPanel (31), TranscriptSegmentList (30), CommentSection (25), FileHeader, VideoPlayer, PlyrMiniPlayer, FloatingPreviewPlayer | file-detail* |
| 3.5 settings I | MediaSourcesSettings (37), ASRSettings (35), SystemStatisticsPanel (33), PromptSettings (23), LLMSettings (20), LLMConfigModal | their settings screens |
| 3.6 settings II | the rest of `components/settings/**` (Backup, MediaMirror, DirectorySync, Retention, Search, CustomVocabulary, auth panels, watchSources, transcription/formControls.css, ...) | their settings screens |
| 3.7 search + chat | routes/search (28), search/SearchAutocomplete, chat/** | search, chat |
| 3.8 misc | SelectiveReprocessModal (30), fileStatus/**, notifications/**, tags/**, collections/**, groups/**, sharing/**, mfa/**, routes/+error, auth routes, `styles/form-elements.css` (28), `styles/tables.css`, `styles/search.css` | owning screens |
| 3.9 non-stylesheet colours | `WaveformPlayer` (canvas reads tokens via `getComputedStyle`, re-reads on theme change), `Toast`, `AISuggestionsDropdown`, `LoginBanner`, `ThemeToggle` SVG attributes → `currentColor` + CSS; `ClassificationBanner` stays (regulatory) | file-detail, login, gallery |
| 3.10 z-index | the non-tier values (3.7), in every file that has one | ui-kit-focus, menu-in-modal steps (`settings:*` + open a SearchableSelect), `notifications` over a modal, fullscreen player (manual check, noted in the commit) |
| 3.11 admin users | `UserManagementTable` (55) and its Wave 10 children | settings:admin-users |
| 3.12 file detail and speakers routes | `routes/files/[id]/+page.svelte`, `routes/speakers/+page.svelte` (after Wave 10) | file-detail*, speakers-* |

Stop if: a literal has no fitting role (report it with its context; do not invent a token
mid-lane; the lead adds it to section 3.2 first).

### Phase 4: snap off-scale values (mode `intended`, D8)

Same surface grouping as Phase 3 (4.1 to 4.12), applying the snap tables in 3.3, 3.4, 3.5 and the
nearest-shadow rule (3.6). Separate from Phase 3 so each diff shows one kind of change.
Expected: sub-pixel to 1 px spacing and type shifts. Possible line-wrap changes are flagged
per screen.

Stop if: a snap changes a line wrap or truncation in a way that hides content (report;
that value may need the next step instead).

### Phase 5: RTL logical properties (en `zero`, ar `intended`)

The same surface grouping (5.1 to 5.12) converts the 400 physical declarations:
- `margin/padding/border-left|right` → `-inline-start|end`
- `left|right` → `inset-inline-start|end`
- `text-align: left|right` → `start|end`
- `float` → `inline-start|end`

Centring patterns (`left: 50%` + `translateX(-50%)`) stay. `translateX` used for direction (switch
thumbs, slide-ins) gets an `[dir='rtl']` counterpart or moves to `inset-inline-*`.
- `en-light` and `en-dark` must pass `zero` (logical equals physical in LTR).
- `ar-*` changes are expected and reviewed. Run `test_rtl_privacy.py`.
- Third-party player chrome (Plyr) is checked in `ar`; if it is meant to stay LTR, its `physical`
  baseline entries get that reason instead of a conversion.

### Phase 6: close-out

**6.1 Alias collapse** (mode `zero`). Core references to deprecated aliases → canonical names
(3.9), only where the values are identical. Alias **definitions** stay (D10). Add a guard
category `alias` (or a `token-parity` rule) so core cannot reintroduce them.

**6.2 Tighten.** Every guard category at zero becomes a hard zero: remove it from the baseline, so
any occurrence is new debt. The remaining permanent entries keep their reasons.

**6.3 Docs.**
- `components/ui/CLAUDE.md`: the catalog entries for Button, Toggle, FormField, LiveProgressCard
  and Banner.
- `frontend/src/styles/CLAUDE.md`: final token reference.
- Update the claim in `components/ui/CLAUDE.md` "Buttons reuse the global classes" with the
  `.btn-*` list.
- Refresh docs-site screenshots (`docs-screenshots` skill) for restyled settings and dialogs.
- Re-capture the visual baselines (6.6).
- Add a CHANGELOG `### Changed` summary.

**6.4 Optional (cohesion):** move `theme.css`'s global element rules (body, links, dark tables,
`.theme-toggle*`) into `styles/base.css` and `ThemeToggle.svelte`. Keep the import order in
`+layout.svelte` and run `zero` mode. This is only if the owner wants `theme.css` to be tokens only.
It is not needed for the rest.

---

## 8. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | A codemod or migration lane runs while another writer edits the same checkout. Pre-commit stashes the whole tree (#434), and a test run against a mutating tree is meaningless. | Root `CLAUDE.md` rules. One writer per checkout, or own worktree. The A/B tool reads `git archive`/rsync snapshots, never the live tree. Confirm zero writers before any A/B run used as evidence. |
| R2 | Every `.css`/`.svelte` edit hot-reloads the dev frontend, and `.py` edits under `backend/app` restart the API (and dispatch `search_index_maintenance`). | This plan edits no `backend/app` code. The A/B servers are separate host Vite processes on their own ports and are unaffected by the dev stack's reloads. |
| R3 | Scoped versus global specificity. Deleting a local button rule hands the element to the global `.btn-*` (0,1,0) and the bare `button` rules. A leftover local rule (0,2,0) still wins. | The computed-style diff lists every resulting change. Never add colours to `button:<pseudo>` globally (#746). New global rules follow the existing patterns (`.btn` plain, form fields `:where()`). |
| R4 | e2e and vitest selectors depend on class names (5, counts listed). | Keep the class or change the selector in the same lane and run that test file. |
| R5 | The managed-edition overlay (`$lib/cloud`, replaced at build) may use alias tokens or the `.btn-*`/`.modal-*-button` classes. | D10 keeps the alias definitions. Before deleting `.modal-*-button` in 2.3c, grep the overlay repo if available. Otherwise keep the rule block with a comment naming the overlay, and ask the owner. |
| R6 | Stripping fallbacks matters only if a component renders where `theme.css` is not loaded. Inside the SPA, the root layout loads it for every route, including `+error`. | The A/B covers `+error` via `reset-password` and `verify-email` with invalid tokens. Anything rendered outside the SPA (none known) would need its own check. |
| R7 | Bundle budget: a new primitive imported by root-closure components (Navbar) grows the gated closure (136 KiB gzip). | Button and Toggle are small. Check the printed headroom at batch end. If a lane drives it `LOW`, that lane uses the classes, not the component, in root-closure files. |
| R8 | Wave 10 splits and master merges conflict with Phase 1's wide codemods. | D15 sequencing. Bring master in right before Phase 1. Phase 1 lanes are each mechanical and re-runnable: on conflict, drop the lane commit and re-run the codemod on the merged tree. |
| R9 | `app.html` declares `--bg-primary` (#f8fafc light / #0f172a dark) for the pre-paint frame. After load, `theme.css` redefines `--bg-primary` as `var(--surface-color)` (#fff / #1e293b), so `html`'s background changes after hydration. Only overscroll areas show it. | Lane 0.2 renames the `app.html` boot variables to `--boot-bg`, `--boot-text`, `--boot-surface`. Listed change: the overscroll background stays `--background-color`. Add a test that the boot values equal `theme.css`'s `--background-color`, `--text-color` and `--surface-color` in both themes. |
| R10 | Captures miss states (hover, focus, open menus, live progress). | The ui-kit fixture, the `hover:`/`focus:`/`click:` steps, and the computed-style diff on everything that is rendered. |
| R11 | `rem` conversion (D7) changes layout for users with a non-default browser font size: more space, more wrapping. | Intended (WCAG 1.4.4). Also spot-check `login`, `gallery` and `settings:profile` at a 20 px root in lane 1d: one manual capture with `page.add_style_tag("html{font-size:20px}")`, nothing breaking. |
| R12 | The guard's regex parser miscounts exotic CSS (nested `@media`, `@supports`, `:global(...)` blocks). | Unit-test fixtures for each construct. Exit 2 on unparseable input. The measurement scripts here already handle one `@media` nesting level. Compare their totals with the guard's first baseline: they must agree within the documented scope differences. |
| R13 | Long-lived lanes touch locale files (F-9 key), and all 12 locales are a shared conflict surface. | Only lane 2.5b touches locales, and only if no existing key fits. Run `npm run check:i18n` explicitly: the pre-commit hook does not fire on JSON-only commits. |

---

## 9. Out of scope

- A Svelte 5 runes migration (0 files use runes; a separate decision).
- A component library or CSS framework. None is added.
- The docs-site (`docs-site/`) styling.
- Redesigning layouts. Every change is a token, a consolidation or a snap listed above.
- The theme toggle's own redesign, chart and data-visualisation palettes (`speakerColors.ts` is
  data), and the regulatory classification banner colours.
- New features in the consolidated components beyond what their current copies do.

---

## Appendix A: file lists (measured 2026-11-01; re-derive with the `rg` lines)

**Local button classes (40):** `rg -l '\.(btn-primary|btn-secondary|btn-danger)\s*[{,:]|\.(cancel|save|submit|delete|danger)-(button|btn)\s*[,{:]' -g '*.svelte' frontend/src`
CommentSection, ConfirmationModal, FileHeader, gallery/BulkTagModal, gallery/GallerySelectionActions,
gallery/QuarantineModal, groups/GroupCreateModal, Navbar, SelectiveReprocessModal,
settings/{ActiveSessionsPanel, AudioExtractionSettings, audit/AuditLogFilters, AuthConfigAuditPanel,
BackupSettings, CacheSettings, DirectorySyncSettings, DownloadSettings, EngineSettings, LDAPSettings,
LLMConfigModal, MediaMirrorSettings, MediaSourcesSettings, OIDCSettings, OrganizationContextSettings,
OrgLegalHoldsPanel, PyannoteCredentialForm, RetentionSettings, RetrySettings, SearchSettings,
SpeakerAttributeSettings, SystemStatisticsPanel, UserProfileSettings}, SettingsModal, SpeakerMerge,
tags/{TagBulkSummary, TagDetailPanel, TagManagerModal}, transcript/TranscriptSegmentList,
UserManagementTable, routes/login.

**Toggles (23 + reference):** `rg -l '\.(toggle|switch|toggle-switch|slider|toggle-slider|switch-slider)\s*[{,]' -g '*.svelte' frontend/src`
The list is in lanes 2.1b and 2.1c; the reference is privacy/PrivacySwitchCard.

**Form groups (39):** `rg -l '\.form-group\s*[{,]' -g '*.svelte' frontend/src`. The families are
in 5.3.

**Hand-rolled confirm dialogs (23):** `comm -23 <(rg -l '<ConfirmationModal' -g '*.svelte' frontend/src | sort) <(rg -l 'createConfirmDialog' -g '*.svelte' frontend/src | sort)`,
minus `ConfirmationModalTestHost`. The list is in 5.5.

**Local progress bars (9):** UploadManager, UploadProgress, fileStatus/TasksGrid, speakers/ClustersTab,
FileHeader, ui/ProgressBar, settings/ReindexLiveProgress, settings/SystemStatisticsPanel,
settings/SearchSettings.

**`.error-message`/`.error-text` (15):** routes/{verify-email, forgot-password, reset-password,
files/[id], accept-invite}, UserFileStatus, tags/TagManagerModal, UploadProgress, VideoPlayer,
fileStatus/TasksGrid, chat/ChatMessage, settings/{FileStatusSettings, asr/CloudProviderPolicy,
SpeakerAttributeSettings, localModels/LocalModelLibrary}.

**`.status-badge` (8), `.empty-state` (7), `.banner` (6), `.spinner` (5):** listed in 5.6.

**`.sr-only`/`.visually-hidden` (10):** FileDetailSkeleton, tags/TagList, gallery/FileListSkeleton,
tags/TagManagerModal, chat/ChatMessage, chat/ChatStatusIndicator, supportAccess/SupportAccessBanner,
settings/IssueRow, routes/search, notifications/ProcessingJobCard.

**Fallback strip (142 files):** `rg -c 'var\(--[\w-]+\s*,\s*(#[0-9a-fA-F]{3,8}|rgba?\(\s*\d)' frontend/src -g '*.{svelte,css}' -g '!theme.css'`
Heaviest: SpeakerClusterCard 30, ClusterMemberList 29, SpeakerInboxItem 27, upload-shared.css 20,
ClustersTab 16, routes/search 15, ProfilesTab 15, MediaSourcesSettings 15, SegmentSpeakerDropdown 15.

**`rgba(59,130,246,α)` (67 files):** heaviest CommentSection 11, FileUploader 11,
SelectiveReprocessModal 11, TranscriptSegmentList 9, ShareCollectionModal 8, upload-shared.css 8,
VirtualList 7.

## Appendix B: active lanes at the time of writing

`git worktree list` on 2026-11-01 showed `cleanup/af-frontend` (4 files under `frontend/src/lib`,
no stylesheets) and `cleanup/b15-users` (clean, at HEAD; expected to split
`UserManagementTable`). Re-check before Phase 1, and hold any file another lane owns.
