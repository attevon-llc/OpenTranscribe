# Legal documents and first-run wizard polish: plan

Branch `fix/legal-wizard-polish` (from `master` `a236125a`). Scope: the legal document
typography, a full-screen reader, and the first-run wizard's stepper. The "live updates
reconnection" toast seen after Confirm is out of scope (websocket code, handled elsewhere).

## Evidence (before)

Captured from this worktree's Vite dev server against the isolated `otfresh-master` stack,
with the wizard status, the legal documents and every mutating request stubbed by Playwright
routing (nothing was accepted or saved server-side). Matrix: light/dark x en/ar x
1440/1024/390. Script: `/tmp/ot-legal-tools/shots.py`. Shots: `/tmp/ot-legal-shots/before/`.

| # | Defect | Evidence |
|---|---|---|
| 1 | List numbers sit **outside** the text column, flush with (or past) the panel edge, instead of in a hanging-indent gutter. Cause: `app.html` resets `* { margin: 0; padding: 0 }`, so every `<ol>` has zero `padding-inline-start` and its outside markers hang into the container padding. Neither legal surface restores it. | `en-light-1440-01-terms-step.png`, `en-light-1440-03-viewer-lists.png` |
| 2 | Nested lists are **not indented at all**: a sub-list renders as a second flat `1. 2. 3.` run under its parent, so "1. Retention" is followed by "1. recordings..." at the same position. | `en-light-1440-03-viewer-lists.png`, `ar-dark-1440-03-viewer-lists.png` |
| 3 | A list that resumes at 10 renders as `1.`: the chat DOMPurify profile strips the `start` attribute `marked` emits. | `en-light-1440-03-viewer-lists.png` (last two items) |
| 4 | No paragraph spacing (the reset again): consecutive paragraphs run together; the `h1` is the browser default (2em) and repeats the dialog title at twice its size; `h2`/`h3` have no rhythm. | `en-light-1440-02-viewer.png` |
| 5 | Table cells break mid-word on a phone ("Operat / or") and the table cannot scroll. | `en-light-390-03-viewer-lists.png` |
| 6 | The terms panel is announced as a read-only **textbox** (`role="textbox"`), and at 390px it is a 40vh scroll box inside a scrolling full-screen dialog (two nested scroll areas). | `ar-light-390-01-terms-step.png` |
| 7 | The "full view" entry points ("Read the full Terms of Use", "Privacy notice") sit below the panel after a stray "Official text" caption, often scrolled out of view; there is no way to read the acknowledgment itself larger, and the viewer is a 720px dialog with no full-screen mode. | `en-light-1024-05-terms-checked.png`, `ar-dark-1440-01-terms-step.png` |
| 8 | **Escape in the viewer also closes the wizard underneath it.** Every `BaseModal` listens on `window`, so one key press dismisses every open dialog in the stack. Found while scripting the capture. | reproduced by `shots.py` (the wizard was gone after Escape) |
| 9 | Stepper: 22px outline circles, a text `✓` glyph, a green done state that no other stepper in the app uses, no connector line, the current step only distinguished by bold label, every other label hidden at all widths. | `en-light-1440-07-step-posture.png`, `en-dark-1440-06-step-password.png` |
| 10 | Footer "Skip for now" is drawn as a white raised button (the global `button` shadow and radius leak onto the link-style control). Selected posture card uses the literal `#3b82f6`, and sizes are literals rather than the type tokens. | `en-light-1440-07-step-posture.png` |
| 11 | After accepting the terms, the next step opened scrolled to the middle (the dialog body keeps its scroll position), so the stepper was out of view. Found in the after-capture. | `/tmp/ot-legal-shots/after2/en-light-390-06-step-password.png` |

## Changes

### 1. Legal prose: one component owns legal typography

New `src/components/legal/LegalProse.svelte` renders sanitized legal HTML and is the only place
legal markdown is styled (the same rule as `chat/ChatMarkdown.svelte` for assistant output). Both
the wizard's terms panel and `LegalDocumentViewer` use it; their ad hoc `:global(h1)`/`blockquote`
rules are deleted.

- Reading text: `--font-size-body` at `--line-height-body`, capped at `--measure-prose`; a
  `compact` variant (`--font-size-md`, no measure cap) for the in-wizard panel.
- Paragraph rhythm `0 0 0.85em`; first/last child margins trimmed.
- Headings: `h1` `--font-size-xl`, `h2` `--font-size-lg`, `h3` body size, all semibold with
  `--line-height-tight`; space above a section is larger than below so a heading binds to its text.
- Lists: `padding-inline-start: 2.25em` gives a hanging gutter; markers are outside, right-aligned
  to the gutter (so `9.` and `10.` align on the period), `tabular-nums`, medium weight.
  Legal-style nesting: level 1 `1.`, level 2 `(a)`, level 3 `(i)` via `@counter-style`; unordered
  `disc` then `circle`. Nested lists sit one gutter in, with `0.4em` between items.
