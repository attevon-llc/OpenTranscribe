# Search results page: UX audit and redesign (Oct 2026)

Scope: `frontend/src/routes/search/+page.svelte` and `frontend/src/components/search/`
(`SearchResultCard`, `SearchOccurrence`, `SummaryResultCard`). Reference guide: the
designer review in `docs/design/ux_designer_review_notes.md` (label `ux-review-2026-09`,
status in `ux_designer_review_status.md`). Its "Search page" items 31-34 are already done;
it has no item for the result card itself, so this audit fills that gap. It applies the
review's standing rules: one shared primitive per pattern (item 4, item 41's "same component
as..."), light/dark parity checked per control (item 41), and less visual weight on headers
(item 23). Type rules come from `frontend/CLAUDE.md` "Type and weight rules" (#1243): title,
then muted meta, then the snippet.

## 1. What is noisy today

One transcript hit renders, all at once and at the same visual weight:

| Element | Problem |
|---|---|
| Speakers (full comma list) | Often 4-10 names. Repeats what each snippet row already says (each row has its own speaker chip). |
| Upload date, duration chip, file-size chip | Three different treatments (plain text, bordered chip, bordered chip). Size is almost never why a user picks a result. |
| Tag pills | Same pill style as the match badges beside them, so tags read as match info. |
| Keyword count badge, semantic count badge, relevance badge | Three coloured badges for one fact ("how well does this match"). |
| "showing N of M" | A third count next to the two badge counts. |
| Match-source badges (Title / Speaker / Transcript) | A fourth colour family. Useful, but only once you are already interested in the file. |
| 2-3 snippet rows, each with a speaker chip, a bordered timestamp chip and an icon button | Each row is two lines tall before the snippet text starts. |
| Play button | Red (`--error-color`) pause glyph while active, which reads as an error or a stop/record control, not as the app's playback style. The pause state also did not follow the real player: pausing or finishing in the floating player left the card showing "pause". |

A results page of 20 such cards is a wall of chips. The eye has no single line to scan.

Summary hits and the two page sections ("Summaries", "Transcripts") cannot be collapsed, so a
query with many summary matches pushes every transcript hit below the fold, and a summary hit
with 8 matching sections shows all 8.

## 2. Pattern applied: search-engine result + knowledge panel

A result answers "is this the one?" in three lines: **title, meta line, snippet**. Everything
else is progressive disclosure. File facts that help *after* you are interested (speakers,
tags, size, match sources) move to a **knowledge-panel-style info card** to the right of the
results column, the way a web search shows an entity card beside ten blue links.

### Keep in the compact result (always visible)

1. Media-type icon + title link (`.result-title`, E2E-guarded), highlighted match kept.
2. "View transcript" (`.view-transcript-btn`, E2E-guarded), now a quiet text button.
3. One muted meta line: upload date, duration, match count, relevance percentage.
   Plain text separated by middots, no chips.
4. Snippets: 1 row (2 when the hit has both exact and related matches, so both kinds show).
   Each row: play control, timestamp link, speaker as muted text, then the snippet with its
   `<mark>`. The Exact/Related label stays on dual-type hits only.

### Hide behind disclosure

- Remaining snippet rows: "Show N more matches" (`aria-expanded`, `aria-controls`).
- Everything in the info card (below): "Details" button on each result.

### Move to the info card (`SearchResultInfoCard.svelte`)

Title, then a definition list: duration, uploaded, size, language; speakers as chips (first
8, then "+N more"); tags as chips; matches (keyword / semantic counts, "showing N of M",
matched-in sources); an "Open file" link.

### Remove

- The separate keyword/semantic/relevance badge trio in the card header (folded into the
  meta line, detail in the info card).
- The local `formatDuration` / `formatFileSize` / `formatDate` copies in the card (the
  folder's CLAUDE.md flags them); the card and info card use `$lib/utils/formatting` and
  `$lib/utils/metadataMapper`'s `formatFileSize`.

## 3. Layout

- Results column: `max-width: 44rem`, left-aligned (start-aligned in RTL), so line length
  stays readable on a wide monitor.
- Wide layout (viewport >= 1440px with the filter sidebar open, >= 1180px with it collapsed,
  which is when 44rem + gap + 19rem fit): a sticky `<aside>` to the right shows the info
  card. It follows the result under the pointer **or keyboard focus** and does not clear on
  pointer-leave (so moving to the panel does not make it vanish). It defaults to the first
  result. "Details" pins a result (`aria-pressed`); while pinned, hover does not change the
  panel. Pressing it again unpins.
- Narrow layout or touch: no aside. "Details" expands the same info card inline inside the
  result (`aria-expanded` + `aria-controls`). One component, two variants.
- The aside lives outside `.results-list`, because `test_search.py` counts
  `.results-list > *` as transcript hits.

## 4. Collapsible blocks (task 3)

- Page sections "Summaries" and "Transcripts" become disclosure headings: the shared
  `ui/ExpandableSection` gains a `headingLevel` prop so the toggle is a `<button>` inside an
  `<h2>` (navigable by heading, operable by Enter/Space, `aria-expanded`). Default: open.
  The Transcripts heading only exists when both sections show; one section has nothing to
  collapse against.
- Summary hit: first 2 matching sections visible, "Show N more sections" disclosure for the
  rest. Same pattern as transcript snippets.

## 5. Playback control (task 4)

Three states driven by the real player (`FloatingPreviewPlayer` already forwards Plyr's
`play` / `pause`; `ended` also emits `pause`):

| State | Glyph | Accessible name | Click |
|---|---|---|---|
| idle (not this occurrence) | play | "Play from this point" | start this clip (any other clip stops: one player) |
| active, playing | pause | "Pause" | pause the floating player |
| active, paused or not yet started | play | "Resume playback" | resume |

`aria-pressed` is true while playing. Active styling is the primary colour (filled circle,
white glyph), the app's selected-control style, never `--error-color`. The page marks the new
occurrence active as soon as it is clicked (before the stream URL resolves), so the previous
card stops showing "pause" immediately, and a slow URL fetch for an older click cannot
overwrite a newer one.

## 6. Accessibility and theming checklist

- Every toggle is a `<button type="button">` with `aria-expanded` or `aria-pressed`.
- Focus is an outline on `:focus-visible` (#746); no colour change on `:focus`.
- Colours from tokens (`--primary-on-surface` for link text, `--text-secondary` for meta,
  `--surface-color`, `--border-color`); dark overrides use `[data-theme='dark']`.
- Logical properties only (`typography.test.ts` enforces this for search components).
- User text keeps `dir="auto"`.
