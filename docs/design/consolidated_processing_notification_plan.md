# Consolidated processing notification (one card per file)

Status: implemented on `feat/ui-polish-batch-oct21` (Task 8). Frontend only.

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
| `enrichment_started` | `transcription/postprocess.py` | yes | - | - | `tasks: string[]` |
| `enrichment_task_complete` | postprocess, analytics, speaker_* tasks | yes | - | - | `task: string` |
| `redaction_status` | `tasks/redaction_task.py` | yes | done, failed, skipped, (pending) | - | no |
| `summarization_status` | `tasks/summarization.py`, `downstream.py` | yes | processing, completed, failed, disabled | 10/30/50/100 | on completion |
| `topic_extraction_status` | `tasks/topic_extraction.py` | yes | processing, completed, failed, not_configured | - | on completion |
| `auto_label_status` | `tasks/auto_labeling.py` | **no** (`file_id="auto_label_batch"`) | | | stays a plain row |
| admin `*_progress/_complete`, sharing, takedown, downloads, audio extraction | various | no | | | stay plain rows |

Every `transcription_status` processing frame carries a `stage` key
(`prepare|transcribe|diarize|finalize|speakers`), from one enum,
`app.core.enums.TranscriptionStage`, which the engine's `ProgressEvent` also uses (`adapt_legacy`
forwards it to a stage-aware task callback). Terminal frames omit it. Every
`send_progress_notification` call site is pinned to its stage, and the enum to the frontend's
`TRANSCRIPTION_STAGES`, by `backend/tests/unit/test_transcription_stage_wire.py`. The frontend
reads the stage; the percent never decides a step.

Silent LLM paths that matter for the weights: an unconfigured LLM sends nothing for summary
(`_handle_no_llm_configured`), and a user who turned off automatic topic extraction gets no
topic event at all (`downstream._auto_topic_extraction_enabled`).

## Architecture

The store keeps **one record per event type per file** exactly as before: the file page
(`lib/fileDetail/notificationHandler.ts`), the gallery (`routes/+page.svelte`) and
`UserFileStatus.svelte` all dispatch on `notification.type` and must keep working unchanged.
Consolidation happens in a pure view model, and every panel/badge surface reads only that:

```
WS frame -> websocket.ts (records + run timing via lib/notifications/runTiming.ts)
         -> notifications[]  -- pages read raw records (unchanged)
         -> lib/notifications/jobGrouping.ts   (root bundle: badge + retention need it)
              groupPanelItems(list)     -> PanelItem[] ('job' per file | 'single')
              countUnreadItems(list)    -> bell badge (unreadCount) + panel header
         -> lib/notifications/processingJob.ts (lazy, loads with the panel body)
              buildJobModel(item, {llmAvailable, now}) -> ProcessingJobModel
         -> lib/notifications/retention.ts prunes a file's records as ONE unit
```

Bundle: `NotificationsPanel.svelte` (mounted by the root layout) is only the shell; the body
loads with `import()` on first open. Measured on `HEAD` + only these changes: root layout node
20,095 B -> 16,633 B gzip, static closure 131,156 B -> 128,728 B.

Job member types (`JOB_TASK_BY_TYPE`): `youtube_processing_status -> import`,
`transcription_status -> transcription`, `redaction_status -> redaction`,
`summarization_status -> summary`, `topic_extraction_status -> topics`. A record is a member only
when it is non-silent and has a `data.file_id`; the group key is `job:<file_id>`.

### Run timing captured in the store (`runTiming.ts`, pure)

Added to `Notification`: `startedAt`, `finishedAt` (epoch ms), `stepLog: {p, at, stage?}[]` (one
entry per change of progress value or stage, capped at 40, first entry kept; a frame without a
stage keeps the previous entry's), and for transcription
`enrichmentStartedAt` + `enrichmentCompletedAt: Record<task, ms>` (replaces the old
`completedEnrichments: string[]`). A processing frame on a record that was terminal starts a new
run (timing and enrichment reset), which is how retry/reprocess are told apart from progress.

## Model (`processingJob.ts`)

Task states: `pending | running | done | failed | skipped`. Wire status mapping:
completed/success/complete/finished/done -> done; error/failed -> failed;
disabled/not_configured/skipped -> skipped; pending/queued -> pending; anything else -> running.

| Task | Chip | Weight | Percent source | Duration source |
|---|---|---|---|---|
| import | if a record exists | 10 | `progress.percentage` | observed `finishedAt - startedAt` |
| transcription | always | 60 | `progress.percentage` | server `duration_seconds`, else observed |
| diarization | always (derived) | 0 (inside transcription) | the `diarize` step | the `diarize` step |
| redaction | if a record exists | 5 | running = 50 | observed |
| enrichment ("Indexing and analysis") | once `enrichment_started` | 0 (never blocks done) | - | per sub-task: `completedAt - enrichmentStartedAt` |
| summary (LLM) | see LLM rule | 20 | `data.progress` | server, else observed |
| topics (LLM) | see LLM rule | 10 | running = 50 | server, else observed |

Overall percent = `round(sum(w*p) / sum(w))` over counted tasks (weight > 0, not skipped);
done/failed count as 100 so a finished-with-failure bar ends full. Clamped to 99 while the job is
not finished, forced to 100 when done.

**LLM rule.** A summary/topics task is counted when a record exists with a non-skip status, or
when `llmAvailable` (`$isLLMAvailable` from `stores/llmStatus.ts`) is true and transcription has
not failed and has not been finished for longer than `LLM_START_GRACE_MS` (3 min). So: no LLM
configured -> no LLM chips and 100% on transcription alone; LLM configured but the user's
automatic topic extraction is off -> the topic chip waits as "Waiting" for 3 minutes, then drops
out and the job completes. An event arriving later brings the chip back.

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

Job state: `failed` if any counted task failed; `done` if all counted tasks are done/skipped;
else `running`. The card is dismissible only when every member record is dismissible.

## UI states (`components/notifications/ProcessingJobCard.svelte`)

- Header: product logo, filename as the link to `/files/<id>` (marks every member read), state
  line (Processing / All tasks complete / A task failed + the failing task's message), relative
  time, and "Completed in X" when done with a known total.
- Primary bar: `role="progressbar"`, `aria-valuenow/min/max`, `aria-valuetext`, labelled by
  filename; percent text and ETA beside it. Shimmer only while running.
- Chip row: one chip per visible task, glyph + label, colour by state; accessible name
  "Task: State". Skipped tasks have no chip.
- Details toggle: `aria-expanded` + `aria-controls`, lists groups: Transcription (total, then
  steps), Indexing and analysis (each enrichment task), AI tasks (summary, topics), each with
  state and duration.
- Live region: `JobAnnouncer.svelte`, one polite `role="status"` region for the panel, speaks
  only a job's transition into done/failed, never progress ticks, and seeds silently on mount.
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

## Verification

`npx vitest run src/lib/notifications src/stores/websocket src/components/NotificationsPanel.test.ts src/components/notifications src/components/Navbar.notificationsMount.test.ts`,
`npm run check`, `npm run check:i18n`, `npm run build`. Visual: open the bell during a real
upload (light + dark, `ar` RTL), with and without an LLM configured, and a forced failure.
