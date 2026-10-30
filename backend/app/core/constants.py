"""
Application-wide constants and configuration values.

This module holds the coded defaults of DB-backed settings and the other
magic numbers shared across the application. Domain groups live beside it:
``queues``, ``scratch_paths``, ``languages``, ``embedding_models``,
``chat_defaults``, ``llm_defaults`` and ``redaction_defaults``.
"""

# File upload constants
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks for file uploads
# Speaker-profile avatar ceiling. Enforced while the body streams by
# app/middleware/upload_limit.py and again on the parsed file by the endpoint.
MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2MB
MAX_FILENAME_LENGTH = 255
DEFAULT_FILE_NAME = "unnamed_file"

# Thumbnail settings (WebP optimized, preserves aspect ratio)
THUMBNAIL_MAX_DIMENSION = 1280  # Longest edge - Full HD for crisp display on any screen
THUMBNAIL_QUALITY_WEBP = 75  # WebP quality (replaces JPEG 85)
THUMBNAIL_QUALITY_JPEG = 70  # JPEG fallback quality
THUMBNAIL_FORMAT = "webp"  # Primary format

# Speaker matching confidence thresholds
SPEAKER_CONFIDENCE_HIGH = 0.75  # Auto-accept (green)
SPEAKER_CONFIDENCE_MEDIUM = 0.50  # Requires validation (yellow)
SPEAKER_CONFIDENCE_LOW = 0.0  # Requires user input (red)

# Per-dependency bound for the /health/ready checks (issue #1000). Library defaults
# (no Redis socket timeout, minio-py's 300 s + 5 retries) let one slow dependency
# outlast the prober's own timeout and pull a healthy instance from service.
DEPENDENCY_PROBE_TIMEOUT_SECONDS = 2.0

# Cache control settings
CACHE_CONTROL_MEDIA_MAX_AGE = 86400  # 1 day for media files
CACHE_CONTROL_THUMBNAILS_MAX_AGE = 86400  # 1 day for thumbnails
CACHE_CONTROL_GENERIC_MAX_AGE = 3600  # 1 hour for other files

# Recording settings defaults
DEFAULT_RECORDING_MAX_DURATION = 120  # minutes (2 hours)
DEFAULT_RECORDING_QUALITY = "high"
DEFAULT_RECORDING_AUTO_STOP = True

# Valid recording durations (in minutes)
VALID_RECORDING_DURATIONS = [15, 30, 60, 120, 240, 480]

# Valid recording quality options
VALID_RECORDING_QUALITIES = ["standard", "high", "maximum"]

# OpenSearch settings
OPENSEARCH_DEFAULT_SIZE = 20
OPENSEARCH_MAX_RESULT_WINDOW = 50000

# Neural search bootstrap self-heal (issue #625). See
# app/services/search/neural_bootstrap.py and its CLAUDE.md section for why this is a
# beat task rather than only a startup task.
NEURAL_BOOTSTRAP_STARTUP_DELAY_SECONDS = 15
NEURAL_BOOTSTRAP_BASE_BACKOFF_SECONDS = 600
NEURAL_BOOTSTRAP_MAX_BACKOFF_SECONDS = 21600
NEURAL_BOOTSTRAP_ALERT_AFTER_ATTEMPTS = 3
NEURAL_BOOTSTRAP_LOCK_TIMEOUT_SECONDS = 900


# Search & RAG constants
SEARCH_DEFAULT_PAGE_SIZE = 20
SEARCH_MAX_PAGE_SIZE = 100
SEARCH_MAX_SNIPPETS_PER_FILE = 10  # Top occurrences per file (reduces memory/latency)
SEARCH_MAX_SEMANTIC_SNIPPETS_PER_FILE = 2  # Display limit for card view (deprecated)
# SEARCH_HYBRID_MIN_SCORE lives in config.Settings (env-tunable, default 0.005) —
# services/search/result_processing.py reads settings.SEARCH_HYBRID_MIN_SCORE. A same-named
# constant here was dead and shadowed the real value when tuning.
SEARCH_CACHE_TTL_SECONDS = 300
SEARCH_CACHE_MAX_SIZE = 256

# Org-collection read route for search (#1275, governance plan D-3): a chunk is readable by a
# caller when its ``collection_ids`` names an organization collection they may see. The ids
# are sent in a ``terms`` clause, so above this many the arm is dropped (falling back to the
# per-file ``accessible_user_ids`` term alone) and a WARNING is logged.
MAX_VISIBLE_COLLECTION_TERMS = 10_000

# Content-access audit (#1277, governance plan D-10): a view-class event for the same
# (event, actor, resource, surface) is written at most once per this window.
CONTENT_AUDIT_DEDUP_SECONDS = 900
#: ``SystemSettings`` key (super_admin toggle, default OFF) that, together with the
#: ``audit.content_access`` capability, switches on the view / media-access / export events.
AUDIT_CONTENT_ACCESS_SETTING_KEY = "audit.content_access.enabled"
AUDIT_CONTENT_ACCESS_DEFAULT = False
#: Rough cost of content auditing, shown in the admin help text (governance plan 3.4): an
#: event is ~0.8 KB; with de-dup an active user writes ~34 KB a day.
AUDIT_EVENT_BYTES_ESTIMATE = 800
AUDIT_ACTIVE_USER_BYTES_PER_DAY_ESTIMATE = 34_000

