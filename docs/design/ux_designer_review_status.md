# UX designer review: implementation status and remaining-work plan

> **Update 2026-10-11 (after packages A to D landed on `feat/v0.6.0-frontend-ux`).** Items 4, 6, 9, 12
> (upload), 14 (notification logo), 17, 18, 19, 26, 29, 30 (gallery and filter sidebar) are now DONE.
> Still open: 15 (auto-clear; proposal pending an owner decision), 39 (file detail header; needs the
> designer's mockup), 40 (toolbar rows; order done, verify against the mockup). The file-size filter was
> removed on the owner's decision. Unused locale keys from items 4 and 29 were deleted.

Audit of the 45 items in `docs/design/ux_designer_review_notes.md` against branch
`feat/v0.6.0-frontend-ux` (worktree `.claude/worktrees/v0.6.0`, HEAD `00d6b229`), 2026-10-11.
Code-verified only (file:line reads), **not browser-verified**. Line numbers are as of that HEAD.

**Product decisions recorded 2026-10-11 (owner):** the designer's recommendations are accepted
as-is. The **file-size filter is removed** (item 29). Item 26's filter order is
**Type, Speakers, Duration, Date, Status, then Tags, Collections, Ownership**.

**Issue state caveat:** #747, #748, #749, #750, #751, #753, #754, #755, #756, #757 are still
OPEN on GitHub, yet most of their work is already committed on this unmerged branch
(e.g. `2e2c0831` #750, `3f2f455b` #747, `497b565a` #749, `9f04a307`/`fe6249be` #751/#752,
`d1afe829` #753, `42604fbc` #754, `861ba6a1` #755, `2070e355` #748, `1811f9fc` #756,
`8e62627d` #757 item 1). They close on merge. Do not re-implement from the issue bodies.

## Status table

| # | Item | Status | Evidence / gap | Issue |
|---|---|---|---|---|
| 1 | Docs: copy icon on install command | DONE | `docs-site/src/pages/index.tsx:231-236` uses `@theme/CodeBlock` (built-in copy button) | #758 (closed) |
| 2 | Docs: lighter border on OT column, check marks verified | DONE | `docs-site/src/pages/index.module.css:467-478` `.highlightCol` border, light + dark; OT column re-verified per #758 close comment (competitor cells not re-verified) | #758 |
| 3 | Docs: meaningful hero blurb | DONE | `docs-site/src/pages/index.tsx:204-209` outcome/effort tagline ("installed with one command…"). Not the literal "N steps" template; acceptable | #758 |
| 4 | Tags/collections pickers match sidebar search + multi-select | PARTIAL | `TagManagerModal`/`CollectionsPanel` search bars converged on style tokens (`8e62627d`), but neither uses `SearchableMultiSelect`; upload steps still hand-roll filter inputs + chip lists (`upload/UploadStepTags.svelte:92`, `upload/UploadStepCollections.svelte:118`) | #757 |
| 5 | Upload modal fixed size, no flicker, no scroll chaining | DONE | `FileUploader.svelte:1112-1120` fills fixed-height dialog; `:1233-1241` `.step-body` `overflow-y:auto; overscroll-behavior: contain` | #739 (closed) |
| 6 | Create collection works, reuses gallery create modal | PARTIAL | Works (inline, `UploadStepCollections.svelte:149-170`, enabled on input — the "disabled" report was a phantom). Does **not** reuse the gallery's `components/collections/CollectionFormModal.svelte`; placeholder `uploader.selectCollection` ("Search or create collection...") is on the create input | #739 |
| 7 | Tags step narrower/centred, collections wider | DONE | `FileUploader.svelte:101` `contentWidth: '420px'` on tags; `:861-862`, `:1250-1253` `margin-inline:auto` | #751 |
| 8 | Remove Skip where Next exists | DONE | `FileUploader.svelte:1038-1041` (Skip removed, comment) | #739 |
| 9 | Step 6 "Review"; bottom button and skip-to match | PARTIAL | Step renamed `uploader.stepReview`="Review"; jump button "Review with defaults" (`:1044-1057`). The forward button on the step before Review still reads `uploader.next` ("Next", `:1069-1079`) instead of "Review" | #751 |
| 10 | Model dropdown speed-vs-quality helper text | DONE | `upload/UploadStepModel.svelte:45,48` + `uploader.highQualitySuffix` / `fastProcessingSuffix` | #751 |
| 11 | AI summary toggle on by default, positive wording | DONE | `UploadStepModel.svelte:14,18,64` `generateSummary = !skipSummary`, label "Generate AI summary" | #751 |
| 12 | Auto-show bottom-right progress panel; bolder active-count colour | PARTIAL | Tray auto-surfaces/expands (`UploadManager.svelte:118`, `stores/uploads.ts:67`), collapsible + persisted. Active count still hardcoded amber `#fbbf24` on `var(--primary-color)` header (`UploadManager.svelte:370-372`, header `:342-345`), ~2.4:1 contrast; also `.stat-queued #d1d5db`, `.stat-completed #86efac` hardcoded | #752 (closed) |
| 13 | Notifications: "N ago" + "completed in" chip | DONE | `NotificationsPanel.svelte:481-485` duration chip, relative stamp `:162` | #753 |
| 14 | Notification icons match routes; transcription uses app logo | PARTIAL | Topic=tag and folder parity done (`NotificationsPanel.svelte:118-146`). Transcription still uses generic `file-text` glyph (`:119`), not the app logo (`assets/logo-icon.png`). Gallery Collections/Tags buttons carry no icon at all (`gallery/GalleryPrimaryActions.svelte:30-43`), so "match the route icon" has nothing to match on the gallery side | #753 |
| 15 | Notifications auto-clear | TODO (spec first) | Nothing built; designer stated no requirement. Needs a policy (types, delay, unread handling) before code | #753 §4 |
| 16 | Title nav bar darker, lifted | DONE | `styles/theme.css:112-115` light `--navbar-bg #f1f5f9` + two-layer shadow; dark `:334-337`; applied `Navbar.svelte:621-623` | #754 |
| 17 | List: title weight 500, title case, numerics right, icons centred, +32px gap | PARTIAL | Title 500 (`VirtualList.svelte:517-525`), numerics right-aligned tabular (`:573-580`), cells `align-items:center` (`:440-444`), 32px gutter (`:536-540`). Header row still `text-transform: uppercase; font-weight: 600` (`:327-330`), not title case / 500 | #749 |
| 18 | All list columns same font size; header matches | PARTIAL | Header labels now uniform (`b478b8d3`, `:592-594`). Body: status cell is `0.7rem` (`:582-587`) vs `0.875rem` elsewhere; header is `0.75rem` uppercase vs body `0.875rem` | #749 |
| 19 | Sort direction arrow: down = desc, up = asc | PARTIAL | Real arrow (not caret) shipped, but the glyph points **up** and is unrotated for **desc** (`ui/SortDropdown.svelte:101-115`, `.asc` rotates 180 `:214-215`; same for menu `:143-148`, `:305-306`). Inverted vs the decision. Comment `:94-99` (J10) to be replaced | #747 |
| 20 | Status: green check / red X circle + real tooltip, grid and list | DONE | `gallery/FileStatusIndicator.svelte:52-80` dot+glyph, `title`/`aria-label`; used by `VirtualList.svelte:288` and `VirtualGrid.svelte:331` | #749 |
| 21 | Filter collapse: left chevron to collapse, right to open, no side swap | DONE | `ui/FilterPanelToggle.svelte:22-37,72-79` one right-pointing chevron rotated 180 when expanded; 32px square; fixed position in `gallery/GalleryFilterPanel.svelte:58-60` | #750 |
| 22 | Remove duplicate helper text under filename search | DONE (cleanup left) | Search block never renders: gallery `GalleryFilterPanel.svelte:67` and search `routes/search/+page.svelte:591` both pass `showSearchField={false}`; search lives in the toolbar (`GalleryHeader.svelte:64`). The dead branch with the duplicate `<small class="input-help">` remains at `FilterSidebar.svelte:843-856` (only `FilterSidebarTestHost` hits it) | #747/#750 |
| 23 | Section headers less bold | DONE | `FilterSidebar.svelte:1332-1339` h3 `0.6875rem`, weight 500, secondary colour | #750 |
| 24 | More vertical space / larger dividers | DONE | `FilterSidebar.svelte:1267` `gap: 1.5rem`; divider `:1317-1330` | #750 |
| 25 | "No speakers detected" cannot be filtered | DONE | Root cause #743: unlabeled-speaker facet `FilterSidebar.svelte:964-978`, error + retry `:907-919`, truncation signal `:979-983`. Caveat: files with **zero** detected speakers still have no facet; reopen only if the designer meant that | #743 (closed) |
| 26 | Filter order matches list columns | PARTIAL (decided) | Current `FilterSidebar.svelte:867-1253`: Type, Speakers, Duration, Date, **Size**, Status, Tags, Collections, Language, Ownership, Owners. Matches the decided order once Size is removed (item 29) and Owners folds into Ownership (item 30). Language is not in the decided list; keep it between Collections and Ownership. Update the "judgment call" comment `:858-865` to say settled | #750 |
| 27 | Typed start/end date entry + graphical picker | DONE | `ui/DateRangePicker.svelte:119-136` two `type="date"` inputs alongside the picker (shared with File Status) | #750 |
| 28 | File-type chips get icons | DONE | `FilterSidebar.svelte:878-892` audio/video icons; documents are future (#516/#552) | #750 |
| 29 | Duration chips above slider; remove file-size filter | PARTIAL (decided) | Chips done (`FilterSidebar.svelte:629-660`, `:1015-1026`, metadata-gated, zero-match chips hidden). File-size filter still present (`:1068-1106` + state) and is now a decided removal, see Package A | #750 |
| 30 | Ownership = searchable dropdown of owners I can access | PARTIAL (decided) | Owner multi-select exists as a **separate** "Owners" section (`FilterSidebar.svelte:1223-1253`, #966, server-derived from visible files). The old All/Mine/Shared chip section (`:1196-1221`) is still its own section. Merge into one Ownership section | #750 / #966 |
| 31 | Clearing search clears results | DONE | `search/SearchAutocomplete.svelte:63-69` dispatches `clear` on empty value; `routes/search/+page.svelte:354,627` | #742 (closed) |
| 32 | Rename Chat to "AI Chat" (navbar, process dropdown) | DONE | `Navbar.svelte:383-392` `nav.aiChat`="AI Chat"; `gallery/GallerySelectionActions.svelte:188` `gallery.bulk.aiChatWithSelected`="AI chat with {{count}}" | #754 |
| 33 | Navbar order; "Transcript Search" single line | DONE | `Navbar.svelte:346-414` Gallery, Transcript Search, AI Chat, Speakers; `white-space: nowrap` `:719-723` | #754 |
| 34 | Search-hit transcript modal: sticky nav, per-mode counts | DONE | `transcript/TranscriptViewModal.svelte:321-338` Keyword(n)/Semantic(n) legend + match nav; sticky `:457-459`; shared file-detail components | #745 (closed), #755 |
| 35 | Replace scroll-position bar with "Jump to current" | DONE | Minimap deleted (`transcript/TranscriptSegmentList.svelte:545`); button `TranscriptDisplay.svelte:207-214` with hover title, scroll-only `:76-87` | #748 |
| 36 | Unconfirmed speakers show LLM name / overflow | DONE | #741 fix; segment label `TranscriptSegmentList.svelte:347,438`, suggestion accept path `:291` | #741 (closed) |
| 37 | "Add speaker" needs name entry | DONE | `SegmentSpeakerDropdown.svelte:164-178` name-first create modal | #740 (closed) |
| 38 | File-status icon says "status" | DONE | `Navbar.svelte:321-330` activity/pulse glyph | #754 |
| 39 | File detail left column reorg; comments right; header goes away | PARTIAL | Order done (`routes/files/[id]/+page.svelte:2292-2383`: player, waveform, actions bar, Tags/Collections or speaker editor, Analytics, File details); comments in right column titled "Comments" (`:2428-2433`, `comments.title`). **Still present:** the `FileHeader` block at the top (`:2262-2269`, owns title editing) and a "Video"/"Audio" `h4` + `FileActionButtons` row above the player (`:2275-2290`) | #748 |
| 40 | Gallery toolbar two-row redesign | PARTIAL | Two rows built (`gallery/GalleryHeader.svelte:46-93`): row 1 search left + actions right; row 2 count chip + Select items left, Sort + view toggle right. Row 1 button order is **Add media, Collections, Tags** (`GalleryPrimaryActions.svelte:18-43`); decision says Tags, Collections, **Add media rightmost** | #747 |
| 41 | File-detail search clear/next invisible in a theme; full button audit | DONE | `TranscriptSearch.svelte:11,332` uses shared `ui/SearchBar`; global focus rule fixed and pinned by `styles/theme-parity.test.ts` (#746) | #746 (closed) |
| 42 | Settings sidebar grouped and titled | DONE | `SettingsModal.svelte:328-430` groups by who/how often (Account, Transcription, AI Chat, Media & Output, Billing/Team, Administration, System) | #861 (from #757 item 2) |
| 43 | Speakers: "still being developed" banner | DONE | `routes/speakers/+page.svelte:864` `<RetrievalQualityNotice surface="speakers" />` | #756 |
| 44 | Speaker profiles: Male/Female text next to icon | DONE | `speakers/GenderBadge.svelte:53-56`; profile toggles `speakers/*Profile*.svelte:111-112` | #756 |
| 45 | Speaker "Inbox" → "Review" with count | DONE | `speakers.tabs.inbox`="Review"; badge `routes/speakers/+page.svelte:885-888` | #756 |

**Totals:** 31 DONE (one with a cleanup tail, #22), 13 PARTIAL, 1 TODO (#15, spec-gated).

### Incidental findings (fold into the matching package)

- `gallery/VirtualGrid.svelte:325` renders a hardcoded English `spk` (`{count} spk`); this ships untranslated in 11 locales. Package B.
- `routes/+page.svelte:79` imports `../components/FileUploader.svelte`, and `gallery/GalleryPrimaryActions.svelte:2-3` import `../../stores/...`. Both break the `$components`/`$stores` alias rule. Fix them when those files are touched (A and B).
- `filter.minFileSizeTooltip` / `filter.maxFileSizeTooltip` have no consumer today (dead keys in all 12 locales). Package A deletes them with the rest.
- Unit mismatch: `/api/files` takes `min_file_size`/`max_file_size` in **MB** (`backend/app/api/endpoints/files/__init__.py:242-243`), `/api/search` takes them in **bytes** (`search.py:162-163`). Document it in the backend docstrings when Package A touches them. Do not change it in this wave.

## Implementation plan (PARTIAL/TODO items only)

There are five packages. Each owns a disjoint set of component files, so they can run in parallel
**worktrees**. They cannot run as concurrent writers in one checkout (see root `CLAUDE.md`).
Each test below must be **seen red against HEAD first**. Use a `git archive HEAD` tree, as
described in the root `CLAUDE.md`, not an in-place file swap.

### ⚠️ Locale JSON collision map

All 12 files in `frontend/src/lib/i18n/locales/` are a single shared edit surface.

| Package | Locale edits | Collides? |
|---|---|---|
| A Filter sidebar | **Deletes** `filter.fileSize`, `filter.minFileSizeTooltip`, `filter.maxFileSizeTooltip`, and, if the dead search branch is removed, `filter.searchFiles`, `filter.searchHelp`, `filter.searchTooltip`. Check `filter.searchPlaceholder` first: `GalleryHeader.svelte:66` still uses it, so **keep** it | **YES** |
| B Gallery list/toolbar/sort | **Adds** one key for the grid speaker count (replacing `spk`), e.g. `gallery.speakerCountShort` | **YES** |
| C Upload + pickers | Probably none (item 9 reuses `uploader.stepReview`; item 6 reuses the modal's keys). Adds a key only if the create input gets a real placeholder (recommended: `uploader.newCollectionPlaceholder`) | Possibly |
| D Notifications | None expected (logo `alt` can reuse `nav.logoAlt`) | No |
| E File detail | Possibly (title-edit relocation may need a label) | Possibly |

**Rule:** land A's and B's locale changes **serially**, A first. A deletes keys and B adds them
in the same 12 files. Alternatively, one agent applies both packages' locale diffs at the end.
Run `cd frontend && npm run check:i18n` after each, by hand: a locale-only commit does not fire
the `frontend-check` hook.

### Package A: Filter sidebar (items 22 cleanup, 26, 29, 30)

**Files**
- `frontend/src/components/FilterSidebar.svelte`: delete the File Size section (`:1068-1106`) and all
  of its state: `fileSizeBounds` `:108`, `fileSizeSliderValues` `:114`, the `fileSizeRange` prop
  `:118`, bounds init `:588-590`, `handleFileSizeSliderChange` `:~665-680`, and the payload field
  `:729`. Also delete the reset lines `:749,:757`, the restore `:811-815`, and the
  `formatFileSize` import/helper if it becomes unused. Merge the "Owners" section `:1223-1253`
  into the Ownership section `:1196-1221`, under one `filter.ownership` heading: Mine/Shared
  quick chips above, `SearchableMultiSelect` of owners below, the same pattern as Tags `:1125-1169`.
  Keep `showOwnerFilter` gating the dropdown part. Rewrite the order comment `:858-865` as settled.
  Optionally delete the dead `showSearchField` branch `:843-856` and the prop `:72`. Both
  consumers pass `false`; update `FilterSidebarTestHost.svelte` and `routes/search/sidebar-chips.test.ts:139-140,176`.
- `frontend/src/components/gallery/GalleryFilterPanel.svelte`: remove the `fileSizeRange` prop `:16` and pass-through `:74`.
- `frontend/src/routes/+page.svelte`: remove `fileSizeRange` from the type `:66`, the state `:144`,
  the active-filter check `:163`, the `min_file_size`/`max_file_size` params `:280-285`, the filter
  handler `:593,:603`, reset `:672`, the persisted save `:1389` and the prop `:1533`. Fix the relative import at `:79`.
- `frontend/src/routes/search/+page.svelte`: remove `filterFileSizeRange` `:52,:66,:173,:463,:598`, the
  store wiring `:231,:431-432,:455`, and the API params `:304-309`. **Keep** the `file_size` sort option `:30`.
- `frontend/src/stores/gallery.ts`: drop `filterFileSizeRange` `:53,:118,:395,:411,:430`. The filter
  state is in-memory (it survives client navigation, not reloads; only view mode is in
  `localStorage`), so no persisted-state migration is needed. Any caller still passing
  `fileSizeRange` must be removed in the same commit so TypeScript catches stragglers.
- `frontend/src/stores/search.ts`: drop `fileSizeRange` `:110,:148` and `setFileSizeRange` `:205-206`.
- Tests to update: `stores/gallery.test.ts:234,261`, `stores/search.test.ts:95-97`,
  `routes/search/sidebar-chips.test.ts:156`, and the `FilterSidebar.test.ts:4` header comment.
- Locales (all 12): delete the keys listed in the collision map.
- E2E: no e2e selector references file size (`backend/tests/e2e` grep is clean). The
  `filesize-metadata-retry` testid disappears; only unit tests used it. Gallery-sidebar visual
  baselines will change: re-capture per `test_visual_regression.py`. Visual-baseline
  freshness is currently waived for v0.6.0 UI work.

**Backend: keep the params, deliberately.** `min_file_size`/`max_file_size` stay on
`GET /api/files` (`files/__init__.py:242-243,357-358` → `filtering.py:166-184,482`), on
`/api/search` (`search.py:162-163,305-306,363-364,437-438,641-642,753-754` →
`services/search/hybrid_search_service.py`, `summary_search.py`) and on
`media_collections.py:1017-1018,1091-1092`. The `file_size` range also stays in
`/files/metadata-filters` (`filtering.py:617-636`). They are public REST surface (API clients,
the planned MCP adapter #560). They are tested (`tests/api/test_files_filtering.py:167`,
`tests/api/endpoints/test_search.py:213-238`) and in `backend/openapi.json`. Removing a UI
control is not a reason to break API consumers. Nothing backend-side becomes dead: each param
still has a caller (the API) and a test. **No OpenAPI regeneration is needed.**

**Red-first tests**
1. `FilterSidebar.test.ts`, "renders sections in the decided order". Collect every `.filter-section h3`
   text and assert exactly `[filter.fileType, filter.speakers, filter.duration, filter.dateRange,
   filter.processingStatus, filter.tags, filter.collections, (filter.language when >1 language), filter.ownership]`.
   It is red today because `filter.fileSize` and `filter.owners` appear.
2. `FilterSidebar.test.ts`, "emits no fileSizeRange". Click Reset and a duration chip, then assert
   no `filter` event detail has a `fileSizeRange` key. Red today (`:729`).
3. `routes/search/sidebar-chips.test.ts` (it already renders the search page). With the store
   holding a stale file-size range, the outgoing search request has no `min_file_size`/`max_file_size`.
   Red today (`:304-309`).
4. `FilterSidebar.test.ts`, "ownership is one section". Exactly one heading `filter.ownership`, and
   it contains the owners `SearchableMultiSelect` (`.multiselect-container`). Red today.

### Package B: Gallery list, toolbar order, sort arrow (items 17, 18, 19, 40 + `spk`)

**Files**
- `frontend/src/components/gallery/VirtualList.svelte`: header `:327-330`: drop
  `text-transform: uppercase`, set weight 500. Header labels are already title case in `en`
  ("Type", "Title"…). Status cell `:582-587`: `0.875rem` like the other body cells. Keep the
  status label clipping from `3ab4ce87`.
- `frontend/src/components/gallery/VirtualGrid.svelte:325`: replace `spk` with an i18n key.
- `frontend/src/components/ui/SortDropdown.svelte:101-115,143-148,210-215,299-306`: desc draws a
  **down** arrow, asc an **up** arrow. Delete the J10 comment `:94-99`. ⚠️ This is shared with
  `/search` (`routes/search/+page.svelte` sort bar). That is intended (one control, one
  semantics), but verify both pages in a browser.
- `frontend/src/components/gallery/GalleryPrimaryActions.svelte:18-43`: DOM order Tags,
  Collections, Add media. Keep the classes `.upload-btn`/`.collections-btn`/`.tags-btn`; they are
  e2e-guarded. Fix the relative imports `:2-3`.
- Locales: one added key (see collision map).

**Red-first tests**
1. `ui/SortDropdown.test.ts`: with `sortOrder='desc'` the direction arrow's head is at the bottom
   (assert the polyline `points`, or assert no `.asc` and a down-pointing path). With `'asc'` it is
   at the top. Red today: desc renders the up arrow.
2. New `gallery/GalleryPrimaryActions.test.ts`: button order by DOM is
   `.tags-btn, .collections-btn, .upload-btn`. Red today.
3. `gallery/VirtualListHeaderStyle.test.ts` (source-reading pattern, since jsdom cannot cascade
   scoped CSS): `.file-list-header` has no `text-transform: uppercase` and has `font-weight: 500`,
   and `.list-cell-status` has `font-size: 0.875rem`. Red today.
4. `gallery/VirtualGrid.test.ts`: a file with `speaker_summary.count = 3` renders the translated
   key, and no `spk` literal appears in the source. Red today.

### Package C: Upload wizard, tray, tag/collection pickers (items 4, 6, 9, 12)

**Files**
- `frontend/src/components/FileUploader.svelte:1069-1079`: when the next active step is `review`,
  the forward button reads `$t('uploader.stepReview')`.
- `frontend/src/components/upload/UploadStepCollections.svelte:149-170`: "Create collection" opens
  `components/collections/CollectionFormModal.svelte`, the gallery's create modal, instead of the
  inline input. On create, add the new collection to the selection. Remove the inline input, or
  give it a real placeholder.
- `frontend/src/components/upload/UploadStepTags.svelte:92`, `UploadStepCollections.svelte:118`:
  converge the filter input + list onto `components/SearchableMultiSelect.svelte` (#757 item 1's
  remaining surfaces). Check `tags/TagManagerModal.svelte:453` and `CollectionsPanel.svelte:440`
  too. They are managers, not pickers, so style parity (already done in `8e62627d`) may be
  sufficient there. Decide explicitly and note the decision in the PR.
- `frontend/src/components/UploadManager.svelte:342-345,370-380`: replace the hardcoded
  `#fbbf24/#d1d5db/#86efac` with theme tokens that pass ≥4.5:1 on `--primary-color` in both themes.
  The active count gets the boldest treatment (weight 600+).

**Red-first tests**
1. `FileUploader.test.ts`: navigate to the step before Review; the forward button's text is
   `uploader.stepReview`. Red today (`uploader.next`).
2. New `upload/UploadStepCollections.test.ts`: clicking Create collection renders
   `CollectionFormModal`'s dialog. Red today (it posts from the inline input).
3. Extend `styles/primary-contrast.test.ts` (source-reading, already used for #752): `.stat-active`
   colour in `UploadManager.svelte` is a `var(--…)` token whose resolved value has contrast
   ≥4.5:1 against `--primary-color` in light and dark. Red today (`#fbbf24`).
4. `upload/UploadStepTags` / `UploadStepCollections` tests: the picker renders
   `.multiselect-container`. Red today.

### Package D: Notifications (items 14, 15)

**Files**
- `frontend/src/components/NotificationsPanel.svelte:118-121,349-356`: add a `logo` `IconName`
  and map `transcription_status` to it, rendered as `<img src={logoIcon}>` from
  `$lib/../assets/logo-icon.png`. Import it the way `navbar/NavbarBrand.svelte` imports
  `logoBanner`. Keep it 16px, `alt=""` (decorative; the title carries meaning). Check dark-mode
  legibility in a browser.
- Item 14 "collections/tags icons match their routes": the gallery Collections/Tags buttons have
  **no** icon. Either add the same tag/folder glyphs to `GalleryPrimaryActions.svelte` (this
  collides with Package B, so B should do it) or record that parity is satisfied by the
  file-detail `TagsSection`/`CollectionsSection` icons. Decide before starting.
- Item 15 auto-clear: **do not build.** Write the policy into #753 (which types, after how long,
  read vs unread, whether a completed transcription auto-clears) and get owner sign-off first.

**Red-first test**
1. `NotificationsPanel.test.ts`: a `transcription_status` notification renders an `img` whose `src`
   contains `logo-icon`, and `summarization_status` still renders the `file-text` svg. Red today.
   The existing icon-coverage test must still pass.

### Package E: File detail header (item 39 remainder)

**Prerequisite:** #748 says it needs the designer's mockup. The open question is where title
editing (`components/FileHeader.svelte:110-150`) and the processing-step/shared-permission
indicators go once the header is removed. Get that answer before coding. The likely home is the
`video-header` row (`routes/files/[id]/+page.svelte:2275-2290`), replacing the "Video"/"Audio" `h4`.

**Files**
- `frontend/src/routes/files/[id]/+page.svelte:2262-2290`: remove the `.file-header` wrapper and
  place the title (editable) plus `FileActionButtons` in the left column above the player.
- `frontend/src/components/FileHeader.svelte` (742 lines): shrink it to an inline title-edit
  component, or split the title editor out and delete the rest. Do not leave two title editors.
- `frontend/src/components/FileHeader.test.ts`: retarget it.
- E2E: grep `backend/tests/e2e` for `.file-header`/`FileHeader` selectors before renaming
  (`test_transcript_*`, `test_visual_regression.py`).

**Red-first tests**
1. Playwright (`backend/tests/e2e`, file-detail): `.file-header` is absent, and the title is
   editable from the left column (double-click/edit → save → reload shows the new title, then
   restore the original title, following the e2e no-persistence rule). Red today.
2. Vitest on the extracted title editor: save dispatches `titleUpdated` with the server's value.
   This ports the existing `FileHeader.test.ts` assertion.

### Sequencing

A and B in parallel worktrees, locales serialised A then B. C, D and E are independent of each
other and of A/B (D's optional icon work goes to B). Re-capture visual baselines once, after
the last package, per the #754 audit note (the navbar/gallery appear in every baseline).
Verify every package in a browser in light and dark, plus `ar` (RTL), before reporting done.