- Tables: horizontally scrollable block, `word-break: normal`, header row on `--bg-secondary`,
  semibold `th`, cells top-aligned with `text-align: start`.
- Links: `--primary-on-surface` (AA on both themes), underlined with an offset.
- Blockquote and `hr` on theme tokens; inline `code` on the surface tint.
- Logical properties only. The text keeps `dir="ltr"` (English is the official text), but the
  component is written direction-agnostic so a translated document would mirror correctly; added
  to `typography.test.ts`'s RTL list.
- `start` is added to the chat DOMPurify profile's `ALLOWED_ATTR` **and** `ADD_URI_SAFE_ATTR`
  (DOMPurify tests every non-URI-safe attribute value against `ALLOWED_URI_REGEXP`, which `10`
  fails): it is an integer list attribute with no URL or script surface, and assistant lists
  interrupted by a code block have the same restart-at-1 bug.

### 2. Full-screen reader

Extend the existing primitive rather than drawing a second overlay (which
`modalChrome.convention.test.ts` forbids):

- `BaseModal` gains `fullscreen` (container fills the viewport, no radius/border, header stays
  pinned because only the body scrolls) and a `header-actions` slot rendered before the close
  button.
- `LegalDocumentViewer` adds a labelled header toggle, "Full screen" / "Exit full screen", with
  the maximize/minimize glyph from `MenuIcon`. The toggle keeps focus (same element, label
  changes). Below 768px BaseModal is already full screen, so the toggle is hidden there. In full
  screen the text column is centred at `--measure-prose`. A `startFullscreen` prop opens it
  directly in that mode.
- Escape closes the reader (focus returns to whatever opened it, via the existing `focusTrap`).
  **Escape is fixed to close only the topmost `BaseModal`** (a module-level open stack), so closing
  the reader never dismisses the wizard beneath it.
- No reading-progress bar and no scroll-to-accept: acceptance lives in the wizard footer and is not
  gated on scrolling, so a scroll gate in the reader would be a second, inconsistent rule.

Wizard terms step: a toolbar above the panel holds the "Official text: English" caption and a
"Full screen" button that opens the acknowledgment itself in the reader. The Terms of Use and
Privacy notice buttons stay `.btn .btn-secondary` beneath the panel. The panel becomes
`role="region"` (focusable, labelled), and below 768px it loses its own max-height so the dialog
has a single scroll area.

### 3. Stepper

New `src/components/ui/Stepper.svelte`: a progress-only (non-interactive) stepper. Visual language
taken from the two existing steppers (`FileUploader`, `WatchSourceModal`) so the app has one look:
24px dot with a 2px border; complete = `--primary-color` fill with a white check drawn by
`MenuIcon` (new `check` glyph), current = primary fill with its number plus a soft primary halo,
upcoming = surface fill, `--border-color` ring, `--text-secondary` number. A 2px connector between
dots, primary once the step before it is complete. Labels under the dots (`--font-size-xs`,
medium; current label `--text-primary` semibold), wrapping inside their column; under a 520px
container only the current label is shown. Semantics: `<ol aria-label>`, `aria-current="step"` on
the current item, a visually hidden "done" on complete items. Connector drawn with logical
`inset-inline`, so RTL mirrors with no extra rule. Transitions only under
`prefers-reduced-motion: no-preference`.

Not migrated: the uploader and watch-source steppers are clickable navigation with a "visited"
state; folding them into this primitive is a separate change.

Other wizard fixes: a step change scrolls the indicator back into view (defect 11); skip link loses the leaked button chrome; posture card selected state uses
`--primary-color` with a tint; literal sizes move to the type tokens.

### New strings (all 12 locales, real translations)

`legal.viewer.enterFullscreen`, `legal.viewer.exitFullscreen`, `firstRunWizard.stepperLabel`.

## Tests (each seen red against `HEAD` in a `git archive` tree first)

- `renderLegalMarkdown.test.ts`: `<ol start="10">` survives sanitizing.
- `LegalDocumentViewer.test.ts`: the full-screen toggle switches the dialog into full-screen mode
  and back, with the label changing; `startFullscreen` opens in full screen.
- `BaseModal.test.ts`: with two open modals, Escape closes only the top one.
- `Stepper.test.ts`: current/complete/upcoming states, `aria-current`, check icon is an SVG (not a
  text glyph), labels present.
- `FirstRunWizard.test.ts`: terms panel is a labelled `region`, not a textbox; the "Full screen"
  control opens the reader for the acknowledgment in full-screen mode.
- `typography.test.ts`: legal prose and the stepper use logical properties only.
- `FirstRunWizard.test.ts`: a step change scrolls the step indicator into view.

## Verification

Re-run the same capture into `/tmp/ot-legal-shots/after2/` (and `after3/` for 390px after the
scroll fix) and read the shots; gates:
`npm run check`, vitest on touched areas, `npm run check:i18n`, `npm run test:audit`,
`npm run build` (bundle budget), prettier via the repo hook.