# How long a mutation (segment edit, speaker reassignment/merge, rediarize) waits
# before a real re-index actually runs (issue #666). A Redis SETNX debounce keyed
# per file coalesces bursts of edits — a user correcting twenty segments in a row
# queues one re-index, not twenty — at the cost of the edit being searchable this
# many seconds later rather than immediately.
TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS = 15


# WebSocket notification types for search
NOTIFICATION_TYPE_REINDEX_PROGRESS = "reindex_progress"
NOTIFICATION_TYPE_REINDEX_COMPLETE = "reindex_complete"
NOTIFICATION_TYPE_REINDEX_STOPPED = "reindex_stopped"

# WebSocket notification type for the local Whisper model download (#1246)
NOTIFICATION_TYPE_LOCAL_MODEL_DOWNLOAD = "local_model_download"

# Celery node-name prefixes (``--hostname <prefix>%h`` in the compose files) of the workers that
# load the pinned Whisper model, so the only ones a model switch restarts (#1246). ``gpu-diarize@``
# is deliberately absent: it serves the diarization queue. If a topology ever puts Whisper there,
# add its prefix here.
WHISPER_WORKER_PREFIXES: tuple[str, ...] = (
    "gpu-transcription@",
    "gpu-transcribe@",
    "gpu-scaled@",
)

# WebSocket notification types for embedding migration
NOTIFICATION_TYPE_MIGRATION_PROGRESS = "migration_progress"
NOTIFICATION_TYPE_MIGRATION_COMPLETE = "migration_complete"
NOTIFICATION_TYPE_MIGRATION_FINALIZED = "migration_finalized"

# Speaker clustering notification types
NOTIFICATION_TYPE_CLUSTERING_PROGRESS = "clustering_progress"
NOTIFICATION_TYPE_CLUSTERING_COMPLETE = "clustering_complete"
NOTIFICATION_TYPE_CLUSTERING_FILE_COMPLETE = "clustering_file_complete"

# Speaker attribute migration notification types
NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_PROGRESS = "attribute_migration_progress"
NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_COMPLETE = "attribute_migration_complete"

# Combined speaker migration notification types
NOTIFICATION_TYPE_COMBINED_MIGRATION_PROGRESS = "combined_speaker_migration_progress"
NOTIFICATION_TYPE_COMBINED_MIGRATION_COMPLETE = "combined_speaker_migration_complete"

# Data integrity / orphan cleanup notification types
NOTIFICATION_TYPE_DATA_INTEGRITY_PROGRESS = "data_integrity_progress"
NOTIFICATION_TYPE_DATA_INTEGRITY_COMPLETE = "data_integrity_complete"

# Embedding consistency / self-healing notification types
NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_PROGRESS = "embedding_consistency_progress"
NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_COMPLETE = "embedding_consistency_complete"

# Progress tracking intervals
PROGRESS_UPDATE_INTERVAL = 1000  # milliseconds
DOWNLOAD_CHECK_INTERVAL = 1000  # milliseconds
MAX_DOWNLOAD_CHECK_COUNT = 60  # seconds

# HTTP status codes (for readability)
HTTP_STATUS_PARTIAL_CONTENT = 206
HTTP_STATUS_RANGE_NOT_SATISFIABLE = 416

# File type patterns
AUDIO_CONTENT_TYPE_PREFIX = "audio/"
VIDEO_CONTENT_TYPE_PREFIX = "video/"
IMAGE_CONTENT_TYPE_PREFIX = "image/"

# WebSocket notification types
NOTIFICATION_TYPE_FILE_CREATED = "file_created"
NOTIFICATION_TYPE_TRANSCRIPTION_STATUS = "transcription_status"
NOTIFICATION_TYPE_SUMMARIZATION_STATUS = "summarization_status"
NOTIFICATION_TYPE_SPEAKER_MATCH = "speaker_match"
# Per-file post-processing task states (app/services/post_processing_status.py).
NOTIFICATION_TYPE_POST_PROCESSING_STATUS = "post_processing_status"
# How long a file's post-processing states stay readable after the last report (seconds).
POST_PROCESSING_STATUS_TTL_S = 2 * 24 * 3600

# Sharing notifications
NOTIFICATION_TYPE_COLLECTION_SHARED = "collection_shared"
NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED = "collection_share_revoked"
NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED = "collection_share_updated"
# Sent to the members who lose sight of a collection when it is restricted (#1275). The
# payload is the collection uuid only: they already knew it, and a name would disclose a
# restricted collection's existence to a person who no longer may know it.
NOTIFICATION_TYPE_COLLECTION_ACCESS_CHANGED = "collection_access_changed"
NOTIFICATION_TYPE_GROUP_MEMBER_ADDED = "group_member_added"
NOTIFICATION_TYPE_GROUP_MEMBER_REMOVED = "group_member_removed"

