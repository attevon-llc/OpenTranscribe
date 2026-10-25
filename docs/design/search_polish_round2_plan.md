# Search page polish, round 2 (Oct 2026)

Follow-up to `search_results_ux_audit.md` (PR #1266), from the owner's feedback after using
it. Reviewed in a real browser first: the worktree's Vite on :5479 against the live dev
backend (:5174, 4 real files), headless Chromium at 1920/1440/1180/390, light and dark, en
and ar. Read-only throughout (GET `/api/search`, clicks on non-mutating controls).

## Recorded `/api/search` shape (keys only)

- Envelope: `filters_applied, page, page_size, query, results, search_mode, search_time_ms,
  source_counts, sources, summary_results, summary_total, total_files, total_pages,
  total_results`.
- Transcript hit: `content_type, duration, file_id, file_size, file_uuid,
  has_both_match_types, keyword_occurrences, language, match_sources, occurrences,
  relevance_percent, relevance_score, semantic_confidence, semantic_occurrences,
  semantic_only, speakers, tags, title, title_highlighted, total_occurrences, upload_time`.
  No `collections` key.
- Occurrence: `chunk_index, end_time, has_keyword_match, highlight_type, match_type, score,
  snippet, speaker, speaker_highlighted, start_time`.
- Summary hit: `file_id, file_uuid, matches, title`.

## 1. Search bar width

**Finding.** Round 1 capped `.search-header` at `65rem` (results column 44rem + gap 2rem +
panel 19rem) and start-aligned it. Measured: at 1440 the content area is 1120px and the bar
1040px, so it looks full width; at 1920 the content area is 1600px and the bar still stops at
1040px, leaving 536px of empty header beside a navbar that spans the whole screen. That is
the "it used to span left to right, now it is shorter" the owner saw.

**Decision: the header band (title row, query field, source chips, results bar) spans the
full width of the content area again; the reading column stays capped.**

- The query field is a page-level control, not reading text. A line-length cap exists so the
  eye can return to the next line of a snippet; it buys nothing for an input. Document-search
  and app-shell conventions (gallery toolbar here, GitHub search, Gmail, Drive) give the
  query box the workspace width; web engines that cap the box (Google, Bing) do so because the
  page has no other chrome, and even there the box does not end where the knowledge panel ends.
- Alignment that matters for scanning is the shared **start** edge: field, chips, result count,
  result titles and snippets all start on one line (inline-start, so mirrored in `ar`). The
  band's **end** edge now matches the navbar's end edge instead of floating mid-screen.
- The results bar (count, Smart/Exact, sort) spans the same band, so the sort control sits
  under the search button and its bottom rule separates "query" from "results" edge to edge.
- Results keep `max-width: 44rem` (readable snippets) and take it first; the panel gets the
  rest, at least 19rem and at most 24rem, so wide monitors give the facts some room without a
  giant card and a 1440 screen never narrows the reading column for the panel.
- Mobile: unchanged, both are full width already.

## 2. "Details" does nothing on transcript results

**Root cause (wide layout, the owner's case).** In panel mode Details only pins the hit in the
aside. The aside already follows the hovered or focused result, and clicking a button first
hovers and focuses its card, so the aside already shows that file when the click lands. The
only change is a 1px border on the card and `font-weight: 500` on the button. Measured: the
button flips `aria-pressed` false -> true and the aside's content is byte-identical before and
after. Narrow layout (1180 with filters open, 390) works: it expands the inline card. No
handler or prop is missing for transcript hits; summary hits have no Details button by design.

**Fix.**
- Wide: the button reads "Pin details" with a pin glyph and is a real toggle (`aria-pressed`,
  pressed styling: tinted pill, filled pin). Pinning gives the aside a visible pinned state: a
  "Pinned" label row with the pin glyph, a primary border, and an "Unpin" icon button
  (`aria-label`), plus a short highlight pulse on the aside when the pinned file changes
  (no animation under `prefers-reduced-motion`). Pressing the card's button again or the
  aside's Unpin clears it; the aside then follows hover/focus again.
- Narrow: unchanged behaviour ("Details", `aria-expanded`/`aria-controls`) plus a chevron so
  the disclosure reads as one.
- The card keeps the selected outline while pinned.

## 3. Tags and collections in the info card

**Findings.**
- `tags` reaches the card (index-sourced, `update_file_tags_index` keeps it current) and
  renders when non-empty. None of the 4 dev files has a tag (file detail confirms `tags: []`),
  so nothing showed. Rendering is verified with the real response decorated in the browser
  (route interception, no writes) and with component tests.
- `collections` is not sent at all.
  (Later in the session the AI auto-labeller had tagged all four files and put each in two
  collections; index tags then matched Postgres file by file, 9/8/8/8, and render.)
- **Related bug found (1):** the page's collection filter sends `collection_id=<uuid>` (the
  filter sidebar's ids are UUIDs) to a parameter declared `int`, so selecting any collection in
  the search sidebar makes `/api/search` answer **422** (reproduced with curl). A collection
  chip can only link to a working filter, so this is fixed here.
- **Related bug found (2):** with the 422 fixed, the filter still returned nothing. The chunk
  documents' `collection_ids` is written only at (re)index time and nothing rewrites it on a
  membership change (`update_file_access_index` rewrites `accessible_user_ids` only). Measured
  on the dev index: all four files' chunks carried `collection_ids: []` while each file sat in
  two collections. The filter now resolves membership from Postgres
  (`collection_visibility.collection_member_file_uuids` -> a `terms` on `file_uuid`), the
  pattern RAG chat already uses for collection scope; only past 65,536 members does it fall
  back to the indexed field. `test_search_completeness_opensearch.py` gets the synthetic
  corpus's membership injected so its `collection_id` filterset stays meaningful.

**Decisions.**
- New `backend/app/services/collection_visibility.py`, the one place that answers "which
  collections may this caller see" for these surfaces, built on the existing
  `PermissionService` helpers (`get_accessible_collection_ids`, `collection_tenant_pred`,
  `org_scope_pred`), never a raw join. #1275 (restricted org collections,
  `v060_org_content_governance_plan.md` 3.1) replaces `visible_collection_ids` there.
  - `visible_collection_ids(db, user_id, organization_id)`: accessible and in the tenant.
  - `collections_on_files(db, file_ids, user_id, organization_id)`: one batched query for a
    whole result page (no N+1), membership rule identical to the file page (the caller's
    collections in the file's tenant). `files/crud.get_file_collections` now delegates to it,
    so the file page and the search card cannot disagree.
  - `resolve_collection_filter(...)`: a collection the caller cannot see filters to nothing
    (sentinel id), never widens and never errors, so the filter is not a membership oracle.
- `/api/search` hits gain `collections: [{uuid, name}]`, display fields only, sorted by name.
  `GET /api/search` gains `collection_uuid` (the SPA sends it); the existing int
  `collection_id` stays for API callers and goes through the same visibility check; both at
  once is a 400.
- Chips link to the filtered search: tag -> `/search?q=..&tags=<name>`, collection ->
  `/search?q=..&collection=<uuid>` (new URL param, read on load and written on search). A plain
  click applies the filter in place (keeps other filters, syncs the sidebar); modified clicks
  open the link. The gallery has no URL-driven filter, so search is the filtered view.
- Info card values get `dir="auto"` (fixes "MB 115.0" in `ar`).

## 4. Collapsible match list

- Caret button at the card's top inline-end corner (top-left in `ar` via logical layout),
  chevron SVG, `aria-expanded`, `aria-controls` = the occurrence list id, keyboard native
  (button), rotates 180deg (transition off under reduced motion).
- Collapsing hides the snippet list and "Show N more"; title, meta, Details and
  `.view-transcript-btn` stay visible (E2E contract).
- Default: the first 5 results open, the rest collapsed. A user's choice is remembered per
  file for the session in `searchStore` (`matchListOpen`), cleared with the search store.
- "Collapse all / Expand all": one quiet text button at the end of the Transcripts block
  header line, outside `.results-list`.

## Files

Backend: `app/services/collection_visibility.py` (new), `app/api/endpoints/search.py`,
`app/api/endpoints/files/crud.py`, `app/schemas/search.py`,
`app/services/search/hybrid_search_service.py` (`_collection_filter_clause`); tests
`tests/unit/test_collection_visibility.py` (new), `tests/api/endpoints/test_search.py`,
`tests/integration/test_search_completeness_opensearch.py`.

Frontend: `routes/search/+page.svelte`, `components/search/SearchResultsBody.svelte`,
`SearchResultCard.svelte`, `SearchResultInfoCard.svelte`, `stores/search.ts`,
`lib/ui/menuIcons.ts` (pin, chevron-down, tag, folder, close glyphs for `ui/MenuIcon`), the
component tests plus `routes/search/collectionFilter.test.ts`, 12 locale files (8 keys),
`components/search/CLAUDE.md`.

## Verification

Backend tests red on a `git archive HEAD` tree first, then green; full backend unit/API suite
once. Frontend tests red first; check, vitest, check:i18n, test:audit, build (bundle budget).
Screenshots before/after in `/tmp/ot-search2-shots/`. The live stack runs master's backend, so
the end-to-end view of `collections` uses this worktree's backend on :5480.
