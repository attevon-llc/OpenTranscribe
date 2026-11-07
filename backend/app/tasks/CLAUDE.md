# app/tasks — Celery tasks (the AI processing pipeline)

## Purpose

Task modules (`ls backend/app/tasks/*.py`, plus the `transcription/` package). The core flow: upload to MinIO → metadata extraction → DB record →
Celery dispatch → WhisperX transcription (100+ languages, word-level timestamps, optional
translate-to-English) → PyAnnote diarization + voice fingerprinting → optional LLM
speaker-ID suggestions (**never auto-applied — manual verification only**) → optional LLM
summarization (BLUF, multi-section stitching, 12 languages) → DB write + OpenSearch
indexing → WebSocket notification.

## Key files

- `transcription/` — the 3-stage chain (`preprocess.py` CPU, `core.py` / `cpu_task.py` transcription with inline speakers, `postprocess.py` CPU) run from the pipeline plan (#1225); `dispatch.py` routes `gpu_transcribe` / `cpu_transcribe` at call time, which is why they are absent from `task_routes`; the legal processing gate runs FIRST in `dispatch._run_pre_dispatch_gate`; voiceprint and gender stages are gated by the privacy policy (#1280) in three tiers; `hooks.py` is the cloud seam (exceptions are swallowed, hangs are not). Stage-by-stage detail: `backend/app/tasks/transcription/CLAUDE.md`.
- `speaker_tasks.py` — a **re-export shim, NOT dead code**. Keeps legacy task-name routing
  alive; must stay in `celery_app`'s `include=` list.
- `rename_propagation_task.py` — `propagate_speaker_rename` / `propagate_title_rename` (+ `rename_digest_task.py`: `regenerate_rename_digests`)
  (**cpu** queue, issue #405). Chunk docs snapshot the speaker display name and the file title
  at index time; renames used to touch only the speaker and full-document indices, so chat's
  speaker scope (an exact `terms` match on the CURRENT name) silently lost every pre-rename
  chunk. Dispatch through `dispatch_speaker_rename` — it coalesces `(file_uuid, old_name)` pairs
  per file — and **capture the old name before the overwrite**: after the commit, Postgres
  cannot say what the chunks were indexed with. Three rules the follow-up round added:
  - **Key on the CANONICAL indexed label,
    `app/utils/speaker_labels.py::canonical_speaker_label_for_row`** — the SAME resolver the
    chunk-index writers (`search_indexing_task`, `reindex_task`) call. Keying on `display_name`
    alone missed a *cleared* label (a legal `{"display_name": ""}`, which reverts to the
    diarizer label) and an edit to `name` alone; keying on the older `display_name or name`
    chain (removed in issue #605) additionally missed a confident VOICEPRINT
    `suggested_name` (`suggestion_source` `voice_match`/`profile_match`; an LLM one is never
    the label, see `app/utils/CLAUDE.md`) — the indexer trusts one at `confidence >= 0.75` and eight
    repair/propagation call sites kept computing the pre-suggestion value, so a rename
    computed the wrong `old_names`, the `update_by_query` matched nothing, and it logged
    `status: success` while the drift survived. `SpeakerRenameTracker.record`/`.flush` is the
    seam every writer of `suggested_name`/`confidence` must route through too, not just a
    `display_name` write — five writers previously dispatched nothing at all when a
    suggestion alone moved the canonical label.
  - **Both tasks re-resolve their target from Postgres at run time**, so two renames dispatched
    close together converge instead of inverting. `A→B` and `B→C` are unordered on an 8-way
    queue; if `B→C` ran first it matched nothing and `A→B` then wrote **B**, recreating #405's
    own bug. Pass `speaker_id` so the task can re-read.
  - **`version_conflicts` is read and retried.** `conflicts="proceed"` only means "do not abort
    the whole update_by_query"; nothing re-examines the skipped documents, so a concurrent
    title+speaker rename left a subset of chunks stale while reporting success.
  - Scope differs on purpose: **title covers the whole file plane** (a digest inherits `title`
    and renders it as a citation), **speaker covers the chunk plane only** (a digest has no
    `speaker` field and its prose bakes the name — regeneration, not rewriting, is the fix).
  - ⚠️ **A PROFILE rename reaches the chunk plane only through its member speakers**
    (issue #675). `SpeakerProfile.name` is indexed nowhere — the chunk writers resolve
    `canonical_speaker_label_for_row(speaker)` and never look at a profile — so re-applying
    the name to every member and dispatching are **one unit**:
    `services/speaker_profile_rename.apply_profile_name_to_speakers` is that unit's single
    implementation, shared by the **two** endpoints that rename a profile
    (`PUT /speakers/{uuid}` with `profile_action="update_profile"`, and
    `PUT /speaker-profiles/profiles/{uuid}` — the Speakers page's editor, which did neither
    half until #675). Dispatch without the rewrite writes a name Postgres does not hold and
    the next reindex reverts it; the rewrite without the dispatch is the #675 bug itself, and
    there is no repair path — `search_index_maintenance` only finds files with **no** chunks,
    so it cannot fix chunks holding a merely-wrong value. Dispatch carries **no**
    `speaker_id`: a profile-wide rename sweeps many speakers and has no single id to re-read.
    Covered by `tests/api/test_rename_propagation_dispatch.py::TestSpeakerProfileUpdateEndpoint`.
- `search_indexing_task.py` + `search_index_sweep_task.py` — a completed transcript must never
  silently miss the search index (#1182). The indexing task retries over
  `SEARCH_INDEX_RETRY_HORIZON_S` (exponential, capped, jittered; `services/search/index_retry.py`)
  at `EmbeddingPriority.PIPELINE_RETRY`, and keeps its `search_indexing` row `pending` (not
  `failed`) while retries remain. The beat sweep re-dispatches files whose latest row is `failed`
  or missing. Indexing writes go through `opensearch_service.client.call_idempotent_write` (own
  timeout + timeout retry; only for writes with an explicit `_id`) — never raise the client-wide
  timeout, which would also slow searches.
- `ingest_artifacts_task.py` — `artifacts.generate_file_facts` (**nlp** queue, #383 Phase 2).
  Builds the deterministic ingest artifacts (statistics, extractive digest with per-sentence
  provenance, keyphrases) and upserts `file_facts`. It rides the nlp pool because that is the
  CPU-only enrichment pool, **not** because it calls a provider: unlike every other task on
  that queue it must NOT return early when no LLM is configured — the no-LLM deployment is
  exactly who it exists for (#403 D6). Dispatched fire-and-forget from
  `transcription/enrich.enrich_and_dispatch`; logic lives in
  `services/ingest_artifacts/` (its own CLAUDE.md).
- `local_model_download.py` — `asr.download_local_model` (**download** queue, #1246): downloads one
  catalog Whisper model into the shared Hugging Face cache for the Settings model library. The
  task only wires Celery to `services/local_models/supervisor.py`: it reads `get_hf_token()` in
  the body (never a fork hook), and the supervisor runs `local_models.runner` as a **child
  process** it can stop, because `snapshot_download` has no cancel hook and `revoke(terminate=True)`
  would SIGTERM the whole prefork child with no chance to clean up or release the lock. Cancelling
  signals that CPU-only child — code managing its own child, not the operator rule against
  signalling CUDA processes. State, the deployment-wide single-download lock (`SET NX`, Lua
  compare-and-delete) and the cancel flag are Redis (`services/local_models/state.py`); progress
  is the `local_model_download` WebSocket event to the initiating admin. No `autoretry_for`,
  not `acks_late`: pressing Download again is the retry. Success is reported only after every
  expected blob is on disk at its size — `snapshot_download` writes `refs/main` before it
  downloads on a branch, so `is_complete` alone can be briefly true with files missing.
- **Cancellation, recovery and retries**: `transcription/cancellation.py` (what a user cancel does when the cooperative flag is missed), `recovery.py` / `recovery_tasks.py` (startup and periodic recovery), superseded task rows (#1185), ONE retry policy in `services/transcription_retry.py`, retries keep their place, and a cold shutdown hands the stage back rather than acking it. Detail: `backend/app/tasks/transcription/CLAUDE.md`.
- `erasure_reconciliation.py` — `gdpr.erasure_reconcile`, **utility** queue, daily 04:40.
  Finishes GDPR Art. 17 erasures that a legal hold deferred, and re-erases subjects a
  backup restore brought back. `takedown_service.release_file` calls its
  `notify_hold_released` for latency; **the schedule is the guarantee** — a hold can also
  be cleared by a DB edit or a restore, and a hook alone has a silent failure mode.
  Phases: 0 self-service requests that came due (`self_erasure_service.execute_due_requests`), 1-2
  open entries, 3 completed entries (user/organization row back; `org_member` re-run bounded by time),
  4 index-level check (`erasure_index_reconcile.run_index_phase`). Result keys: `self_erasure_due`,
  `self_erasure_executed`, `org_member_rerun`, `org_member_needs_review` (replaced
  `org_member_manual_review`), `index_resurrections_redeleted`, `index_resurrected_entries`,
  `index_unverified`. Design rationale (what the ledger must never store, why the org-member re-run is
  time-bounded) lives in `app/services/CLAUDE.md`.
- `legal_retention.py` — `legal.acceptance_retention_sweep`, **utility** queue, daily 05:10: deletes pseudonymised `legal_acceptance` rows past `retain_until` (v451) under `SET LOCAL opentranscribe.legal_retention_sweep = 'on'`, the only DELETE the guard trigger admits. Bounded at 500 per tick.
- `audit_retention.py` — `audit.retention_sweep`, **utility** queue, daily 05:15, no-op while `AUDIT_LOG_RETENTION_DAYS` is 0 (default; positive values must be >= 365, enforced at startup AND inside the `audit_prune` SQL function). Calls `audit_prune` for `auth_config_audit` and `support_access_use` only (SECURITY DEFINER; the only way the app role deletes audit rows). Ledger, takedown and grant rows are never pruned.
- `audit_outbox_relay.py` — `audit.outbox_relay`, **utility** queue, every 60 s (v459, issue #1329). Delivers pending `audit_outbox` rows (governance audit events staged in the change's own transaction) through `services/audit_outbox.relay`: claim with `FOR UPDATE SKIP LOCKED` as a short committed lease, deliver outside any transaction, record outcomes; the first sink failure ends the pass (backoff 30 s doubling to 1 h). Deletes `sent` rows after 7 days and logs the stuck count (pending > 15 min, or `rejected`) at ERROR; the API gauge is `audit_outbox_stuck_events`.
- `voiceprint_retention.py` — `privacy.voiceprint_retention_sweep`, **utility** queue, daily 05:25, off unless `privacy.voiceprint_retention_months` > 0. Selects due files (every linked profile stale by `last_interaction_at`, file older than the cutoff, not held, voiceprints present; at most 200) and dispatches `PurgeScope(kind="files")`; returns `unavailable` while this build's purge has no files scope. Stale profiles left empty are listed, never deleted.
- `privacy_purge_task.py` (beat half) — `privacy.purge_reverify`, **utility** queue, quarter-hourly at :11/:26/:41/:56 (v461, #1334). Takes `privacy_purge_pending` rows older than `purge_pending.REVERIFY_GRACE_SECONDS` (1 h): deletes and measures their cluster-centroid documents, drops a row once nothing is left, audits each row under the purge's own task id and scope (`phase: reverify`), and fails a purge whose task row is still pending/in progress with `reason: interrupted`. One indexed query when nothing is outstanding.
- `telemetry.py` — `telemetry.usage_report`, **utility** queue, hourly at :47 (#1019). The opt-in anonymous usage report: `services/telemetry.send_if_due` sends nothing unless a super admin turned it on AND `off_reasons()` is empty. It is a DOUBLE opt-in: the images ship `DO_NOT_TRACK=1` (kept on purpose, it also quiets any library honouring it), so an operator first sets `DO_NOT_TRACK=0`; only 0/false/no/off/empty read as unset (`DO_NOT_TRACK`, `OPENTRANSCRIBE_TELEMETRY` not a yes, `OPENTRANSCRIBE_DEMO_MODE`, no `OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY`), then one 5 s POST a day (+/- a per-install hour), never retried; the due check reads the last ATTEMPT so an unreachable receiver is tried daily, not hourly. Exactly four fields (`PAYLOAD_FIELDS`); adding one means bumping `TELEMETRY_SCHEMA_VERSION` and `docs-site/docs/operations/telemetry.md` together (`test_telemetry_report.py` compares them). Not replayable.
- `directory_sync_task.py` — LDAP reconciliation/deprovisioning, **cpu** queue.
  `directory.sync_check_schedule` runs from beat every 15 min, reads the DB-stored cron
  (`directory_sync.schedule`), and dispatches `directory.sync_run` when due — so changing the
  schedule needs **no beat restart**. It claims the window by stamping
  `directory_sync.last_run_at` *before* dispatch, and the run itself takes a Redis lock, so a
  double tick cannot start two passes. Policy and safety rules live in
  `services/directory_sync_service.py`; this module is only the scheduling shell.
- `account_lifecycle.py` — FedRAMP AC-2 account-inactivity expiration, **utility** queue.
  `account.inactivity_sweep` runs from beat daily (04:25), locked, no due-check split (unlike
  `directory_sync_task`'s admin-configurable interval, this sweep's cadence is fixed and it
  either runs or is a no-op via `ACCOUNT_EXPIRATION_ENABLED`). Policy and safety rules live in
  `services/account_lifecycle_service.py`; this module is only the scheduling shell.

## Conventions / patterns

- **A stage that completes a processing step records it, in its own write session** (#1229):
  `services/file_provenance.record_stage(db, file_id, "<step>", **facts)` (or one of its
  `record_*` wrappers). Current call sites: `transcription/storage.update_media_file_transcription_status`
  (`record_transcription_run`: new run + transcription + inline/parallel speaker detection,
  with the plan's engine/execution/merge passed down from `finalize._process_and_save_critical`),
  `rediarize_task._save` (`record_diarization`, same session as the re-split segments, with the
  plan's engine so a vendor's own id is never read as this server's engine),
  `transcription/postprocess._process_native_embeddings` and
  `transcription/embeddings._process_speaker_embeddings_native` (`record_inline_voiceprints`),
  `speaker_embedding_task._match_and_finalize_embeddings` (`voiceprints`, facts from the
  extractor's `Voiceprints`, incl. its `backend`),
  `summarization._persist_summary`, `TopicExtractionService._persist_extraction`,
  `speaker_identification_task._store_speaker_predictions` (the LLM facts are resolved in the
  no-session phase and handed to the write). A new or replacement stage must keep the call;
  `tests/unit/test_file_provenance_hooks.py` runs each site for real and
  `test_pipeline_execution_matrix.py::test_every_stage_of_a_run_records_what_it_ran` runs the
  chain per executable plan cell.

- **Every transcription progress frame names its sub-stage.** `transcription_status`
  processing frames carry `stage` (`prepare|transcribe|diarize|finalize|speakers`) from
  `core/enums.TranscriptionStage`, the one vocabulary the engine's `emit` also uses; the
  notification card renders steps from it, never from the percent. Pass
  `stage=TranscriptionStage.X` to every `send_progress_notification` call, and give an
  engine-driven progress closure a `stage` parameter (defaulting to its task's stage) so
  `adapt_legacy` forwards the engine's. `tests/unit/test_transcription_stage_wire.py` lists
  every call site with its stage and fails on a new one without a row; the mapping
  (including `rediarize_task`'s diarize -> finalize -> speakers) is in
  `docs/design/consolidated_processing_notification_plan.md`.

- **Every per-file task that runs after the transcript is committed reports its state through
  `services/post_processing_status.py`, never an ad-hoc event.** Decorate the task with
  `@tracks_post_processing(PostProcessingTask.X, file_param=...)` (start + outcome from the
  returned `{"status": ...}`; a `Retry` reports nothing), use `reporting_step()` for a step inside
  another task, report `QUEUED`/`SKIPPED` where you decide whether to dispatch it (the enrichment
  fan-out does this as one plan), and report a hand-off BEFORE the parent's own outcome. A task
  that returns `success` when it did nothing must return `skipped` instead (the summary's no-LLM
  path does). That is what lets the notification card say "Complete" truthfully; the old
  `enrichment_task_complete` reported search indexing done at dispatch time. Keys:
  `core/enums.PostProcessingTask`; a new key also needs a card tier (frontend
  `POST_PROCESSING_TIERS`), pinned by `tests/unit/test_post_processing_status.py`.
- **Any WebSocket event naming a `MediaFile` must go through
  `app/utils/websocket_notify.py:send_ws_event_for_file`, never the raw `send_ws_event`**
  (issue #908). `send_ws_event` has no notion of quarantine at all — a filename, title, or
  speaker-display-name event pushed with it can disclose a taken-down file to a non-admin
  recipient even though the file already 404s on every read surface. The wrapper consults
  `file_availability.is_notification_suppressed` (by file id), `_for_uuid` (by UUID — most
  tasks here hold only a UUID), or `filter_suppressed_file_uuids` (a multi-file event); pass
  exactly one of `file_id=`/`file_uuid=`/`file_uuids=` or it raises `TypeError`.
  `tests/unit/test_ws_event_quarantine_discipline.py` is the structural gate — an
  allowlist-with-written-reason covers the small set of genuinely corpus-wide events
  (admin-migration progress counters, cache invalidation) that carry no single file's identity.
- **A task that re-reads a `MediaFile` by id must re-apply the quarantine gate.**
  `media_download.py`'s two tasks are the worked examples (issue #818):
  `prepare_media_download_task` wraps its file read in `file_availability.exclude_unavailable`,
  and `prepare_bulk_subtitles_task` passes `include_quarantined` through to
  `SubtitleService.build_subtitle_archive`, which skips a taken-down file rather than
  exporting it. Both re-resolve the admin bypass at RUN time via
  `takedown_service.is_review_admin(db, user_id)` — a task holds a user id, never a `User`,
  and a role captured at dispatch cannot reflect a privilege change made in the gap.
- **`transcript_document.py` (`download.prepare_transcript_document`, #1278)** renders one
  transcript to DOCX or PDF on the `download` queue. `max_retries=0` and a 300 s soft limit
  (`DOCUMENT_EXPORT_SOFT_TIME_LIMIT_S`): a retry would re-run an audited, policy-resolving render, so
  a failure is cached (`bulk_export_result:{job}`) and published once with a `reason` slug from
  `download_events.DOCUMENT_EXPORT_ERRORS`. It holds a short DB session only for
  `transcript_document.build.prepare_document` (which resolves the redaction policy from a
  `user_id` at run time and re-checks quarantine and scan state), then renders with no session
  open, because a long PDF takes about a minute. Result delivery rides the existing bulk-export SSE
  channel; the object lands under `bulk/` (1-day lifecycle).
- Queues (`core/queues.py:CeleryQueues`): `gpu`, `cpu`, `download`, `nlp`, `embedding`,
  `utility`, `redaction`, plus dynamic `cloud-asr`, `cpu-transcribe`, `gpu-transcribe`,
  `gpu-diarize`. **`task_create_missing_queues=False`** — a queue-name typo raises at
  dispatch instead of creating a phantom queue. **Every registered task needs a
  `task_routes` entry**, or an entry in `celery_routing.py:INTENTIONALLY_UNROUTED_TASKS` with a
  written reason (call-time routing only) — `tests/unit/test_task_routes_complete.py` fails
  otherwise (issue #1268); `celery._validate_task_routes()` logs the same check at worker startup.
  **Never put `queue=` in a task decorator**: Celery merges the route UNDER the task's own
  options, so a decorator queue silently overrides `task_routes` (the same test forbids it).
- ⚠️ **A valid route is not a drained route, and `--lite` is where they diverge.**
  `docker-compose.lite.yml` scales BOTH `gpu` consumers (`celery-worker`,
  `celery-worker-gpu-scaled`) to `replicas: 0`, so anything pinned to `gpu` there is
  published into a queue nothing drains — no error, no retry, and the API has already
  answered 200 (issue #865, and issue #584 for one earlier instance of the same shape).
  The eight speaker/diarization tasks that PREFER a GPU but are correct without one are
  listed once as `celery_routing.py:GPU_PREFERRED_TASKS` and routed through
  `queues.gpu_preferred_queue()`, which resolves to `cpu` when `DEPLOYMENT_MODE=lite`
  (the lite overlay sets that on **every** service, and routing is decided by the
  publisher). The reroute costs only speed — `requirements-lite.txt` deliberately keeps
  `torch+cpu`/`torchaudio`/`pyannote.audio`/`onnxruntime`.
  The post stage is published with an explicit queue (`pipeline_plan.post_stage_queue`),
  so a cloud detector lands on `cloud-asr`, which lite staffs. **Lite has no local speaker
  detection and no local transcription (#1228)**: the plan never produces `inline_local` /
  `post_stage_local` there (a stored local choice plans `execution: none` with its blocking
  issue), dispatch refuses it with the fixed sentence before any task row, `post_stage_queue`
  refuses this server's engine instead of resolving to `cpu` (the old rediarize-on-CPU
  route), and `rediarize_task(engine="local")` / `transcription.cpu_transcribe` refuse at
  their top as defence in depth. Voiceprints still run on `cpu` (`embedding_stage`) after
  every cloud detector; `rediarize` stays in `GPU_PREFERRED_TASKS` for the full image only.
  `tests/unit/test_lite_dispatch_refusals.py` pins the dispatch half.
  `tests/unit/test_lite_mode_queue_consumers.py` derives the lite consumer set
  from the compose files and fails on any new route into a dead queue; an exemption
  there requires a written reason.
  This is deliberately NOT the live `inspect().active_queues()` probe that
  `transcription/queue_consumers.py` uses — that costs a broker broadcast on interactive
  request paths, and a full deployment whose GPU worker is momentarily down is an
  outage where queueing is the *correct* behaviour, not a case for silent CPU
  downgrade.
- Priorities are **per-queue** (`GPUPriority.X` is unrelated to `CPUPriority.X`); the scheme
  is documented in the comment block above `task_queues` in `core/celery.py`.
- ⚠️ **Not every worker runs `--pool=threads`, and assuming so cost issue #631 an entire
  investigation.** Only the GPU family (`GPU_WORKER_POOL`), `celery-redaction`
  (`REDACTION_WORKER_POOL`) and the gpu-scale worker (`GPU_SCALE_POOL`) default to threads.
  **Five services pass no `--pool=` flag and are therefore PREFORK** —
  `celery-download-worker`, `celery-cpu-worker`, `celery-cloud-asr-worker`,
  `celery-nlp-worker`, `celery-embedding-worker` — so `worker_process_init` **does** fire on
  them, once per forked child. Logging is still wired via `setup_logging` (which covers both
  pool types); `worker_process_init` is not where you put logging setup.
  **Anything you add to `core/celery_worker_lifecycle.py:init_worker_process` runs inside a fork against
  `worker_proc_alive_timeout`** — overrun it and the child is SIGKILLed before it signals UP,
  its replacement runs the same code, and the worker enters a fork/kill loop that consumes
  zero tasks (69,231 kills over 10 h 46 m, issue #631). No network, no locks, no model loads
  in there.
  **Every** worker's container healthcheck now runs `backend/scripts/celery_pool_healthcheck.py`
  (`inspect ping` is answered by MainProcess and cannot see a wedged pool). It reads
  `pool.implementation` and adapts: a threads pool passes on the reply alone, a prefork pool
  must also have children **and** must not show a fully-replaced pid set with a frozen
  accepted-task counter — that pair is the storm signature, and it is why "the child list is
  non-empty" was not enough (billiard appends a child to the pool *before* `start()`, so a
  pool killing children faster than they can signal UP always has pids to report).
  ⚠️ Nothing in the compose set acts on `unhealthy`: there is no autoheal/watchtower container
  and `restart: always` fires only when the **main** process exits. The check makes the wedge
  visible; it does not recover from it.
- **`Timed out waiting for UP message` covers RESPAWNS ONLY — its absence proves nothing.**
  celery arms that kill timer from `on_process_up` via `hub.call_later`, so it exists only once
  the worker has a hub: the children forked when the pool is **first populated** predate it, and
  a hub blocked inside a long callback cannot fire it either. Anything slow on `worker_ready`
  runs in the MainProcess main thread and blocks exactly that loop — which is why
  `preload_models`' model loads are bounded (`_CPU_WHISPER_PRELOAD_TIMEOUT_S`, and the
  `hf_hub_offline.load_with_timeout` wrappers), not just for tidiness. When a worker looks
  wedged with **no** such line in its logs, read `init_worker_process`'s `fork init
  started`/`fork init finished` pairs instead — those are emitted from the child and do not
  depend on the parent's loop.
- `request_id` is stamped on task headers at publish and cleared in `task_postrun`; never
  assume a ContextVar survives into the next task.
- **NEVER hold a DB session across slow non-DB work.** Three phases, always: a short read
  session returning **plain data**, the slow work with **no session open**, then a short
  write session. This is the single most repeated defect in this package — see below.

## The session-lifetime rule (read before writing any task)

A task that wraps its whole body in one `with session_scope() as db:` and then does MinIO,
ffmpeg, a model load, an LLM call, SMTP or OpenSearch **inside** it holds an open transaction
for the duration. `session_scope` is not at fault; it simply never gets to exit.

**This has wedged the live database twice, on two different workers, in one day.** The CPU
worker sat `idle in transaction` for 48 minutes (a 3-hour task, killed by the hard time
limit); the NLP worker for 1h26m. Both on a full-entity `transcript_segment` SELECT.

Why it matters beyond the hang — a plain SELECT takes `ACCESS SHARE` for the life of the
transaction, so:

1. **It queues `ALTER TABLE`.** That is an Alembic upgrade hanging mid-release, and dev runs
   migrations automatically on backend startup. It is how the bug was found: DDL migration
   tests started failing with `psycopg2.errors.LockNotAvailable`.
2. **It pins the VACUUM horizon** on `transcript_segment`, the largest table in the product.
3. **It consumes a pool connection permanently.**

**The fix pattern** (`speaker_attribute_task.py` is the worked example — read it):

- Read phase returns **plain data, never ORM instances**. An instance escaping the session can
  lazy-load later and silently reopen a transaction, reintroducing the bug invisibly.
- Narrow the query to the columns you use, and `outerjoin` rather than letting `seg.speaker`
  lazy-load per row.
- Where a callee takes `db` and does the slow work itself, change it to take the data — or to
  open its own short session. Passing `db` down is how the idiom spreads.
- Watch for objects that **hold** an ORM row (`LocalWatchClient` did) and for ORM attribute
  reads after scope exit. `db.expunge()` turns a silent lazy load into a loud
  `DetachedInstanceError`.

**`ThreadPoolExecutor` does not bound anything.** `__exit__` calls `shutdown(wait=True)` with
no timeout, so a per-future `result(timeout=30)` is decorative — one wedged child holds the
block, and the transaction, indefinitely.

**The gate: `scripts/audit-session-lifetime.py`.** Nine AST detectors (subprocess/ffmpeg,
object storage, OpenSearch, HTTP, LLM, model load, SMTP, thread pool, plus the
interprocedural one), an allowlist at `scripts/session-lifetime-allowlist.txt` keyed
`<file>::<scope>::<category>` with a **mandatory** reason, count-aware so one line buys one
finding, and a **stale entry fails the run** — the file can only shrink. `--selftest` after
touching a detector; `backend/tests/unit/test_session_lifetime_audit.py` runs the same cases
under pytest *and* mutation-checks every rule, because a detector that matches nothing
reports zero findings and reads exactly like a clean codebase.

> **The interprocedural rule exists because a body-scan is not enough.** `scan_single`'s
> `session_scope` wrapped `_perform_scan(db, ...)`; the remote listing, the per-file download
> and the MinIO upload were all one frame further down, so the first AST sweep — which only
> looked *inside* `with` bodies — reported it clean. The rule therefore also flags any
> function that both **accepts a `Session`** and does slow work, whichever end the leak is at.

**Testing it**: the `tests/unit/test_*_session_lifetime.py` modules (one per task family, e.g.
`test_llm_task_session_lifetime.py`, `test_media_task_session_lifetime.py`; shared stand-ins in
`_session_lifetime_trackers.py`) swap the module's `session_scope`
for a depth-tracking stand-in and has each slow-call stub report the open-scope depth at the
moment it runs. Assert `>= 2` scopes opened, so a task that never touches the DB cannot pass.
A structural "does it call session_scope" test is not enough.

## How it connects

- `dispatch_upload_pipeline` — the shared post-upload tail — lives in
  **`app/api/endpoints/files/upload_dispatch.py`**, not here. `services/watch_sources/processing.py`
  calls it so auto-import and manual upload share one path.
- Boundary smoothing is applied in `transcription/core.py` at the `finalize_segments()`
  chokepoint (`app/utils/segment_postprocess.py`). Deep detail: `app/transcription/CLAUDE.md`.
- Watch-source tasks: `watch_source_tasks.py`; deep detail in
  `app/services/watch_sources/CLAUDE.md`.

## Gotchas

- **`visibility_timeout` is set in `core/celery.py`'s `broker_transport_options`** —
  `CELERY_VISIBILITY_TIMEOUT` (default `21600`, 6h), not the Redis broker's 3600s default. This
  used to be unset, so `acks_late=True` transcription tasks (`core.py`, `preprocess.py`,
  `postprocess.py`) running past one hour were redelivered to another worker and transcribed
  twice — fixed, single source of truth is `core/celery.py`. Raising a file-length limit still
  needs this value to stay above the longest job it enables. It is NOT how a dead worker's
  stage comes back any more (the orphan reaper does that in minutes) — so never lower it to
  speed recovery up; it applies to every acks_late task, alive or not.
- **Model loading is per-worker and must not leak across queues.** `PRELOAD_GPU_MODELS=true`
  only on the GPU workers; `PRELOAD_REDACTION_MODELS=true` only on `celery-redaction`
  (dedicated CPU service owning the `redaction` queue, run under `nice`). Importing a
  model-loading module into a task that runs on the wrong queue wastes 15+ GB of VRAM.
- Redaction detection is **not** unconditional: `_dispatch_redaction` returns early unless
  `resolve_effective_config(db, user_id).enabled`, and redaction is opt-out by default.
  Enabling redaction later triggers lazy detection on first file open. (Both the
  `postprocess.py` comment and the `redaction_task.py` docstring claimed "always" until #296.)
- Multi-GPU scaling: `./opentr.sh start dev --gpu-scale` sets `COMPOSE_PROFILES=gpu-scale`
  and loads `docker-compose.gpu-scale.yml` (N threads on `GPU_SCALE_DEVICE_ID`, tune
  `GPU_SCALE_WORKERS` to VRAM). **`GPU_SCALE_ENABLED` does not enable scaling** — only the
  `--gpu-scale` flag does; no compose file or startup script reads the variable. It *is* read
  in exactly one place, `tasks/utility.py` (`env_bool("GPU_SCALE_ENABLED")`), to pick which GPU device IDs the
  system-stats task queries — so a stale value misreports which GPU is in use without
  changing any scheduling. Also note
  `GPU_SCALE_DEFAULT_WORKER` defaults to `0` in compose (default GPU worker disabled) but
  `1` in `.env.example` (both GPUs transcribe) — check which you actually have.
- **Flower's `/api/workers` is a boot-time snapshot, not a live roster (issue #609).**
  `Flower.start()` issues exactly ONE `celery inspect` broadcast at process startup with a 1 s
  reply timeout and never re-inspects on a timer. A GPU worker that was still importing
  torch/whisperx, or running `@worker_ready preload_models()` synchronously in its own main
  thread (`PRELOAD_GPU_MODELS`, above), when Flower booted is absent from that endpoint
  **forever** — waiting and re-checking cannot help. **`--pool=threads` workers are NOT
  invisible to Celery** — an `inspect` broadcast reaches them fine, which is what their
  container healthchecks rely on; two prefork workers (`cpu-processor`, `cloud-asr`) lose the
  identical race, so the pool type is not the cause. Always read
  `/api/workers?refresh=1` (Flower's own documented parameter, awaited server-side), never the
  unrefreshed endpoint — `scripts/gpu-scale-smoke.sh` and `scripts/bulk-processing-cheatsheet.sh`
  both do this. `docker-compose.yml`'s flower `command:` also raises `--inspect_timeout` to 10 s
  so a refresh survives a GIL-bound worker.