# Token estimation constants for LLM services
CHARS_PER_TOKEN_ESTIMATE = 4.0  # Average characters per token
SUBWORD_TOKENIZATION_FACTOR = 1.3  # Factor for subword tokenization
TOKEN_ESTIMATION_BUFFER = 1.1  # 10% buffer for safety

# Stream processing constants
DEFAULT_CHUNK_SIZE = 16384  # 16KB default chunk size
VIDEO_CHUNK_SIZE = 65536  # 64KB for video streaming
AUDIO_CHUNK_SIZE = 8192  # 8KB for audio streaming

# Speaker analysis pipeline segment duration thresholds
# After merging adjacent segments, only sections above these thresholds are sent to the model.
SPEAKER_SEGMENT_MIN_DURATION = 1.0  # Standard minimum — skip segments shorter than this
SPEAKER_SHORT_SEGMENT_MIN_DURATION = (
    0.5  # Fallback for speakers whose total merged time never reaches 1s
)

# Transcription settings defaults
DEFAULT_TRANSCRIPTION_MIN_SPEAKERS = 1
DEFAULT_TRANSCRIPTION_MAX_SPEAKERS = 20
DEFAULT_SPEAKER_PROMPT_BEHAVIOR = "always_prompt"
DEFAULT_GARBAGE_CLEANUP_ENABLED = True
DEFAULT_GARBAGE_CLEANUP_THRESHOLD = 50

# Field groups for the scoped reset (DELETE /user-settings/transcription?group=...).
# The union must equal the fields the PUT handler persists; a unit test enforces it.
TRANSCRIPTION_SETTING_GROUPS: dict[str, tuple[str, ...]] = {
    "language": ("source_language", "translate_to_english", "llm_output_language"),
    "accuracy": (
        "garbage_cleanup_enabled",
        "garbage_cleanup_threshold",
        "vad_threshold",
        "vad_min_silence_ms",
        "vad_min_speech_ms",
        "vad_speech_pad_ms",
        "hallucination_silence_threshold",
        "repetition_penalty",
    ),
    "speakers": (
        "diarization_source",
        "diarization_asr_config_uuid",
        "speaker_prompt_behavior",
        "min_speakers",
        "max_speakers",
    ),
}

# Watch Sources global tuning (DB-backed via SystemSettings, admin-UI managed,
# no restart). Coded defaults here are the single source of truth — there are
# NO watch tuning .env vars (only the physical WATCH_FOLDER_PATH mount).
# SystemSettings keys: watch.enabled / watch.file_stability_seconds /
# watch.max_imports_per_scan / watch.fs_events_enabled / watch.fs_events_mode /
# watch.fs_events_poll_seconds.
DEFAULT_WATCH_ENABLED = True
DEFAULT_WATCH_FILE_STABILITY_SECONDS = 30  # skip files modified within N s (still writing)
# Per-scan cap on how many standalone files one scan imports, NOT a concurrency
# limit — imports run serially inline inside scan_single. Raising this lengthens a
# single scan task rather than parallelizing it (issue #295).
DEFAULT_WATCH_MAX_IMPORTS_PER_SCAN = 5
DEFAULT_WATCH_FS_EVENTS_ENABLED = False  # optional watchdog layer (polling is the baseline)
# How the FS-event layer picks an observer per local source (issue #294):
#   auto    — filesystem heuristic + live delivery probe, falling back to the
#             cross-platform PollingObserver when native events don't arrive
#             (macOS/Windows Docker bind mounts, NFS/SMB/NAS mounts).
#   native  — force the platform observer (inotify in our Linux containers).
#   polling — force watchdog's PollingObserver (works everywhere, costs a stat sweep).
#   off     — run no observer at all; Celery polling remains the only mechanism.
DEFAULT_WATCH_FS_EVENTS_MODE = "auto"
WATCH_FS_EVENTS_MODES = ("auto", "native", "polling", "off")
# Stat-sweep interval for the PollingObserver fallback. Lower = lower latency,
# higher = cheaper on a large or network-mounted tree.
DEFAULT_WATCH_FS_EVENTS_POLL_SECONDS = 15

# Transactional auth email (password reset / invitation / verification / security
# notice). The transport is DB-backed: a super_admin designates ONE of the shared
# EmailNotificationConfig rows — the same admin-UI-managed, AES-256-GCM-encrypted
# smtp/m365/exchange stack watch-source notifications use — and auth mail goes out
# through it. The designation is a SystemSettings key rather than a column so it
# needs no migration and no restart.
#
# Empty default = nothing is designated, and app/services/email_service.py falls
# back to the SMTP_* env vars. It deliberately does NOT auto-pick a config: those
# rows are created for specific notification purposes, and mailing password resets
# out of an unrelated mailbox leaks the deployment's auth mail through it.
AUTH_EMAIL_CONFIG_SETTING_KEY = "email.auth_config_uuid"
DEFAULT_AUTH_EMAIL_CONFIG_UUID = ""

