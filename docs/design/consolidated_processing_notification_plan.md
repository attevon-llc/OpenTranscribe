# Consolidated processing notification (one card per file)

Status: implemented on `feat/ui-polish-batch-oct21` (Task 8, frontend only); post-processing
stages and the honest "Complete" added on `fix/notification-finishing-stages` (backend + frontend,
see "Post-processing stages" below).

## Problem

One uploaded file produced up to five separate bell-panel rows, and the badge counted each one:
`transcription_status`, `summarization_status`, `topic_extraction_status`, `redaction_status` and
(for URL imports) `youtube_processing_status`, each keyed `progressId = <type>_<file_id>`. The
enrichment events were folded into the transcription row as green "done" chips with no timing.

## Event inventory (backend -> `stores/websocket.ts`)

| WS type | Emitter | file-scoped | statuses seen | progress | `duration_seconds` |
|---|---|---|---|---|---|
| `youtube_processing_status` | `tasks/youtube_processing.py` | yes | pending, processing, completed, error | 0-100 | no |
| `transcription_status` | `tasks/transcription/notifications.py` | yes | pending, processing, completed, error | 0-100 | on completion |
| `transcript_ready` | same | yes | (none) | - | - | silent, data plane only |
| `post_processing_status` | `services/post_processing_status.py` (only sender) | yes | per task: queued, started, done, failed, skipped | - | `started_at`/`at` per task |
| `redaction_status` | `tasks/redaction_task.py` | yes | done, failed, skipped, (pending) | - | no |
| `summarization_status` | `tasks/summarization.py`, `downstream.py` | yes | processing, completed, failed, disabled | 10/30/50/100 | on completion |
| `topic_extraction_status` | `tasks/topic_extraction.py` | yes | processing, completed, failed, not_configured | - | on completion |
| `auto_label_status` | `tasks/auto_labeling.py` | **no** (`file_id="retroactive_apply"` / `"batch_grouping"`) | | | ONE batch row per run |
| admin `*_progress/_complete`, sharing, takedown, downloads, audio extraction | various | no | | | stay plain rows |

Removed by the post-processing work: `enrichment_started`, `enrichment_task_complete` (postprocess,
analytics, speaker attribute/identification/clustering) and `search_indexing_complete` (which also
bypassed the quarantine gate: it passed the file id in `extra`, not to the suppression check).

Every `transcription_status` processing frame carries a `stage` key
(`prepare|transcribe|diarize|finalize|speakers`), from one enum,
`app.core.enums.TranscriptionStage`, which the engine's `ProgressEvent` also uses (`adapt_legacy`
forwards it to a stage-aware task callback). Terminal frames omit it. Every
`send_progress_notification` call site is pinned to its stage, and the enum to the frontend's
`TRANSCRIPTION_STAGES`, by `backend/tests/unit/test_transcription_stage_wire.py`. The frontend
reads the stage; the percent never decides a step.

Silent LLM paths (an unconfigured LLM sends no `summarization_status`; a user who turned
automatic topic extraction off gets no topic event) are no longer guessed at: the plan reports
both as `skipped`.

## Architecture

The store keeps **one record per event type per file** exactly as before: the file page
(`lib/fileDetail/notificationHandler.ts`), the gallery (`routes/+page.svelte`) and
`UserFileStatus.svelte` all dispatch on `notification.type` and must keep working unchanged.
Consolidation happens in a pure view model, and every panel/badge surface reads only that:

```
WS frame -> websocket.ts (records + run timing via lib/notifications/runTiming.ts;
                          post_processing_status via lib/notifications/postProcessing.ts)
         -> notifications[]  -- pages read raw records (unchanged)
         -> lib/notifications/jobGrouping.ts   (root bundle: badge + retention need it)
              groupPanelItems(list)     -> PanelItem[] ('job' per file | 'single')
              countUnreadItems(list)    -> bell badge (unreadCount) + panel header
         -> lib/notifications/processingJob.ts (lazy, loads with the panel body)
              buildJobModel(item, {llmAvailable, now}) -> ProcessingJobModel
         -> lib/notifications/retention.ts prunes a file's records as ONE unit
         -> lib/notifications/reconcile.ts (lazy) GET /files/{uuid}/post-processing when the
              model says a frame may have been missed
```

Bundle: `NotificationsPanel.svelte` (mounted by the root layout) is only the shell; the body
loads with `import()` on first open. Measured on `HEAD` + only these changes: root layout node
20,095 B -> 16,633 B gzip, static closure 131,156 B -> 128,728 B.

