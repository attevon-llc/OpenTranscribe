# src/styles — global stylesheets

Loaded once by `routes/+layout.svelte`: `theme.css` (tokens, light on `:root`, dark on
`[data-theme='dark']`), `form-elements.css`, `range-slider.css`, plus `animations.css`, `tables.css`,
`search.css`. Tokens and their rules are in `frontend/CLAUDE.md` (Theming, Type and weight).

## Form fields (`form-elements.css`) — one source of truth

Every text-like `<input>` (text, email, password, search, number, url, tel, date, time, ...),
`<textarea>` and `<select>` is styled here with no class: 8px radius, `--field-border`, `--field-bg`,
0.875rem / 1.25rem text, 0.5rem x 0.75rem padding (38px tall), brand-blue focus ring
(`--field-focus-ring`), `--text-muted` placeholder, 0.6 opacity disabled, `aria-invalid="true"`
error state. `.field-sm` is the compact size for toolbars and dense tables.

- **Scoped, not global.** The rule is `:where(input:not([type=checkbox], [type=radio], [type=range],
[type=file], [type=color], [type=hidden], [type=image], [type=button], [type=submit],
[type=reset]), textarea, select)`. The old bare `input { width: 100% }` stretched checkboxes and sliders;
  do not widen the selector back.
- **Zero specificity on purpose** (`:where()` throughout): a component's deliberate exception still wins with
  one class. The flip side is the rule: a component sets layout (width, flex, margin) and, where it must, size
  (padding, font-size). It does **not** set a field's border, radius, background, shadow or outline —
  `formFields.test.ts` fails on it. A field embedded in a wrapper that draws the box uses `border: none`
  and the wrapper uses `--field-*` tokens.
- **Select is platform-agnostic**: `appearance: none` (both prefixes), one chevron as `--field-chevron`
  (light/dark variants) on the inline-end side, mirrored in RTL by `[dir='rtl']`, `padding-inline-end`
  clearing it, `::-ms-expand` hidden, `option` colours set. `color-scheme: light|dark` on `:root` /
  `[data-theme='dark']` makes the browser's own popup list, date picker, scrollbars and autofill follow the app
  theme (Safari ignores option colours and relies on it). `font-family: inherit` everywhere — never the OS
  control font. A `background` shorthand on a select deletes the chevron; never set one.
- **macOS/Windows differences removed**: `::-webkit-search-*` glyphs hidden (SearchBar draws its own clear
  button), date/time inner value height and padding normalised, native calendar indicator softened, WebKit
  autofill tint overridden.
- A `<select>` cannot show rich options; use `SearchableSelect` where it must (no new component library).

## Sliders (`range-slider.css`)

See `components/ui/CLAUDE.md` (RangeSlider): rounded track, blue fill to the thumb centre, oval white-ringed
thumb, identical for WebKit and Gecko, and `slider-dual` for the gallery duration slider. Thumb rules outside
this file fail `rangeSliderStructure.test.ts`.

## Design tokens and the style-literal guard

Method of record: `docs/design/frontend_styling_layer_plan.md`. All tokens live in `theme.css`; component-level
tokens (`--field-*`, `--slider-*`) stay beside their stylesheet and are defined from role tokens, never literals.

Families: colour roles (light on `:root`, dark on `[data-theme='dark']`; status families `error|warning|success|info`
with `-color`, `-solid`, `-text`, `-bg`, `-border`), `--space-*` (rem, 4px base), `--radius-*` (px), `--font-size-*`,
`--shadow-*` (both themes), `--duration-*`, `--z-*`. Search hits have two roles: a FILL mixed down from
`--highlight-search` / `--highlight-current` (`color-mix(in srgb, var(--highlight-search) N%, transparent)`, N% of the
token's alpha; text sits on it) and a RING (border, box-shadow) in `--highlight-search-ring` / `--highlight-current-ring`,
which reach 3:1 against the surface (SC 1.4.11). Never draw a ring with a fill token or fill with a ring token:
`contrast-pairs.test.ts` reads every mark and ring in `search.css` and SegmentSpeakerDropdown and checks both roles.
An overlay on a picture or avatar uses `--media-scrim` (not `--modal-backdrop`).
A tooltip chip uses `--tooltip-bg` / `--tooltip-text`; code highlighting uses the `--syntax-*` palette (a code palette
is its own role, never a UI colour); a QR code sits on `--qr-surface`, white in both themes on purpose. Scales are theme-independent and are not redeclared in dark
(`token-parity.test.ts`); `contrast-pairs.test.ts` checks AA for every text and control pair.

Rules for a component `<style>`:

1. Role and scale tokens only (`padding: var(--space-2) var(--space-3)`, `border-radius: var(--radius-md)`).
2. Tints are `rgba(var(--<family>-color-rgb), α)`, never numeric channels.
3. No `var(--token, literal)` fallbacks: every token is declared on `:root`, a fallback only hides a typo.
4. Dark mode comes from the token; a `:global([data-theme='dark'])` override needs a one-line reason.
5. Logical properties (`padding-inline-start`, `inset-inline-end`, `text-align: start`); `left: 50%` centring is fine.
6. Existing rules stand: no `.dark`, no colours on `button:<pseudo>` (#746), fields painted only by
   `form-elements.css`, text never `--primary-color`, no purple.
7. Canvas and inline-SVG colours read tokens at runtime via `getComputedStyle(document.documentElement)`.

Guard: `python3 scripts/guards/check_style_literals.py` (baseline `scripts/guards/baselines/style_literals.txt`) counts
colour, fallback, shadow, radius, spacing, font-size, font-weight, z-index, duration and physical-direction literals
per file. Counts may only fall; run with `--tighten` in the same commit that removes literals.
Screenshot A/B for any restyle: `scripts/e2e/styling-ab.sh` against a `--fresh` stack (never the live one), and the
`/a11y-fixtures/ui-kit` page shows every shared component in every state.

## Stylelint

`npm run lint:css` (see `frontend/CLAUDE.md`, Commands) lints this folder and every component `<style>`.
Repo conventions it is configured around, each with its reason in `frontend/stylelint.config.mjs`: legacy
`rgba(var(--x-rgb), α)` tints (the `-rgb` tokens are comma lists), `(max-width: …)` media queries, hand-written
`-webkit-` prefixes (dev has no prefixer), six-digit hex as written, BEM `__element` / `--modifier` classes, and
`:global(...)`. Class names a library emits (`.rangeSlider`, `.hljs-built_in`) carry a reasoned
`stylelint-disable`. `no-descending-specificity` debt is baselined per file; fix a file with a `styling-ab.sh` A/B
and `--tighten` the baseline in the same commit.

## Shared blocks (de-duplicated, issue: frontend duplicate-code ratchet)

- `auth.css` — the card/header/form/button/link base of the unauthenticated pages (login, register,
  forgot/reset password, accept invite, verify email); each page imports `$styles/auth.css` and restates
  only what differs. `utilities.css` also holds `.section-toggle` (+ `.section-toggle-heading`), `.text-btn`,
  `.pill-chip`, `.copy-button-header` and the `.filter-rail` shell.
- `sourceTree.ts` — `walkSource` / `rel` / `SRC` for the style-source tests; do not hand-roll another `walk()`.