# The coded default of settings.FRONTEND_URL, restated here because it is a value
# to REJECT rather than to use: it is set in none of the 23 compose files, so a
# deployment that configures mail but not FRONTEND_URL would mail every user a
# credential link pointing at their own machine. email_service refuses to send
# such a link once a real transport exists.
DEFAULT_FRONTEND_URL = "http://localhost:5173"

# Scheduled database backups (Feature C, issue: data-loss incident).
# DB-backed via SystemSettings (admin-UI managed, no beat restart). Coded defaults
# here are the single source of truth — there are NO backup .env vars. The ONLY
# physical env is BACKUP_HOST_PATH (docker-compose.backup.yml mount) which lands at
# the container path DEFAULT_BACKUP_DESTINATION. SystemSettings keys: backup.enabled /
# backup.schedule / backup.destination / backup.retention_daily|weekly|monthly /
# backup.encrypt / backup.passphrase_file / backup.include_opensearch /
# backup.last_run_at / backup.last_result.
DEFAULT_BACKUP_ENABLED = False  # opt-in: writes data off the live DB volume
DEFAULT_BACKUP_SCHEDULE = "0 3 * * *"  # cron: daily at 03:00 (worker timezone = UTC)
DEFAULT_BACKUP_DESTINATION = "/backups"  # container path; mount a host dir here
# GFS retention: keep N most-recent daily, N weekly (Mondays), N monthly (1st-of-month).
DEFAULT_BACKUP_RETENTION_DAILY = 7
DEFAULT_BACKUP_RETENTION_WEEKLY = 4
DEFAULT_BACKUP_RETENTION_MONTHLY = 12
DEFAULT_BACKUP_ENCRYPT = False  # gpg AES-256 symmetric; needs a passphrase file
# Path (in-container) to a file whose contents are the gpg symmetric passphrase.
# Empty = no passphrase configured (encryption requested but unconfigured → task errors).
DEFAULT_BACKUP_PASSPHRASE_FILE = ""  # noqa: S105  # nosec B105 - a file PATH default (empty = unset), not a password
# ⚠️ This used to default to False, justified by a comment calling OpenSearch derived data
# that Postgres could rebuild. That justification was WRONG and the default was dangerous
# (issue #658). The transcript/chunk indices are rebuildable; the SPEAKER indices are not.
# Postgres stores no embedding vectors at all — SpeakerProfile carries only
# embedding_count + last_embedding_update (models/media.py) — so `speakers_v3`/`speakers_v4`
# are the sole copy of a deployment's biometric data, and re-deriving one needs the source
# media (which may be gone) plus a GPU re-embed run. Defaulting this off shipped a
# scheduled backup that silently excluded the only unrecoverable data in the system.
#
# On by default is also the non-noisy choice: the scheduled backup only runs at all when
# `backup.destination` is a writable mount, which in practice means the --with-backup
# overlay — and that same overlay is what allow-lists `path.repo` on the OpenSearch
# container, so the snapshot repository the leg needs is present exactly when the leg can
# run. Where it is not, opensearch_snapshot.perform_snapshot records an "unsupported"
# status naming the voiceprints at risk; it never fails the pg dump.
DEFAULT_BACKUP_INCLUDE_OPENSEARCH = True

# Backup destination: "local" (mounted dir, default) or "s3" (S3-compatible bucket).
# The s3 path lets homelab/cloud users push dumps off-host to AWS S3 / MinIO / Backblaze /
# Wasabi / etc. All s3.* settings are DB-backed SystemSettings (NO .env); the secret key is
# AES-256-GCM encrypted at rest (app.utils.encryption.encrypt_api_key) and never returned by
# the API (write-only — only a backup.s3_secret_key_set bool is exposed). SystemSettings keys:
# backup.destination_type / backup.s3_endpoint_url / backup.s3_region / backup.s3_bucket /
# backup.s3_prefix / backup.s3_access_key_id / backup.s3_secret_key (encrypted).
DEFAULT_BACKUP_DESTINATION_TYPE = "local"  # "local" | "s3"
DEFAULT_BACKUP_S3_ENDPOINT_URL = ""  # empty = real AWS S3; set for MinIO/B2/Wasabi/etc.
DEFAULT_BACKUP_S3_REGION = ""  # e.g. us-east-1; empty when the endpoint doesn't need one
DEFAULT_BACKUP_S3_BUCKET = ""  # target bucket (must already exist)
DEFAULT_BACKUP_S3_PREFIX = "opentranscribe/"  # key prefix within the bucket
DEFAULT_BACKUP_S3_ACCESS_KEY_ID = ""

