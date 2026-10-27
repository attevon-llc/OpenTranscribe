# src/styles — global stylesheets

Loaded once by `routes/+layout.svelte`: `theme.css` (tokens, light on `:root`, dark on
`[data-theme='dark']`), `form-elements.css`, `range-slider.css`, plus `animations.css`, `tables.css`,
`search.css`, `support-access.css`. Tokens and their rules are in `frontend/CLAUDE.md` (Theming, Type and weight).

## Form fields (`form-elements.css`) — one source of truth

Every text-like `<input>` (text, email, password, search, number, url, tel, date, time, ...),
`<textarea>` and `<select>` is styled here with no class: 8px radius, `--field-border`, `--field-bg`,
0.875rem / 1.25rem text, 0.5rem x 0.75rem padding (38px tall), brand-blue focus ring
(`--field-focus-ring`), `--text-muted` placeholder, 0.6 opacity disabled, `aria-invalid="true"` /
`.is-invalid` error state. `.field-sm` is the compact size for toolbars and dense tables.

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
this file and the two Plyr player files fail `rangeSliderStructure.test.ts`.
