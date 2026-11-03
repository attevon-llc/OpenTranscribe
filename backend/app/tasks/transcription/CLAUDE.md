# tasks/transcription — the 3-stage chain, gates, and failure handling

Moved intact from `backend/app/tasks/CLAUDE.md` ("Key files"). The session-lifetime rule and queue wiring stay in `backend/app/tasks/CLAUDE.md`; engine detail: `backend/app/transcription/CLAUDE.md`.

- `transcription/` — the 3-stage chain: `preprocess.py` (CPU: download, ffmpeg, MinIO temp)
  → `core.py` / `cpu_task.py` (transcription + inline speakers + DB save) → `postprocess.py`
  (CPU: voiceprints, indexing, downstream dispatch). `dispatch.py` orchestrates the chain and
  routes `transcription.gpu_transcribe` / `cpu_transcribe` **at call time** — that's why
  they're deliberately absent from `task_routes`. The legacy single-task
  `transcription.process_file` path (`legacy_task.py`, `background.py`) is deleted (#1225).
- **Every stage runs from the pipeline plan (#1225).** `dispatch._plan_for_dispatch` resolves
  `services/pipeline_plan` ONCE (`plan_for_user`), refuses a blocking issue before any publish
  (file ERROR, `error_category` from the fixed sentence, `PipelinePlanRefusedError`), and puts
  `plan.to_context()` in the chain context as `pipeline_plan`; stages read it with
  `transcription/context.plan_of`. The transcription link and its queue are the plan's
  (`_transcribe_stage`); `gpu-transcribe` (split) only for inline speakers. Postprocess hands
  speaker detection to the **post stage** (`rediarize_task`, the plan's engine / merge /
  queue: `cloud-asr` for a vendor, `gpu_preferred_queue()` for this server) whenever
  `execution` is `post_stage_*`; otherwise it runs the voiceprint step itself
  (`inline_native`) or dispatches it (`embedding_stage`, CPU). **One metering terminus per
  run**: postprocess when nothing was handed off, the post stage when it ran, the embedding
  stage for `embedding_stage` runs (`closes_pipeline`; the post stage dispatches it with
  `closes_pipeline=False`). Pinned cell by cell in `tests/unit/test_pipeline_execution_matrix.py`.
- **Voiceprint and gender stages are gated by the privacy policy (#1280), in three tiers.**
  (1) The plan: `PlanInputs.voiceprints_allowed` (dispatch passes the FILE's organization and id, from
  its row) makes `speaker_id.execution` `none` plus an info issue `voiceprints_disabled`; the per-job
  `TranscriptionConfig` gets `enable_native_embeddings=False` / `request_gender=False` from
  `services/privacy/gates.job_privacy_overrides` (a restriction only: an allowed job keeps the
  deployment's own defaults). (2) Every stage that produces or consumes a voiceprint asks again with a
  fresh read, `services/privacy/gates.voiceprints_allowed(file_id)`: the postprocess inline match,
  v4 staging and embedding-stage dispatch, `finalize._centroids_for_chain`, the embeddings helpers, the
  voiceprint stage task (which still completes and meters the run), the reassignment update and the post
  stage. A denied stage returns "skipped by policy", never an error. (3) The OpenSearch write
  functions refuse a forbidden scope (`assert_voiceprint_write_allowed`). ⚠️ **The per-job answers
  travel as PER-CALL arguments** (`diarize(..., return_centroids=, request_gender=)` via
  `_run_diarize(tc=)`): the ModelManager shares ONE diarizer, and the config it was built with, across
  every job on a worker, so a per-job value left on `diarizer.config` is some earlier job's. A new
  stage that touches a voiceprint calls the gate first; a gate that cannot read the file denies.
- `privacy_purge_task.py` — `privacy.purge_voiceprints` (**cpu** queue, `include=` and `task_routes` both registered, #1280).
  Deletes the voiceprints and/or unconfirmed inferred gender a privacy switch only hides, for a `PurgeScope` the route built
  from the request context. Two passes under ONE task id: pass 1 now, pass 2 `SECOND_PASS_DELAY_SECONDS` (60 s, longer than
  the 10 s write-backstop cache) later, both idempotent; the `Task` row (`task_type="privacy_purge"`) stays `in_progress` until
  pass 2 ends and ends `failed` if a store could not be fully purged. It re-checks `purge.targets_still_enabled` when it
  starts and aborts (`reason: policy_enabled`) if the layer was switched back on. Counts live in a Redis record read by
  `GET /privacy/purge/{task_id}`; audit events carry counts only. Opens a short session per relational step and none during
  the OpenSearch delete.
- `rediarize_task.py` — the post stage AND the user's manual "re-detect speakers" (which
  plans with the file owner's current choice and never meters). Read phase → detector with
  no session open → `finalize_segments` re-split → write phase → voiceprint stage once
  (inline with this server's vectors, else `extract_speaker_embeddings_task` on `cpu`). A
  vendor failure is non-fatal: transcript kept, `CLOUD_DIARIZATION_FAILED_MESSAGE` recorded.
- `services/voiceprint_extractor.py` (used by `speaker_embedding_task.py`) — the voiceprint
  stage's ONE interface: `extract(audio_path, turns) -> Voiceprints` (per-speaker 256-d
  WeSpeaker vectors). `get_voiceprint_extractor()` is the seam the Rust implementation
  (#1231) replaces.
- `transcription/requested_options.py` — the per-file request (Whisper model, speaker range,
  "skip diarization") stored on `media_file.requested_*` by every fresh dispatch and replayed by
  `dispatch_transcription_pipeline(..., reuse_requested_options=True)` — which retry, recovery and
  the download hop of a URL import pass. A fresh request overwrites the stored one even with
  `None`. Speaker-range precedence is decided once, in `user_settings.resolve_speaker_range`
  (per file, then the user's saved range, then env); never re-derive it at a call site.
- `transcription/hooks.py` — cloud-edition seam. Exceptions are swallowed; **hangs are not**
  (hooks run inside the completion path holding an open `session_scope`). Any outbound call
  in a hook needs a tight timeout.
- **The legal processing gate runs FIRST in `dispatch._run_pre_dispatch_gate`** (before the
  duration ceiling and before any before-dispatch hook): `app.legal.processing_gate` checks the
  FILE OWNER's acceptance of the documents `legal.processing_required_document_keys` names. A
  refusal marks the file ERROR with `error_category = legal_acceptance_required` (permanent:
  `check_for_stuck_files` never re-selects it) and raises `LegalAcceptanceRequiredError` (a
  `DispatchBlockedError`, dict `detail`). Tasks that start processing without going through
  dispatch (AI steps, manual rediarize, URL imports, watch scans) call the gate in their own body
  and return `skipped`. `tests/unit/test_processing_gate_coverage.py` derives the processing tasks
  from `task_routes` (nlp/redaction/download queues, `GPU_PREFERRED_TASKS`, the call-time-routed
  chain links): a new one fails until it calls the gate or is exempted with a reason.

- `transcription/cancellation.py` — what a **user cancel** does when the cooperative
  checkpoint fires (#823). It is deliberately NOT `context.requeue_after_abort`:

  | trigger | outcome | why |
  |---|---|---|
  | worker shutdown (#809) | `Reject(requeue=True)` — run it elsewhere | the work is still wanted |
  | user cancel (#823) | `finish_cancelled` — return, ack, stop for good | the user asked it to stop |

  Requeueing a cancelled job resurrects it on the next worker, so the UI says "Cancelled" and
  the file goes back to processing. `finish_cancelled` **returns** a
  `{"status": "cancelled", ...}` dict — that normal return is what acks under `acks_late=True`
  — and `postprocess.finalize_transcription` recognises it and no-ops (cleaning up the temp
  audio). It never travels `_handle_transcription_failure`: a cancel is not a failure.
  - ⚠️ **Two-phase status, and `FileStatus.CANCELLING` is the honest middle.** The API
    (`utils/task_cancel.cancel_active_task`) arms the flag and writes `CANCELLING` — *we asked*;
    only `finish_cancelled` writes `CANCELLED` — *it actually stopped*, with `Task.completed_at`
    as the timestamp. It used to write `CANCELLED` optimistically while the GPU carried on.
    `CANCELLING` already existed in `core/enums.FileStatus`, the API filter list,
    `formatting_service` and the frontend status union; nothing had ever written it.
  - `active_task_id` is **retained** while `CANCELLING` — it is the handle both the worker's
    confirmation and `transcription.reconcile_cancellation` key on.
  - `reconcile_cancellation` (utility queue, `countdown=CANCEL_RECONCILE_DELAY_S`) stops
    `CANCELLING` wedging when no checkpoint can ever fire (worker died; or on `--lite`, a run
    published into a queue nothing drains). It resolves to `CANCELLED` **unconfirmed** and logs
    at WARNING — a bounded, logged version of the pre-#823 behaviour, not a guarantee. It
    deliberately does **not** clear the flag: a stage still running must stand down at its next
    checkpoint rather than finish and mark the file COMPLETED.
  - Both `finish_cancelled` and `reconcile_cancellation` re-check ownership
    (`_owns_the_file`). `POST /files/{uuid}/reprocess` cancels and immediately re-dispatches,
    so the old run's checkpoint can fire minutes later against a file a NEW run already owns.
  - ⚠️ **`revoke(active_task_id, terminate=True)` is gone, and it was dead on every pool type.**
    Not just `--pool=threads`: `dispatch.py` calls `pipeline.apply_async()` with no `task_id=`,
    so celery mints its own ids while `active_task_id` holds the separate application `uuid4`.
    The id being revoked had never belonged to a celery message anywhere. Don't "restore" it.
- `recovery.py` / `recovery_tasks.py` — `system.startup_recovery` and the periodic
  `cleanup.health_check` reclaim files stuck in PROCESSING with no live Celery task.
  ⚠️ **A transcription's Task row is created at DISPATCH, so neither its age nor "last
  touched before this process started" says it is dead** (issue #1020 — both failed or
  duplicated runs that were only waiting for a GPU worker). Its liveness is
  `core/task_liveness.py`: a queued marker (set by `dispatch.py`, re-armed when a stage
  returns) and a heartbeat (`run_heartbeat`, wrapped around every executing stage). Only a
  run with neither is recovered, an unreadable Redis means "unknown, do nothing", and the
  duration budget runs from the heartbeat's start. Any code that fails a transcription on
  recovery's behalf calls `supersede_run`, and every stage calls
  `transcription/run_ownership.py` first, so a stale message stands down instead of
  resurrecting its failed row. Other task types create their row when they start and keep
  the older checks.
  **Every** pipeline stage (preprocess, GPU/CPU transcribe, diarize, finalize) holds the run's
  lease (`run_heartbeat`) for its whole body — a stage without one is invisible to recovery.
  ⚠️ A run younger than `TRANSCRIPTION_RECLAIM_GRACE_SECONDS` is never reclaimed
  (`transcription_retry.recover_lost_run` is the one choke point): dispatch commits the row
  before it writes the queued marker, and that window once got a run reclaimed 0.1 s after
  creation (issue #1178). And `update_task_status` drops a `pending`/`in_progress` write to a
  transcription row that is no longer active, so a superseded stage's late progress write can
  never resurrect a reclaimed row. Other task types are exempt: they `self.retry()` under the
  same id and must be able to reopen their own failed row.
- **Superseded task rows (issue #1185).** `system.health_check` runs
  `services/task_row_supersede.resolve_superseded_task_rows` FIRST (before stuck-task recovery, so that
  step never "recovers" a step that already finished): an active row with a NEWER `completed` row of the
  same `task_type` and `media_file_id` is failed as `superseded by <id>`, counted in
  `task_rows_superseded_total{task_type}`, and clears a `MediaFile.active_task_id` that still names it. A
  batch of 500 per run; a sweep failure is logged and recovery carries on. The same module's predicate
  feeds the `task_rows_superseded_active` gauge, and `v448` makes a second active row per file and step
  impossible: `create_task_record` supersedes the older one when a step starts again. Don't write a second
  definition of "superseded" (the migration repeats the rule in SQL; keep them in step).
- **ONE retry policy: `services/transcription_retry.py`.** Every way a run ends early goes
  through it — a stage that raised (`_handle_transcription_failure`, preprocess's
  `_mark_pipeline_error`), a worker that died (`core/broker_orphans.py` reaper, and the health
  check's `recover_stuck_task`). Don't add a parallel path. Two classes:
  **permanent** (`ErrorCategory.INVALID_MEDIA` — set by `classify_failure` from an input-shaped
  user reason; `CONFIGURATION_REQUIRED` — the user's own settings make the run impossible, e.g.
  pyannote.ai diarization with no key, #1204; also `PRIVATE_OR_REMOVED`, `FILE_TOO_LARGE`,
  `USER_CANCELLED`, `RETRIES_EXHAUSTED`) fails fast, never retried; **transient** (everything in
  `RETRIABLE_CATEGORIES`, including UNKNOWN) is requeued with backoff + jitter until the admin
  retry limit, then ERROR with `RETRIES_EXHAUSTED`. Worker loss spends a SEPARATE per-file
  budget (`TRANSCRIPTION_MAX_INFRA_REQUEUES`, a Redis sorted set), never `retry_count`.
  ⚠️ Never fail a transcription Task row with `update_task_status` on a recovery path: its
  aggregate turned the still-PROCESSING file ERROR (or COMPLETED, with an older completed
  sibling task) before any retry branch could see it — that is how worker restarts under load
  ended 84 of 418 files in ERROR. `transcription_retry._retire_run` sets the row directly.
- **Retries keep their place.** `dispatch_transcription_pipeline(retry=True)` publishes every
  stage at `CPUPriority.PIPELINE_RETRY` / `GPUPriority.TRANSCRIPTION_RETRY` (ahead of new
  submissions); the reaper and `requeue_after_abort` restore the exact stage to the HEAD of
  its priority list. A plain `Reject(requeue=True)` LPUSHes to the BACK — it is only the
  fallback when the message can't be found.
- **A cold shutdown (`SystemExit` in the child) hands the stage back; it does not ack it.** billiard
  >= 4.3.1 (pinned) reports the job lost, so Celery requeues it to the head of its queue and the
  reaper leaves a run that is waiting on a queue list alone (`broker_orphans.read_queued_run_ids`).
  billiard 4.3.0 acked it and dropped the message. `task_liveness.run_heartbeat` therefore marks a
  run queued only on a normal return, `Retry` or `Reject(requeue=True)`, never on a teardown
  exception. Detection of a dead worker's stage takes `BROKER_ORPHAN_STALE_SECONDS` (120) plus up
  to one `BROKER_ORPHAN_SWEEP_INTERVAL_SECONDS` (60) sweep. Pinned by
  `unit/test_billiard_shutdown_is_not_acked.py`.