# Incremental MinIO media mirror (issue #242) — copies the irreplaceable originals
# (media bucket, minus regenerable prefixes) to a second location on a schedule.
# Same conventions as the DB backup above: DB-backed SystemSettings (backup.mirror_*),
# coded defaults here, NO .env vars beyond the physical mount BACKUP_MIRROR_HOST_PATH
# (docker-compose.backup.yml → container path DEFAULT_BACKUP_MIRROR_DESTINATION).
# The mirror NEVER deletes destination objects (fat-finger/ransomware protection).
DEFAULT_BACKUP_MIRROR_ENABLED = False  # opt-in: reads the whole media bucket
DEFAULT_BACKUP_MIRROR_SCHEDULE = "30 3 * * *"  # nightly, offset from the 03:00 DB dump
DEFAULT_BACKUP_MIRROR_DESTINATION_TYPE = "local"  # "local" | "s3"
DEFAULT_BACKUP_MIRROR_DESTINATION = "/media-mirror"  # container path; mount a host dir here
DEFAULT_BACKUP_MIRROR_THROTTLE_MS = 0  # inter-object sleep (ms) to cap I/O pressure
DEFAULT_BACKUP_MIRROR_S3_ENDPOINT_URL = ""  # empty = real AWS S3; set for MinIO/B2/etc.
DEFAULT_BACKUP_MIRROR_S3_REGION = ""
DEFAULT_BACKUP_MIRROR_S3_BUCKET = ""  # target bucket (must already exist)
DEFAULT_BACKUP_MIRROR_S3_PREFIX = "opentranscribe-media/"  # key prefix within the bucket
DEFAULT_BACKUP_MIRROR_S3_ACCESS_KEY_ID = ""

# Directory reconciliation / deprovisioning (LDAP).
# DB-backed via SystemSettings (admin-UI managed, no beat restart). Coded defaults
# here are the single source of truth — there are NO directory-sync .env vars; the
# directory connection itself reuses the existing LDAP auth config.
# SystemSettings keys: directory_sync.enabled / directory_sync.schedule /
# directory_sync.dry_run / directory_sync.max_disables_per_run /
# directory_sync.last_run_at / directory_sync.last_result.
#
# Defaults are deliberately the timid ones. The sweep DISABLES accounts, so
# shipping it on-by-default would let a first-boot LDAP misconfiguration lock a
# whole deployment out before anyone saw a log line. Enabled=False + dry_run=True
# means an operator must opt in twice — once to run it, once to let it act.
DEFAULT_DIRECTORY_SYNC_ENABLED = False  # opt-in: disables accounts
DEFAULT_DIRECTORY_SYNC_SCHEDULE = "0 4 * * *"  # cron: daily 04:00 UTC, after the backups
DEFAULT_DIRECTORY_SYNC_DRY_RUN = True  # report what WOULD be disabled, change nothing
# Blast radius per pass. A directory that answers "gone" for everyone (wrong
# search_base, wrong group DN) is indistinguishable from mass offboarding, so the
# cap is what stops one bad config from disabling the deployment in a single run.
DEFAULT_DIRECTORY_SYNC_MAX_DISABLES_PER_RUN = 10

# Same reasoning as DEFAULT_DIRECTORY_SYNC_MAX_DISABLES_PER_RUN, for the AC-2
# inactivity sweep (account_lifecycle_service.py): ACCOUNT_INACTIVE_DAYS has no
# range validation at the env layer, so a misconfigured 0/negative value would
# otherwise make every account with a recorded login a candidate on the first
# tick. Higher than directory sync's 10 because this is a deployment-wide DAILY
# pass, not a 15-minute LDAP reconciliation tick -- a legitimate first enable on
# an established deployment can easily have more than 10 genuinely stale accounts.
ACCOUNT_INACTIVITY_MAX_DISABLES_PER_RUN = 500

# FedRAMP AC-10 concurrent-session ceiling (issue #632). `token_service.
# enforce_session_ceiling` revokes every active session beyond the newest N in one
# UPDATE ... OFFSET :limit statement; this bounds the per-call blast radius against a
# large pre-existing backlog (a user who accumulated hundreds of sessions before the
# ceiling became a real invariant). Applied both on the per-login path (login.py) and
# the periodic sweep (session_cap_service.py), the latter with its own, larger cap
# below — a login only ever has to clear its own small overshoot, while the sweep may
# be catching up an entire deployment's backlog in one pass.
SESSION_CAP_EVICTION_BATCH_LIMIT = 50
SESSION_CAP_SWEEP_MAX_PER_USER = 500

# Silero VAD defaults — used by faster-whisper BatchedInferencePipeline
DEFAULT_VAD_THRESHOLD = 0.5  # Speech detection sensitivity (0.1-0.95)
DEFAULT_VAD_MIN_SILENCE_MS = 2000  # Min silence to split segments (ms)
DEFAULT_VAD_MIN_SPEECH_MS = 250  # Min speech duration to keep (ms)
DEFAULT_VAD_SPEECH_PAD_MS = 400  # Padding around detected speech (ms)

# Accuracy tuning defaults
DEFAULT_HALLUCINATION_SILENCE_THRESHOLD: float | None = None  # None = disabled
DEFAULT_REPETITION_PENALTY = 1.0  # 1.0 = no penalty

