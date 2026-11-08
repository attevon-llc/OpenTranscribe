# src/components/fileDetail

## Purpose

Presentational children extracted from the file-detail route `src/routes/files/[id]/+page.svelte`
(which stays the coordinator). Import via `$components/fileDetail/...`.

## Key files

- `TxtExportOptionsModal.svelte` — the TXT-export options dialog (include timestamps / speakers /
  comments + redaction note). Markup + scoped CSS only.
- `TranscriptExportDialogs.svelte` — transcript export and copy for the page: the comments
  confirmation, the TXT options dialog, the lazily loaded Word/PDF dialog and the server-side
  copy. Every byte comes from the server (#673 downloads, #821 copy, #1278 documents); nothing here
  serializes a transcript and a failure never gets a local fallback. The page calls `openExport` /
  `copyTranscript` via `bind:this` and reads `documentBusy` / `copyStatus` back through `bind:`.
  `FilePlayer.svelte` — the player plus, for a completed audio/video file, the waveform; keeps
  `currentTime` and the active-segment highlight in step and performs the one-time `?t=` seek. The
  page owns the stream URL and calls `seekToTime` / `updateSubtitles` via `bind:this`.
  `FileSummary.svelte` — the summary modal, its reprocess actions, the header's generate action and
  the `?view=summary` deep link (the page binds the two spinner flags and calls `show()` /
  `generate()` via `bind:this`). `*TestHost.svelte` / `*TestStub.svelte` here
  (`FileSummaryTestHost`, `TranscriptExportDialogsTestHost`, `SummaryModalTestStub`,
  `VideoPlayerTestStub`) are test doubles, not product components.
- `DocumentExportOptionsModal.svelte` — the Word/PDF options dialog (timestamps, speakers, comments
  when the file has any, line numbering, page size, redaction note). **Lazy**: the page loads it with
  a dynamic `import()` so it adds nothing to the root layout or the static closure
  (`check-bundle-budget` has headroom to keep that true; never raise the budget for it). Scroll lock
  is BaseModal's, unlike the TXT dialog (whose lock lives in `TranscriptExportDialogs`). Prefs come from `$lib/export/documentExportPrefs`.
- `FileActionButtons.svelte` — the video-header action row (view transcript · AI summary
  view/generating/generate · reprocess). Emits `viewTranscript`, `showSummary`,
  `generateSummary`, `openReprocess`.
- `RedactionControls.svelte` — the show-original / rescan footer under the transcript. Renders
  nothing when the viewer can neither toggle nor rescan. Emits `rescan`, `toggleOriginal`.
- `RedactionPendingPanel.svelte` — the "detection still running" placeholder that replaces the
  transcript column while redaction is pending.
- `SpeakerProfileConfirmModal.svelte` — update-globally / create-new confirmation for a renamed
  speaker. Emits `updateProfile`, `createNewProfile`, `cancel`.
- `AudioTracksNotice.svelte` — the audio-track panel (#1263): lists the container's tracks
  (`file.audio_streams`) with the transcribed one marked, and a calm, non-blocking notice when the
  server flags `audio_language_mismatch` (detected language vs the track's tag), with a **Choose
  another track or language** action (**Choose a language** with no other track) that opens the
  reprocess wizard, where `$components/ForcedLanguageSelect.svelte` forces the transcription
  language (#1263; `language` in the reprocess body, highlighted on a mismatch). Renders nothing for
  a single-track file without a mismatch. The picker itself is `$components/AudioTrackSelect.svelte`, shown on the
  wizard's settings step; labels come from `$lib/fileDetail/audioTracks`.
- `ProcessingSection.svelte` — the collapsible "Processing" section rendered right after
  `MetadataDisplay` (#1229): how the file was processed, one row per step, via the shared
  `$components/ProcessingProvenance.svelte`. Props: `steps` (the page's
  `file.processing_steps`), `expanded`. Renders nothing when the payload has no rows. It
  replaced the models row that used to sit inside `MetadataDisplay` (hardcoded "Whisper:"
  label, and every non-`native` engine shown as PyAnnote); the backend's
  `processing_steps` is display-ready, so derive nothing here.

- **Legal hold (Lane D):** the page calls `provideFileFrozen()` and sets it from `file.hold.held`;
  `FileActionButtons` (summary generate, reprocess) and `RedactionControls` (rescan) read
  `fileFrozen()` and render disabled with a `legal-hold/FrozenNote`. `FileHeader` renders the
  held-file banner BELOW the title row and nothing hold- or ID-related in it: no hold pill, no
  Copy file ID, no Place legal hold (owner feedback 2026-11-08: they read as the title's state).
  Holds are placed from the gallery Organize menu or Settings; the file ID, for support and
  takedown disputes, is a row in `MetadataDisplay` (File details). See
  `components/legal-hold/CLAUDE.md`.

## Conventions / patterns

- The route page owns the state and orchestration; these children take props and dispatch events.
  The orchestration itself lives in **context modules** under `$lib/fileDetail/`: each takes a
  context object that reads and writes the page's state (the page keeps owning it, #338) —
  `fileDetailData` (reads), `fileSpeakers` (composes `speakerLoader` / `speakerSave` /
  `speakerEvents` / `speakerNames`), `segmentEdit`, `segmentPaging`, `segmentSync`, `segmentDom`,
  `notificationHandler` / `notificationSelect`, `summaryActions`, `reprocess` / `reprocessStages`,
  `redactionView`, `mediaStream`, `downloadStream`, `deepLinks`, `pageTimers`.
- `TxtExportOptionsModal`: `bind:show` + the `include*` toggles; emits `confirm`/`close`. **Its
  scroll lock sits in `TranscriptExportDialogs`** (a `$:` block on the TXT flag), not the page.
- The modal-chrome CSS (`.modal-overlay`/`.modal-dialog`/`.modal-*`/`.btn*`) is duplicated between
  `TxtExportOptionsModal` and `SpeakerProfileConfirmModal` on purpose — Svelte scopes `<style>` per
  component, so each dialog needs its own copy. The page no longer carries it (#284 A3.5).
- `SpeakerProfileConfirmModal` holds its own scroll lock for exactly its lifetime (taken on mount,
  released on destroy); the page renders it inside an `{#if}`.

## How it connects

- Parent: `routes/files/[id]/+page.svelte`. Export content is generated server-side
  (`GET /files/{uuid}/export`, issue #673); `$lib/export/txtExportPrefs` only persists the
  TXT options dialog's toggles. Markdown goes through the same sync flow; Word and PDF go through
  `$lib/export/requestDocumentExport` (async job, no local fallback) after the options modal, with
  `documentBusy` passed to the actions bar while one runs, and the stream closed in `onDestroy`.
  Every export or copy failure toasts a translated reason (`$lib/export/exportErrors`).
- The transcript itself is rendered by `TranscriptDisplay` + `components/transcript/*`.
- **Issue #748 layout**: the left (`.video-column`) column order is player, waveform,
  `TranscriptActionsBar` (Export · Edit speakers · Download — moved OUT of `TranscriptDisplay`,
  renders directly on the page now), then either `SpeakerEditorPanel` (while editing speakers)
  or `TagsSection`/`CollectionsSection` (otherwise), then `AnalyticsSection` (always visible,
  including while editing speakers — it's the speaker context being labelled against), then
  `MetadataDisplay` (also moved — was in `.file-header` above the grid, hidden while editing
  speakers), then `ProcessingSection` (#1229, the last item in this column, hidden with it). `CommentSection` moved to the bottom of the
  RIGHT (`.transcript-column`) column, below `RedactionControls`. The page owns
  `isEditingSpeakers` directly (`TranscriptActionsBar`'s `toggleSpeakerEditor` event flips it
  inline) — there is no longer a prop-forwarding hop through `TranscriptDisplay`.
- The download SSE stream `TranscriptActionsBar` triggers lives in
  `$lib/fileDetail/downloadStream.ts` (`createDownloadStreamManager()`, issue #748 J5) — a
  factory returning `{ downloadMedia, cleanup }`; the page calls `cleanup()` from its own
  `onDestroy`.

## Gotchas

- **`transcript_segments` is the SINGLE representation of segment data — never reintroduce a
  second copy (#352).** `file.grouped_segments` is the backend's display grouping and carries
  only `segment_uuids`; `$lib/transcript/resolveGroupedSegments.ts` (shared by
  `TranscriptDisplay` and the search-result "view transcript" modal, issue #755) is the one
  place those references are resolved. Groups used to embed full segment copies, so the page
  held two objects per segment and every optimistic write patched only the flat one — renaming
  a speaker or editing a segment saved to the database and then rendered nothing until a full
  page reload. **Mutate segments only through `$lib/fileDetail/segmentSync`**
  (`renameSpeakersInFile`, `patchSegmentInFile`, `appendSegmentPage`), never with a bare loop
  over `transcript_segments`. `transcriptStore` (the raw store) is still fed for
  `AnalyticsSection`'s `SpeakerStats` — its `processedTranscriptSegments` derived store (the
  old `TranscriptModal`'s speaker-grouped reading view) was deleted with that component
  (#755).
- **Prop-drilling is the settled pattern — don't add a store for page state (#338).** The page
  used to also write a `reactiveFile` writable on every mutation; nothing ever subscribed to it,
  so all 13 `.set()` calls were inert while reading as if they refreshed the UI. It has been
  deleted. The update path is the page's own `file` assignment (`file = {...file}`, or a member
  assignment like `file.tags = …`) invalidating the variable, which re-renders the children
  through their props. `notificationHandler.setFile` exists for exactly that reason and must keep
  doing the real assignment.
- The file-detail E2E (`backend/tests/e2e/test_file_detail_transcript.py`) guards the page's
  transcript/export/speaker-editor surfaces — keep those working when adding children here.
- This is a coordinator route: it legitimately keeps a large `<script>` (data loading, WebSocket
  notifications, speaker bulk-save). Extract markup/CSS into children and orchestration into the
  context-module pattern above (`$lib/fileDetail/*`), not into a store.