Job member types (`JOB_TASK_BY_TYPE`): `youtube_processing_status -> import`,
`transcription_status -> transcription`, `redaction_status -> redaction`,
`summarization_status -> summary`, `topic_extraction_status -> topics`,
`post_processing_status -> postProcessing`. A record is a member only
when it is non-silent and has a `data.file_id`; the group key is `job:<file_id>`.

### Run timing captured in the store (`runTiming.ts`, pure)

Added to `Notification`: `startedAt`, `finishedAt` (epoch ms), `stepLog: {p, at, stage?}[]` (one
entry per change of progress value or stage, capped at 40, first entry kept; a frame without a
stage keeps the previous entry's). A processing frame on a record that was terminal starts a new
run (timing reset), which is how retry/reprocess are told apart from progress. (The
`enrichment*` fields that rode on the transcription record are gone: post-processing has a
record of its own, so a rediarize post stage restarting the transcription run no longer wipes it.)

## Post-processing stages (`fix/notification-finishing-stages`)

**The bug.** The card said "All tasks complete" while speaker gender, search indexing and other
post-processing were still running. "Indexing and analysis" had weight 0 because nothing said
when it finished; worse, `enrich_and_dispatch` sent `enrichment_task_complete` for search
indexing right after *dispatching* it, several tasks reported nothing when a setting turned them
off, and failures sent nothing at all.

### Inventory: what runs after the transcript is committed

Dispatch order: `postprocess._finalize` marks the file completed (or hands off to the
`rediarize` post stage / the CPU voiceprint stage, which own completion) and queues
`transcription.enrich_and_dispatch`, which dispatches, in order: search indexing, file facts,
redaction, then `trigger_automatic_summarization` (analytics, summary, topics), speaker
attributes (which chains LLM speaker suggestions), speaker clustering. Summary chains its index;
topic extraction applies tag/collection suggestions inline.

| Task (key) | Celery task | Event before | Completion signal before | Now |
|---|---|---|---|---|
| voiceprints / speaker matching | inline in `postprocess`, or `rediarize`, `extract_speaker_embeddings` | `transcription_status` stage `speakers` | the run's completed frame | unchanged (main tier) |
| `search_indexing` | `index_transcript_search` | `enrichment_task_complete` sent at DISPATCH (`postprocess.py`); `search_indexing_complete` (raw, unsuppressed) on success | none on failure; "done" before it ran | decorator: started / done / skipped (no segments) / failed after the retry horizon; retries keep it started; a debounced reindex reports queued |
| `file_facts` | `artifacts.generate_file_facts` | none | none | decorator |
| `analytics` | `analytics.analyze_transcript` | `enrichment_task_complete` on success | none on failure | decorator |
| `redaction` | `redaction.detect` | `redaction_status` done/skipped | none on failure; nothing when off | plan says skipped when off; decorator |
| `speaker_attributes` | `detect_speaker_attributes` | `enrichment_task_complete` on success only | none on the 6 skip paths or failure; listed even when detection is off | plan queued/skipped; decorator |
| `speaker_identification` | `ai.identify_speakers` | `enrichment_task_complete` | none when the auto switch is off | its dispatcher reports queued/skipped BEFORE the attribute task's own outcome; decorator |
| `speaker_clustering` | `speaker.cluster_for_file` | `enrichment_task_complete` | none on failure | decorator (retries keep it started) |
| `summary` | `ai.generate_summary` | `summarization_status` | silent when no LLM | plan skipped with no LLM / disabled; decorator with a deferred start (no "running" flash without an LLM) |
| `summary_indexing` | `index_file_summary` | none | none | summary reports it queued before its own done; decorator |
| `topics` | `ai.extract_topics` | `topic_extraction_status` | silent when the auto switch is off | plan skipped; decorator with a deferred start |
| `auto_label` | inline in topic extraction; `ai.auto_label_batch` per file in a retroactive run | batch-scoped `auto_label_status` only | per file: none | `reporting_step` started/done/failed per file; the retroactive coordinator reports each file queued; the run stays ONE `auto_label_status` row |

Not tracked, deliberately: `ai.group_batch_files` (upload-batch grouping across files) stays a
batch row; manual triggers (summary regenerate, identify speakers, reprocess stages) report
through the same decorators, so an open card shows them, but they are not part of a plan.

### Backend (single source of truth)

- `app/core/enums.PostProcessingTask` (keys) and `PostProcessingState`
  (`queued | started | done | failed | skipped`).
- `app/services/post_processing_status.py` is the only sender. `report()` (one task),
  `report_plan()` (the fan-out's decisions; opens a run with `run_at`), `@tracks_post_processing`
  (start + outcome of a task body: return `{"status": ...}` mapped by `outcome_of`; a Celery
  `Retry` or an `autoretry_for` exception reports nothing), `reporting_step()` (a step inside
  another task). Frames go to the file OWNER via `send_ws_event_for_file` (quarantine-suppressed),
  carry `{file_id: uuid, tasks: {key: {state, at, started_at?}}, run_at?}` and nothing else (no
  filename, no transcript text).
- Each report is also written to Redis `post_processing:{file_id}` (Lua: last write wins per task
  by server time; a newer plan drops older entries; 2-day TTL) and served by
  `GET /api/files/{uuid}/post-processing` (tenant- and quarantine-gated; `run_at: null` = no run
  known).
- The plan is sent from `enrich_and_dispatch` after every dispatch decision. A task not
  dispatched at all is absent, so nothing waits for it. Pinned by
  `backend/tests/unit/test_post_processing_status.py` (enum <-> card tiers, a reporter per key,
  ordering, quarantine, Redis semantics, the REST fallback, the retroactive auto-label run).

### Frontend

`post_processing_status` frames become ONE record per file (`postProcessing.ts`,
`upsertPostProcessingRecord`): created only beside an existing transcription record (an admin
reindex of an old file creates no card), status `processing` while any task is queued/started
(retention pins it), `error` on a failure (kept), `completed` otherwise; always dismissible.


## Model (`processingJob.ts`)

Task states: `pending | running | done | failed | skipped`. Wire status mapping:
completed/success/complete/finished/done -> done; error/failed -> failed;
disabled/not_configured/skipped -> skipped; pending/queued -> pending; anything else -> running.
Post-processing entries map queued -> pending, started -> running.

Three tiers, shown only when they apply:

| Tier | Tasks | Shown when |
|---|---|---|
| Main processing | import, transcription (+ steps), diarization | a transcription/import record exists |
| Finishing | speaker gender, speaker suggestions, speaker clustering, search indexing, facts and keyphrases, analytics, redaction | always after transcription (placeholder "Preparing the final steps" until the plan) |
| AI enrichment | AI summary, AI topics, summary indexing, tags and collections | any AI task is not skipped; before the plan, only when an LLM is available |

`POST_PROCESSING_TIERS` maps every backend key to a tier (pinned to the enum by the backend test).

Overall state, in order: **Needs attention** if main processing failed; **Processing** until the
transcript is ready; **Finishing up** while any post-processing task is queued/running or the plan
has not arrived (within `PLAN_GRACE_MS`, 2 min); **Needs attention** if a post-processing task
failed; **Transcript ready** ("final steps could not be confirmed") when no plan was ever seen
(grace over, or the server knows no run); **Complete** otherwise. Never "Complete" with a known
task outstanding or with no plan.

Weights: import 10, transcription 60, then per task (gender 3, suggestions 2, clustering 1,
indexing 4, facts 2, analytics 1, redaction 2, summary 10, topics 4, summary index 1, tags 2);
before the plan the finishing tier holds 15. Percent = weighted mean, clamped to 99 unless
Complete / Transcript ready (100); a failure keeps the progress reached.

Missed frames: an outstanding run with no frame for `STALE_AFTER_MS` (10 min) is flagged "No
update for a while" (still Finishing up, never Complete). The model sets `reconcile` when the
plan is late (> 30 s), when it would say Transcript ready, or when a finishing run is quiet for
5 min; the open panel then reads `GET /files/{uuid}/post-processing` (throttled to once a minute
per file, `reconcile.ts`) and folds the snapshot in like a plan. A 403/404 (file gone) folds in
"no run known"; a network error changes nothing.

**Stale downstream records.** A summary/topics/redaction record older than the current
transcription run's `startedAt` belongs to a previous run (reprocess) and is treated as absent.

**Transcription steps** come from the `stage` on each `stepLog` entry. The current step is the
latest entry's stage (a record with no stage at all, e.g. from an older backend, sits at
`prepare`); earlier steps are `done`, later ones `pending`, or on failure the current one
`failed` and later ones `skipped`. A step's duration is the sum of the spans from each entry
that opens a run of that stage to the next entry of a different stage (or `finishedAt`), so a
stage the backend returns to is timed correctly. A stage never observed is `done` with no
duration once a later stage or completion is seen (frames can be missed across a reload). The
diarization chip is the `diarize` step's state and duration.

Stage mapping, by producer: preprocess and any requeue (`transcription_retry`, the OOM retry in
`task_recovery_service`) -> `prepare`; GPU/CPU/cloud transcription, including the engine's
in-stage "Loading audio" -> `transcribe`; engine/gpu-split diarization -> `diarize`; engine
speaker assignment and `finalize.py` -> `finalize`; `postprocess.py` (voiceprints, hand-off to
the post stage or voiceprint stage, completion) -> `speakers`. The post stage
(`rediarize_task`, also the manual re-detect) follows a completed frame, so the client sees a new
run: `diarize` (start, audio, detection) -> `finalize` (assign, save) -> `speakers` (voiceprints).

The card is dismissible only when every member record is dismissible (the post-processing
record always is).

## UI states (`components/notifications/ProcessingJobCard.svelte`)

- Header: product logo, filename as the link to `/files/<id>` (marks every member read), state
  line (Processing + current step / Finishing up + "Waiting on: A, B, C +N" or "Preparing the
  final steps" / Complete / Needs attention + "X failed: message" / Transcript ready + "Final steps
  could not be confirmed"), plus "No update for a while" when stale.
- Primary bar: `role="progressbar"`, `aria-valuenow/min/max`, `aria-valuetext`, labelled by
  filename; percent text and ETA beside it. Shimmer only while running.
- Tier chip row: one chip per shown tier (finished tiers collapse to a single done chip), with a
  `done/total` count while the tier is open; accessible name "Tier: State, N of M done".
- Task chip row (live or failed cards only): the running and failed tasks, or, when nothing is
  running yet, the waiting ones. Skipped tasks never have a chip.
- Details toggle: `aria-expanded` + `aria-controls`, lists each tier (state) and its tasks
  (state or duration from server `started_at`/`at`), transcription steps nested.
- Live region: `JobAnnouncer.svelte`, one polite `role="status"` region for the panel, speaks
  only a job's transition into Complete / Needs attention / Transcript ready, never progress
  ticks or the move into Finishing up, and seeds silently on mount.
- SVG glyphs only (`TaskStateGlyph`), no emoji or text glyphs.
- `prefers-reduced-motion`: no shimmer, pulse or width transition.

Component split (the old panel was 1062 lines), all under `components/notifications/` unless noted:
`components/NotificationsPanel.svelte` (shell: open state, scroll lock, click-outside, backdrop,
lazy body), `NotificationsPanelBody.svelte` (coordinator: store access, header, list, clock, open
and dismiss), `NotificationShell.svelte` (item chrome: indicator, icon box, dismiss),
`NotificationRow.svelte` (non-job rows), `ProcessingJobCard.svelte`, `JobTaskBreakdown.svelte`,
`NotificationProgress.svelte` (the `role="progressbar"` bar), `TaskStateGlyph.svelte`,
`NotificationIcon.svelte`, `JobAnnouncer.svelte`; icon registry, tone, relative time and filename
shortening in `lib/notifications/display.ts`.

## Deleted paths

- Per-event rendering of file-processing records in the panel (each type was its own row).
- `Notification.completedEnrichments` and the green enrichment chips on the transcription row.
- The generic row's duration chip (every record that carries `duration_seconds` is now a job
  member, so it was unreachable); its "duration_seconds, never duration" trap test moved to the
  model.
- The per-record unread count in `unreadCount` and the panel header.
- The panel-local `showPanel = false` on link click (it hid the panel without closing the store,
  so the next bell click did nothing); the store is closed instead.
- The click-outside lookup of `.notifications-button`, a class nothing has, which made the
  listener a no-op; it now looks up the real `.notifications-btn`.
- The diverging `dismissible` rules of the create and update paths (a first frame of
  `not_configured`/`disabled`/`done` was undismissable); both use the terminal-state mapping.
- Retention treated redaction's `done` as not-a-success, pinning such rows forever.
- The `notifications.completedIn` string (all 12 locales), dead with the duration chip.
- (post-processing) `enrichment_started` / `enrichment_task_complete` / `search_indexing_complete`,
  `postprocess._build_enrichment_task_list`, `search_indexing_task._send_indexing_notification`,
  `Notification.enrichment*`, `ENRICHMENT_TASK_KEYS`, the "Indexing and analysis" task,
  `LLM_START_GRACE_MS`, and the strings `notifications.enrichment.*`, `job.task.enrichment`,
  `job.stateDone`, `job.stateFailed`, `job.announceFailed`.

## Verification

`npx vitest run src/lib/notifications src/stores/websocket src/components/NotificationsPanel.test.ts src/components/notifications src/components/Navbar.notificationsMount.test.ts`,
`npm run check`, `npm run check:i18n`, `npm run build`; backend
`pytest tests/unit/test_post_processing_status.py`. Visual: inject frames with Playwright
`routeWebSocket` on `/api/ws` (normal flow, finishing after AI done, skipped tasks, failure,
no LLM; light + dark, `ar` RTL, 1440 and 390), or open the bell during a real upload.