# Valid speaker prompt behaviors
VALID_SPEAKER_PROMPT_BEHAVIORS = ["always_prompt", "use_defaults", "use_custom"]

# Diarization source options. What each one resolves to, per transcription engine and
# deployment, is decided in one place: services/pipeline_plan.resolve_pipeline_plan.
# ``asr_config`` = another saved ASR configuration's speaker detection, named by the
# ``transcription_diarization_asr_config_uuid`` user setting.
VALID_DIARIZATION_SOURCES = ("provider", "local", "pyannote", "asr_config", "off")
DEFAULT_DIARIZATION_SOURCE = "provider"

# Native diarizer (diar-native sidecar) model provisioning.
#
# The ONNX/PLDA set is exported locally from the gated pyannote weights — see
# app/transcription/native_provision.py for why it cannot be shipped. These are coded
# defaults so a deployment needs no .env entry to work; the matching DIAR_NATIVE_*
# variables in .env.example only exist to override them.
#
# `fast` carries the batch-64 graphs; `small` is the laptop tier (issue #511).
VALID_DIAR_NATIVE_MODEL_SETS = ("fast", "small")
DEFAULT_DIAR_NATIVE_MODEL_SET = "fast"
# A cold export writes ~484 MB and measured 137 s in this image. The budget is wide
# because most of the wall clock is a HuggingFace download that a slow link can stretch;
# it exists to bound a hang, not to pace a healthy run.
DEFAULT_DIAR_NATIVE_PROVISION_TIMEOUT_S = 1800

# Per-request /diarize timeout, DURATION-SCALED (issue #656 Step 4). A flat bound is wrong in
# both directions: DIAR_NATIVE_TIMEOUT_S (env, ceiling only) is meaningless for a 30-second
# memo and would fail a genuine multi-hour recording if raised naively. The real per-request
# budget is derived from audio already in hand (`len(audio)/16000`, no new plumbing needed):
#
#   budget(d) = min(DIAR_NATIVE_TIMEOUT_S, DIAR_SIDECAR_TIMEOUT_FLOOR_S + d / DIAR_SIDECAR_MIN_RTF)
#
# Derivation (docs/BENCHMARK_RESULTS.md): PyAnnote diarization measured 80.2x/79.1x realtime
# solo, 26x at concurrency=3 (the slowest figure in the tree, "queue-load est."); largest file
# ever benchmarked is 4.7h = 17,059s. DIAR_SIDECAR_MIN_RTF=3.0 leaves 8.7x margin on the worst
# figure and 26x on the only directly-measured one. DIAR_SIDECAR_TIMEOUT_FLOOR_S=300 covers
# connect + first-request ORT session load (SPEAKRS_LAZY_SESSIONS=1) + a short admission queue.
# Above ~1.25h audio the ceiling binds at 1800s, still 2.7x the slowest number ever recorded for
# the largest supported file. CPU-mode sidecar throughput is UNMEASURED here — that is why the
# ceiling stays the env var DIAR_NATIVE_TIMEOUT_S rather than being folded into this constant.
DIAR_SIDECAR_TIMEOUT_FLOOR_S = 300
DIAR_SIDECAR_MIN_RTF = 3.0

# =============================================================================
# diar-native sidecar-unavailable retry policy (issue #656 Step 5)
# =============================================================================
# Mirrors CLOUD_ASR_* in core/queues.py: a retry ladder is policy, not an operator knob, so there are
# deliberately no matching .env entries. 30+60+120+240 ~= 7.5 min total, sized to span a
# container recreate plus a lazy model (re)load — the sidecar healthcheck itself needs
# 15s x 3 failures to mark the container unhealthy, so the first retry must clear that.
DIAR_SIDECAR_RETRY_BASE = 30  # seconds — first retry delay when the sidecar gave no Retry-After
DIAR_SIDECAR_RETRY_MAX = 600  # seconds — backoff ceiling (10 min)
DIAR_SIDECAR_MAX_RETRIES = 4  # attempts

# =============================================================================
# Organization Context Settings
# =============================================================================

# Organization context maximum character length
ORG_CONTEXT_MAX_LENGTH = 10000

# Default organization context settings
DEFAULT_ORG_CONTEXT_TEXT = ""
DEFAULT_ORG_CONTEXT_INCLUDE_DEFAULT_PROMPTS = True
DEFAULT_ORG_CONTEXT_INCLUDE_CUSTOM_PROMPTS = False

# =============================================================================
# Download Quality Settings (URL/YouTube Downloads)
# =============================================================================

VIDEO_QUALITY_OPTIONS: dict[str, str] = {
    "best": "Best Available",
    "2160p": "4K (2160p)",
    "1440p": "1440p (QHD)",
    "1080p": "1080p (Full HD)",
    "720p": "720p (HD)",
    "480p": "480p (SD)",
    "360p": "360p (Low)",
}

AUDIO_QUALITY_OPTIONS: dict[str, str] = {
    "best": "Best Available",
    "320": "320 kbps",
    "192": "192 kbps",
    "128": "128 kbps",
}

