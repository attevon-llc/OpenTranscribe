<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # Background Tasks Documentation
</div>

This directory contains the Celery tasks that do the application's long-running work: the transcription pipeline, speaker analysis, search indexing, LLM enrichment, downloads, redaction, maintenance and recovery. Tasks run asynchronously so API requests never block on them. Subsystem rules and hard-won gotchas are in `app/tasks/CLAUDE.md`; read it before changing the pipeline.

## ⚡ Task System Overview

### Architecture
```
API Request → Task Dispatch → Celery Worker → AI Processing → Database Update → WebSocket Notification
     ↓              ↓              ↓               ↓                ↓                    ↓
  HTTP Layer    Redis queues   Background     AI/ML Models    Data Storage      Real-time UI
```

### Core Technologies
- **Celery** with a **Redis** broker; **Flower** for monitoring (http://localhost:5175/flower)
- **WhisperX / faster-whisper** for transcription and **diar-native / PyAnnote** for diarization (engine code in `app/transcription/`)
- **Multi-provider LLMs** for summarization, topics and speaker suggestions (`app/services/llm_service.py`)
- **FFmpeg / ffprobe / ExifTool** for media processing

## Task Structure

`app/tasks/` has roughly 50 modules, grouped by concern:

```
tasks/
├── transcription/                    # The processing pipeline (see below)
├── Speakers
│   ├── speaker_embedding_task.py            # Voiceprint extraction and reassignment updates
│   ├── speaker_identification_task.py       # LLM name suggestions (never auto-applied)
│   ├── speaker_update_task.py, speaker_merge_task.py   # Background speaker updates and merges
│   ├── speaker_attribute_task.py, speaker_attribute_migration_task.py, combined_speaker_analysis_task.py
│   ├── speaker_clustering.py, rename_propagation_task.py, rediarize_task.py
│   ├── speaker_embedding_consistency.py, embedding_consistency_repair.py
│   ├── embedding_migration_v4.py, speaker_embedding_migration.py, migration_pipeline.py
│   └── speaker_tasks.py                     # Re-export shim; keeps legacy task names alive (no logic here)
├── Search
│   ├── search_indexing_task.py              # Index a completed transcript (retries over a horizon)
│   ├── search_index_sweep_task.py           # Beat sweep re-dispatching files missing from search
│   ├── search_maintenance_task.py, search_reembed_task.py, reindex_task.py, opensearch_integrity_task.py, tenant_backfill_task.py
├── LLM enrichment
│   ├── summarization.py, summary_retry.py   # ai.generate_summary and retry
│   ├── topic_extraction.py, auto_labeling.py
│   └── ingest_artifacts_task.py             # Deterministic (no-LLM) facts / digest / keyphrases
├── Media
│   ├── media_download.py, youtube_processing.py, transcript_document.py   # Downloads, yt-dlp, DOCX/PDF export
│   ├── thumbnail.py, thumbnail_migration.py, waveform.py, waveform_generation.py, playback_rendition.py
│   └── imohash_recompute.py, local_model_download.py
├── Governance and compliance
│   ├── redaction_task.py                    # Content-redaction detection (redaction queue)
│   ├── erasure_reconciliation.py, account_lifecycle.py, session_cap.py, chat_retention.py
│   └── directory_sync_task.py
├── Operations
│   ├── cleanup.py                           # Stuck-file recovery, retention, scratch janitor, upload sweeper
│   ├── recovery.py, recovery_tasks.py       # Reclaim lost runs; storage re-ingestion
│   ├── storage_orphan_sweep_task.py, backup_tasks.py, watch_source_tasks.py
│   ├── analytics.py, baseline_export.py, utility.py
```

## Transcription Pipeline (`transcription/`)

### How a recording is processed
`services/pipeline_plan.py` decides, per file, which engines run (local GPU/CPU or cloud for transcription; ASR-provided, local or cloud diarization), where, and on which queue. `transcription/dispatch.py` resolves the plan once, refuses a blocking issue before anything is published, and builds a Celery chain. The default local path is three stages:

```
transcription.preprocess  →  transcription.gpu_transcribe  →  transcription.postprocess
 (download, ffmpeg)            (Whisper + inline speakers)       (voiceprints, index, notify)
        CPU                              GPU                              CPU
```

Task functions: `preprocess_for_transcription` (`preprocess.py`), `transcribe_gpu_task` (`core.py`), `finalize_transcription` (`postprocess.py`).

Other stage tasks, selected by the plan:
- `transcription.cpu_transcribe` (`cpu_task.py`): CPU Whisper, full image only
- `transcription.diarize_gpu` (`diarize_task.py`): diarization only, for the multi-GPU split (`gpu-diarize` queue)
- `cloud_asr.py`: the cloud ASR provider pipeline (`cloud-asr` queue)
- `rediarize_task.py`: the post stage that detects speakers on an already-saved transcript, and the manual "re-detect speakers" action

Enrichment (speaker embeddings, indexing, summarization, topics, facts, redaction) is dispatched asynchronously from postprocess and does not hold the GPU. `transcription.enrich_and_dispatch` (in `postprocess.py`) fans it out.

### Modules in `transcription/`
| Module | Role |
|---|---|
| `preprocess.py` | Stage 1: download from MinIO, extract audio with FFmpeg, probe metadata |
| `core.py` | Stage 2 (GPU): transcription with the engine in `app/transcription/` |
| `postprocess.py` | Stage 3: results, voiceprints, indexing, notifications, downstream dispatch |
| `dispatch.py` | Plan resolution and Celery chain orchestration |
| `finalize.py`, `downstream.py` | Critical-path persistence (segments, speakers, DB) and post-transcription dispatch |
| `cpu_task.py`, `diarize_task.py`, `cloud_asr.py` | Alternative stage 2 implementations |
| `pipelines.py`, `context.py` | Local engine pipelines and configuration; shared task context and failure handlers |
| `audio_processor.py` | `prepare_audio_for_transcription`, `extract_audio_from_video`, `convert_audio_format` |
| `metadata_extractor.py` | `extract_media_metadata` (ExifTool), `get_important_metadata`, `update_media_file_metadata`, ffprobe fallbacks |
| `speaker_processor.py` | Speaker label normalization, speaker row creation, overlap marking, sidecar gender |
| `embeddings.py` | Speaker-embedding extraction/matching in the pipeline |
| `storage.py` | `save_transcript_segments`, `update_media_file_transcription_status` |
| `notifications.py` | WebSocket notifications (`send_progress_notification`, `send_completion_notification`, `send_error_notification`, ...) |
| `requested_options.py`, `user_settings.py` | Per-file request options and per-user transcription preferences |
| `run_ownership.py`, `cancellation.py` | Stand down superseded runs; user-cancel handling |
| `hooks.py`, `waveform_generator.py` | Cloud-edition seam hooks; waveform generation |

### Key behaviours
- **Admin-pinned model**: the local Whisper model comes from the `SystemSettings` key `asr.local_model`, falling back to `WHISPER_MODEL`. GPU workers preload models at startup (`worker_ready`) when `PRELOAD_GPU_MODELS=true`.
- **Progress**: stages report through `progress_tracker.py` (EWMA ETA); every progress frame names its sub-stage (`TranscriptionStage` in `core/enums.py`).
- **One retry policy**: `services/transcription_retry.py` decides retry vs. fail for every way a run can end early. Permanent errors fail fast; transient ones are requeued with backoff.
- **Liveness and recovery**: every stage holds a run lease (`core/task_liveness.py`); lost stages are reclaimed (`core/broker_orphans.py`, `system.reclaim_lost_tasks`) and a stale message stands down through `run_ownership.py`.

## Speaker Tasks

`speaker_tasks.py` is a re-export shim only. The implementations live in dedicated modules:
- `speaker_identification_task.py`: LLM name suggestions from conversation context (`ai.identify_speakers`, nlp queue). Suggestions are never auto-applied; they need manual verification.
- `speaker_update_task.py` / `speaker_merge_task.py`: background speaker record updates and merges.
- `speaker_embedding_task.py`: voiceprint extraction (`extract_speaker_embeddings`) and reassignment updates, on the `cpu` queue.
- `rename_propagation_task.py`: propagate speaker and title renames into the search chunk index.

## Analytics Tasks (`analytics.py`)

`analytics.analyze_transcript` computes per-file analytics (word counts, speaker talk time, turn-taking, interruptions, pace) through `services/analytics_service.py` and stores them in the `analytics` table. `POST /api/files/{uuid}/analytics/refresh` recomputes on demand.

## AI Summarization Tasks (`summarization.py`)

`ai.generate_summary` (nlp queue) generates the BLUF-format summary through `LLMService`: it reads the transcript and speakers, sizes sections against the model's context window, summarizes long transcripts section by section and stitches the results, then stores them in `media_file.summary_data` and indexes a derived summary document for search. Output languages are `LLM_OUTPUT_LANGUAGES` (12). The no-LLM path reports `skipped`, not success. `topic_extraction.py` (`ai.extract_topics`, `ai.extract_topics_batch`) and `auto_labeling.py` produce tag/collection suggestions; the per-user opt-outs for the automatic AI steps are in `services/auto_llm_settings.py`.

## 🧹 Cleanup, Recovery and Maintenance (`cleanup.py`, `recovery.py`)

`cleanup.py` tasks:
- `cleanup.run_periodic_cleanup`: detect and recover stuck files (not on the beat schedule; invoked by operators or other tasks)
- `cleanup.deep_cleanup(dry_run)`: admin-triggered orphan removal
- `cleanup.health_check`, `cleanup.emergency_recovery(file_ids)`: health report and targeted recovery
- `cleanup.orphan_upload_sweeper`, `cleanup.scratch_janitor`: abandoned uploads and scratch volume (scheduled)
- `cleanup_expired_files`: admin-configured file retention (scheduled hourly)

`recovery.py` holds `system.reclaim_lost_tasks` and `system.reclaim_orphaned_deliveries` (scheduled) and the startup and periodic health recovery (`system.health_check`). The stuck-file logic lives in `services/file_cleanup_service.py` and `utils/task_utils.py`.

### Periodic schedule
`celery_app.conf.beat_schedule` in `app/core/celery.py` is the authoritative schedule (about 20 entries: health check every 10 minutes, lost-task reclaim every 2 minutes, search maintenance and sweeps, OpenSearch orphan cleanup, embedding consistency check, GPU stats, retention, scratch janitor, upload sweeper, watch-source scans, backup and mirror checks, directory sync check, chat retention, account-inactivity sweep, session-cap sweep, GDPR erasure reconcile). Read it there rather than from this list. The `celery-beat` service runs it.

## Task Naming & Queue Convention

### Naming Rules

All Celery tasks use an explicit `name=` in the decorator. New tasks use a dotted `<domain>.<action>` name:

| Domain | Purpose | Example |
|--------|---------|---------|
| `transcription.*` | Core transcription pipeline | `transcription.gpu_transcribe` |
| `ai.*` | LLM/AI enrichment | `ai.generate_summary` |
| `media.*` | Media processing | `media.generate_waveform` |
| `download.*` | URL/playlist downloads, prepared exports | `download.media_url` |
| `analytics.*` | Transcript analytics | `analytics.analyze_transcript` |
| `system.*` | Health checks, recovery, GPU stats | `system.health_check` |
| `cleanup.*` | File cleanup | `cleanup.run_periodic_cleanup` |
| `redaction.*`, `artifacts.*`, `asr.*`, `search.*`, `migration.*`, `directory.*`, `account.*`, `gdpr.*`, `backup.*`, `watch_source.*`, `chat.*` | Other subsystems | `redaction.detect`, `artifacts.generate_file_facts` |

Legacy flat names (for example `generate_thumbnail`, `extract_speaker_embeddings`, `reindex_transcripts`) are stable. Do not rename them (routing and in-flight messages depend on the name), and do not create new flat names.

### Queue Assignment Rules

1. **Define queue routing in `task_routes` only** (`backend/app/core/celery.py`), the single source of truth
2. **Do NOT put `queue=` in task decorators.** A decorator queue silently overrides `task_routes`. Enforced by `tests/unit/test_task_routes_complete.py`
3. **Use `CeleryQueues.*` constants** from `app.core.constants`, never raw queue-name strings
4. **Exception**: tasks routed at call time (for example `transcription.gpu_transcribe`, `cpu_transcribe`, the plan's post stage) are listed with a reason in `INTENTIONALLY_UNROUTED_TASKS` in `core/celery.py`
5. **Lite deployments**: work that prefers a GPU but is correct without one is listed in `GPU_PREFERRED_TASKS` and routed through `gpu_preferred_queue()`, which resolves to `cpu` when `DEPLOYMENT_MODE=lite`

### Queue Definitions

All valid queues are declared in `CeleryQueues` (`backend/app/core/constants.py`); `task_create_missing_queues=False` makes a queue-name typo raise at dispatch time. Worker services and concurrency come from `docker-compose.yml` (concurrency values are environment-tunable):

| Queue | Worker service | Concurrency | Purpose |
|-------|----------------|-------------|---------|
| `gpu` | `celery-worker` | 1 | Local GPU transcription and diarization |
| `gpu-transcribe`, `gpu-diarize` | `celery-worker-gpu-transcribe`, `celery-worker-gpu-diarize` | 1 each | Split GPU stages (`gpu-split` profile) |
| `download` | `celery-download-worker` | 5 | yt-dlp downloads, prepared exports, model downloads |
| `cpu` | `celery-cpu-worker` | 8 | Preprocess/postprocess, waveform, thumbnails, speaker embeddings, reindex |
| `utility` | `celery-cpu-worker` | 8 | Lightweight maintenance and scheduled sweeps |
| `cpu-transcribe` | `celery-cpu-worker` | 8 | CPU transcription (full image) |
| `cloud-asr` | `celery-cloud-asr-worker` | 16 | Cloud ASR provider calls |
| `nlp` | `celery-nlp-worker` | 4 | LLM calls (summaries, topics, speaker ID) and ingest artifacts |
| `celery` | `celery-nlp-worker` | 4 | Default fallback queue |
| `embedding` | `celery-embedding-worker` | 1 | Transcript search indexing |
| `redaction` | `celery-redaction` | 2 | Content-redaction detection |

Priorities are per queue (`GPUPriority`, `CPUPriority`, `NLPPriority`, `UtilityPriority`, ... in `core/constants.py`); the scheme is documented above `task_routes` in `core/celery.py`.

### Startup Validation
`tests/unit/test_task_routes_complete.py` fails on any registered task missing from `task_routes` and not listed (with a reason) in `INTENTIONALLY_UNROUTED_TASKS`. On worker startup `_validate_task_routes()` logs the same check as a WARNING, because an unrouted task would otherwise land on the default `celery` queue.

### Task Registration
Tasks are registered through the explicit `include=[...]` list in the `celery_app` initialization (`backend/app/core/celery.py`). When adding a task module, add it to that list.

## 📊 Task Monitoring

### Flower Dashboard
- **URL**: http://localhost:5175/flower (basic auth: `FLOWER_USER` / `FLOWER_PASSWORD`)
- Task monitoring, worker status, performance metrics

### Task Status Tracking
Each tracked task has a row in the `task` table (`models/media.py`): `id` (the Celery task id, a string), `task_type`, `status` (`pending`, `in_progress`, `completed`, `failed`), `progress` (0.0 to 1.0), `error_message`, timestamps, plus the owning `media_file_id` and `user_id`. A file has at most one active (`pending` / `in_progress`) row per `task_type`.

```python
from app.db.session_utils import session_scope
from app.utils.task_utils import update_task_status

with session_scope() as db:
    update_task_status(db, task_id, "in_progress", progress=0.5)
```

Related API: `GET /api/tasks`, `GET /api/tasks/{task_id}`, `GET /api/tasks/progress/active` (live per-stage progress from Redis).

## 🛡️ Error Handling

### Retry
Pipeline tasks declare `autoretry_for` with a narrow exception tuple (for example `ConnectionError`, `TimeoutError`), `acks_late=True`, `reject_on_worker_lost=True` and bounded `max_retries` with backoff; see `transcription/preprocess.py` for the decorator. Whole-run retry after a failure is decided by `services/transcription_retry.py`, not by ad-hoc `self.retry()` calls.

### Error Notification
Failure handlers classify the error once (`ErrorCategorizationService.classify_failure`), store the fixed user-facing sentence on the task and file, log the raw text server-side only, and notify the user through `transcription/notifications.send_error_notification`. Never put raw exception text on the wire.

## 🚀 Performance Notes

- **Never hold a database session across slow work** (MinIO, ffmpeg, model loads, LLM or HTTP calls). Read in a short `session_scope`, return plain data, do the slow work with no session open, then write in a short session. `scripts/audit-session-lifetime.py` enforces this and is part of the commit gate. Details and worked example: `app/tasks/CLAUDE.md`.
- **GPU memory**: models load per worker; do not import a model-loading module into a task that runs on the wrong queue.
- **Temporary files**: use `tempfile.TemporaryDirectory()` or the shared scratch volume helpers (`utils/scratch_volume.py`, `utils/temp_file_utils.py`).

## 🧪 Testing Tasks

Task tests live in `backend/tests/` (mostly `tests/unit/`), run against the live dev stack's PostgreSQL. Call the task function directly (a bound task's `.run(...)`, or `.apply(...)` to go through Celery's eager machinery) and mock only external network services. The stage tasks take a pipeline-plan dict; build one with `services/pipeline_plan.resolve_pipeline_plan(...).to_context()` rather than hand-writing it.

Useful structural tests: `test_task_routes_complete.py` (routing), `test_task_session_lifetime.py` (session scope), `test_pipeline_execution_matrix.py` (pipeline plan cells). Run with `./scripts/run-backend-tests.sh tests/unit`; see `backend/README.md`.

## 🔧 Adding New Tasks

### Checklist
1. **Define the purpose**: one responsibility; pick a `<domain>.<action>` name
2. **Register**: add the module to `include=[...]` and a `task_routes` entry in `app/core/celery.py` (and a `CeleryQueues` constant if you need a new queue)
3. **Sessions**: follow the three-phase session rule above
4. **Progress and errors**: report progress with `update_task_status` / `progress_tracker`; classify failures once
5. **WebSocket events** that name a media file go through `utils/websocket_notify.send_ws_event_for_file`
6. **Tests**: add a routing/behaviour test; run the task-route and session-lifetime tests

### Task Template
```python
from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.utils.task_utils import update_task_status


@celery_app.task(bind=True, name="domain.action", priority=CPUPriority.USER_TRIGGERED)
def domain_action(self, file_uuid: str) -> dict:
    """One-line description of what the task does.

    Args:
        file_uuid: UUID of the media file to process.

    Returns:
        A dict with a ``status`` key (``success``, ``skipped``, or ``error``).
    """
    task_id = self.request.id

    # 1. Short read session returning plain data (no ORM instances escape)
    with session_scope() as db:
        update_task_status(db, task_id, "in_progress", progress=0.1)
        inputs = load_inputs(db, file_uuid)

    # 2. Slow work with no session open
    result = do_slow_work(inputs)

    # 3. Short write session
    with session_scope() as db:
        save_result(db, file_uuid, result)
        update_task_status(db, task_id, "completed", progress=1.0, completed=True)

    return {"status": "success"}
```

`load_inputs`, `do_slow_work` and `save_result` stand for your own functions. Routing is in `task_routes`, so the decorator carries no `queue=`.