DEFAULT_VIDEO_QUALITY = "best"
DEFAULT_AUDIO_ONLY = False
DEFAULT_AUDIO_QUALITY = "best"

VALID_VIDEO_QUALITIES = list(VIDEO_QUALITY_OPTIONS.keys())
VALID_AUDIO_QUALITIES = list(AUDIO_QUALITY_OPTIONS.keys())

# =============================================================================
# Auto-Label Settings
# =============================================================================

DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD = 0.75
FUZZY_MATCH_THRESHOLD = 0.85

# Tag/collection source identifiers.
#
# ``TAG_SOURCE_BULK_GROUP`` belongs to *Collections* only — it is never a tag
# origin, so tag code must not treat it as one. ``TAG_SOURCE_AI_ACCEPTED`` is
# the human endorsement of an auto-labeled tag: a person merged another tag into
# it (or otherwise vouched for it), so it is no longer merely machine-proposed.
# ``Tag.source``/``FileTag.source`` are nullable and were never backfilled, so
# NULL still means "predates auto-labeling" and orders as manual.
TAG_SOURCE_MANUAL = "manual"
TAG_SOURCE_AUTO_AI = "auto_ai"
TAG_SOURCE_AI_ACCEPTED = "ai_accepted"
TAG_SOURCE_BULK_GROUP = "bulk_group"

# WebSocket notification types for auto-labeling
NOTIFICATION_TYPE_AUTO_LABEL_STATUS = "auto_label_status"

# AI Summary settings defaults
DEFAULT_AI_SUMMARY_ENABLED = True

# WebSocket notification type for redaction completion.
NOTIFICATION_TYPE_REDACTION_STATUS = "redaction_status"

# pyannoteAI's diarization model for its cloud diarization and STT-orchestration APIs.
# precision-2 is deprecated by the vendor on 2026-10-17 (#1153); precision-3 replaces it
# and, like precision-2, supports transcription (community-1 does not).
PYANNOTE_DEFAULT_DIARIZATION_MODEL = "precision-3"

# Coarse buckets the SPA's `file_type` filter sends, mapped to the MIME family prefix that
# actually appears in the indexed/stored `content_type` field (`audio/mpeg`, `video/mp4`, ...).
# The single source of truth for both the search plane (services/search/filters.py's
# _file_type_filter_clause) and the gallery plane (files/filtering.py's
# apply_file_type_filter) — issue #871. They used to keep separate copies, and only the
# search plane's got the #463 fix that makes an unrecognized value narrow to nothing instead
# of being silently dropped.
FILE_TYPE_MIME_PREFIXES: dict[str, str] = {"audio": "audio/", "video": "video/"}

# =============================================================================
# Presigned-URL revocation (issue #907)
# =============================================================================
# The S3/MinIO object tag key the restricted presign identity's policy Denies
# s3:GetObject on (StringEquals, value STORAGE_QUARANTINE_TAG_VALUE — see
# storage_presign_identity.QUARANTINE_TAG_VALUE for the case-sensitivity note).
# Both the policy builder (storage_presign_identity.build_presign_policy) and
# the tagger (minio_service.set_object_quarantine_tag) import this ONE constant
# rather than hardcoding the string twice.
STORAGE_QUARANTINE_TAG_KEY = "ot-quarantine"

# =============================================================================
# Support-access grants (issue #1122)
# =============================================================================
# Code constants, not settings, on purpose: the control constrains platform admins, so an
# admin must not be able to widen it from the UI (separation of duties).
SUPPORT_ACCESS_MIN_TTL_MINUTES = 15
SUPPORT_ACCESS_MAX_TTL_MINUTES = 480
SUPPORT_ACCESS_BREAK_GLASS_MAX_TTL_MINUTES = 240
SUPPORT_ACCESS_DEFAULT_TTL_MINUTES = 60
#: An undecided request lapses after this long and can no longer be approved or denied.
SUPPORT_ACCESS_PENDING_EXPIRY_HOURS = 72
#: Cap on a presigned URL minted under a grant. A SigV4 URL is a bearer token, not bound to
#: a user, so revoking the grant does not invalidate one already minted; this bounds the
#: residual access after revocation.
SUPPORT_ACCESS_PRESIGN_MAX_SECONDS = 300
#: Grant creation is rate limited per user so request spam cannot flood approvers.
SUPPORT_ACCESS_REQUEST_RATE_LIMIT = "10/hour"
#: Organization takedown actions are rate limited per user (issue #1253). Every quarantine or
#: release bumps the deployment-wide retrieval-cache version, so one tenant's churn costs every
#: tenant their cache; a per-tenant version is a separate follow-up.
ORG_TAKEDOWN_RATE_LIMIT = "30/hour"
#: Recording notices and outcomes, notifying owners, and reading the notice list (issue #1254).
#: Shared per user across the uuid-bearing routes: the limiter's default bucket includes the
#: path, so a per-route limit would give every distinct uuid a fresh allowance.
TAKEDOWN_NOTICE_RATE_LIMIT = "120/hour"
#: The compliance export is the heaviest read here and returns other tenants' metadata.
TAKEDOWN_EXPORT_RATE_LIMIT = "10/hour"
#: Rows one export may return. A partial export says so (``X-Export-Truncated``).
TAKEDOWN_EXPORT_MAX_ROWS = 20000
#: Rows one audit-log export may return, for the super-admin and the organization-admin export alike
#: (issue #1277). It is also OpenSearch's default ``max_result_window``, so a larger page would fail.
AUDIT_EXPORT_MAX_ROWS = 10000
#: An organization admin's audit export: it reads the whole tenant's history in one request.
ORG_AUDIT_EXPORT_RATE_LIMIT = "10/hour"
#: Placing and releasing organization holds (issue #1276): one budget per user across both POSTs.
#: A hold is rare and deliberate; the limit is a brake on a runaway script, not a workflow.
ORG_HOLD_RATE_LIMIT = "30/hour"
#: The owner's own takedown history, read by the SPA on the settings page.
TAKEDOWN_OWNER_READ_RATE_LIMIT = "120/hour"
#: Header that carries a grant uuid. Never ambient: a request without it has no grant.
SUPPORT_ACCESS_GRANT_HEADER = "X-Support-Access-Grant"

# --- Cloud ASR provider policy (issue #1246) ---------------------------------------------
#: ``SystemSettings`` key: JSON list of the cloud provider ids a plain user may choose.
#: ABSENT (or the JSON ``null``) means every provider is allowed, which is what a
#: deployment did before the policy existed; an empty list means none.
ASR_CLOUD_PROVIDERS_FOR_USERS_KEY = "asr.cloud_providers_for_users"
#: ``SystemSettings`` key: ``"true"``/``"false"``. Whether a plain user may save a cloud
#: provider configuration (and key) of their own. Absent means true.
ASR_USERS_OWN_KEYS_KEY = "asr.users_may_add_own_keys"

# --- DOCX / PDF transcript export (issue #1278) ------------------------------------------
# Safety bounds, not tuning: a document export that exceeds one of them is refused, so none of
# them is an environment variable or a SystemSettings row.
#: Numbered lines on each PDF page. Fixed so a page:line pair is a stable citation: the page
#: size changes the line height, never the grid.
DOCUMENT_LINES_PER_PAGE = 25
#: Hard cap on laid-out lines (about 2,400 pages, eight times a measured four-hour recording).
DOCUMENT_EXPORT_MAX_LINES = 60_000
#: Celery soft time limit of one document render, in seconds. Never retried.
DOCUMENT_EXPORT_SOFT_TIME_LIMIT_S = 300
#: Where the images carry the pinned Noto fonts the PDF renderer embeds (see the ``fonts``
#: stage of ``Dockerfile.prod``/``Dockerfile.lite``/``Dockerfile.blackwell``).
DOCUMENT_FONT_DIR = "/usr/share/fonts/opentranscribe"
#: Expiry of the Redis guard that collapses a repeated identical export request onto one job.
DOCUMENT_EXPORT_GUARD_TTL_S = 300

# --- Privacy policy layer (issues #1280, #1279) ------------------------------------------
# Instance layer of the restrict-only privacy policy (``app.services.privacy.policy``).
# Platform (extension resolver), instance, organization and user are ANDed; any layer off
# means off. Defaults are coded here, not in ``.env``: an operator edits them in the admin UI.
#: ``SystemSettings`` key: ``"true"``/``"false"``. May voiceprints exist and be matched at all?
PRIVACY_VOICEPRINT_MATCHING_KEY = "privacy.voiceprint_matching_enabled"
#: ``SystemSettings`` key: ``"true"``/``"false"``. May gender be inferred from voice or names?
PRIVACY_GENDER_INFERENCE_KEY = "privacy.gender_inference_enabled"
#: Both default ON: an upgrade deletes and rewrites nothing and gender inference stays on by
#: default (owner decision 2026-10-25), with a user notice that it can be switched off. It is
#: not result-neutral for a person who had already switched gender detection off: that user
#: layer is the pre-existing setting below, so their stored inferred gender is hidden.
DEFAULT_PRIVACY_VOICEPRINT_MATCHING_ENABLED = True
DEFAULT_PRIVACY_GENDER_INFERENCE_ENABLED = True
#: ``UserSetting`` key for the user layer of voiceprint matching. The gender user layer is the
#: existing ``speaker_attribute_gender_detection_enabled``.
PRIVACY_USER_VOICEPRINT_MATCHING_KEY = "privacy_voiceprint_matching_enabled"
PRIVACY_USER_GENDER_DETECTION_KEY = "speaker_attribute_gender_detection_enabled"
#: How long the write backstop (``assert_voiceprint_write_allowed``) trusts a per-scope answer.
#: Entry gates read fresh; only the backstop caches, and the purge task's second pass runs
#: later than this so a write that raced a switch-off is still caught.
PRIVACY_WRITE_BACKSTOP_TTL_SECONDS = 10.0
