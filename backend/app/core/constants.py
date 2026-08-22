"""
Application-wide constants and configuration values.

This module contains commonly used constants across the application
to avoid magic numbers and improve maintainability.

Language Data Sources:
- Whisper language codes: Imported from faster_whisper.tokenizer._LANGUAGE_CODES (with fallback)
- Language names: From OpenAI whisper source (https://github.com/openai/whisper/blob/main/whisper/tokenizer.py)
  Title-cased for display purposes.
"""

import logging
import os as _os

_logger = logging.getLogger(__name__)

# =============================================================================
# Celery Task Queue Priorities
# =============================================================================
# Priorities are PER-QUEUE and independent of each other.
# A GPUPriority.INTERACTIVE=0 has no relation to CPUPriority.PIPELINE_CRITICAL=2.
# Scale: 0 (highest — runs first) to 9 (lowest — runs last).
# Configured via broker_transport_options: priority_steps=list(range(10)).


class CeleryQueues:
    """All valid Celery queue names — single source of truth.

    Use these constants instead of raw strings to prevent typo-based phantom queues.
    Celery is configured with task_create_missing_queues=False, so any queue name
    not declared here will raise an error at dispatch time.
    """

    GPU = "gpu"
    DOWNLOAD = "download"
    CPU = "cpu"
    NLP = "nlp"
    EMBEDDING = "embedding"
    UTILITY = "utility"
    CLOUD_ASR = "cloud-asr"  # Dynamic: cloud ASR providers (CPU worker consumes)
    CPU_TRANSCRIBE = "cpu-transcribe"  # Dynamic: lightweight CPU transcription
    GPU_TRANSCRIBE = "gpu-transcribe"  # Phase 4: transcription-only GPU worker
    GPU_DIARIZE = "gpu-diarize"  # Phase 4: diarization-only GPU worker
    REDACTION = "redaction"  # Content redaction detection (dedicated CPU service)
    DEFAULT = "celery"  # Celery default queue (NLP worker consumes as fallback)

    ALL: list[str] = [
        GPU,
        DOWNLOAD,
        CPU,
        NLP,
        EMBEDDING,
        UTILITY,
        CLOUD_ASR,
        CPU_TRANSCRIBE,
        GPU_TRANSCRIBE,
        GPU_DIARIZE,
        REDACTION,
        DEFAULT,
    ]


class GPUPriority:
    """GPU queue (concurrency=1). Controls what runs next when the worker is free.
    Interactive UI actions must preempt queued long-running jobs.
    """

    INTERACTIVE = 0  # User action awaiting instant feedback (~5s), e.g. speaker drag
    NEAR_REALTIME = 1  # User action with response in <30s, e.g. manual embedding re-extract
    USER_IMPORT = 3  # User-submitted transcription/import (~5-60min)
    USER_REDIARIZ = 4  # User-triggered re-diarization of an existing file (~5-30min)
    USER_RECLUSTER = 5  # User-triggered full speaker re-clustering (~5-15min)
    ADMIN_MIGRATION = 7  # Admin bulk migration batches (~1-5min/batch); yields to user work


class CPUPriority:
    """CPU queue (concurrency=8). Controls ordering when all workers are busy."""

    PIPELINE_CRITICAL = 2  # Completes the import pipeline the user is watching
    #                        e.g. waveform, thumbnail, post-transcription clustering
    USER_TRIGGERED = 4  # Explicit user action outside the import pipeline
    SYSTEM = 5  # System monitoring and stats collection
    ADMIN_BATCH = 6  # Admin bulk operations and data migrations
    MAINTENANCE = 8  # Scheduled background maintenance (search, cleanup)


class NLPPriority:
    """NLP queue (concurrency=4). LLM API calls and AI enrichment tasks."""

    USER_TRIGGERED = 3  # User explicitly requested (summarize, identify speakers)
    AUTO_PIPELINE = 5  # Automatically triggered after transcription completes
    ADMIN_BATCH = 7  # Admin batch operations
    BACKGROUND = 9  # Background retroactive enrichment (no user waiting)


class DownloadPriority:
    """Download queue (concurrency=3). Network I/O for media downloads."""

    SINGLE_URL = 3  # Single URL — user watching progress bar
    PLAYLIST = 6  # Playlist — bulk download, less urgent per individual item


class EmbeddingPriority:
    """Embedding queue (concurrency=1). OpenSearch neural indexing.
    Single-worker queue — priority controls backlog ordering.
    """

    PIPELINE_CRITICAL = 2  # Post-import indexing — makes new content searchable


class UtilityPriority:
    """Utility queue (concurrency=8). Lightweight maintenance and system tasks."""

    EMERGENCY = 1  # System recovery, critical operations
    OPERATIONAL = 3  # Health checks, monitoring
    ROUTINE = 5  # Periodic cleanup, access tracking
    BACKGROUND = 7  # Migration finalization, status checks
    DEV_TOOLS = 9  # Development and testing utilities (baseline export etc.)


class RedactionPriority:
    """Redaction queue (dedicated CPU service). Content-moderation detection.

    Priority controls backlog ordering within the redaction worker pool.
    """

    PIPELINE_AUTO = 4  # Auto-dispatched after a transcript completes
    USER_TRIGGERED = 3  # User re-ran detection (e.g. after editing a segment)
    ADMIN_BACKFILL = 7  # Admin bulk re-index (model upgrade / first rollout)


# =============================================================================
# Dynamic imports for language support
# =============================================================================

# Try to import language codes from faster_whisper for validation
# Skip in test mode to avoid heavy torch import
if _os.environ.get("SKIP_CELERY", "").lower() == "true":
    _logger.debug("Test mode: using None for WHISPER_LANGUAGE_CODES")
    WHISPER_LANGUAGE_CODES: set[str] | None = None
else:
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES

        WHISPER_LANGUAGE_CODES = set(_LANGUAGE_CODES)
    except ImportError:
        _logger.warning("Could not import faster_whisper language codes for validation")
        WHISPER_LANGUAGE_CODES = None

# File upload constants
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks for file uploads
MAX_FILENAME_LENGTH = 255
DEFAULT_FILE_NAME = "unnamed_file"

# Video processing constants (legacy - kept for backward compatibility)
THUMBNAIL_MAX_WIDTH = 320
THUMBNAIL_MAX_HEIGHT = 240
THUMBNAIL_QUALITY = 85

# Thumbnail settings (WebP optimized, preserves aspect ratio)
THUMBNAIL_MAX_DIMENSION = 1280  # Longest edge - Full HD for crisp display on any screen
THUMBNAIL_QUALITY_WEBP = 75  # WebP quality (replaces JPEG 85)
THUMBNAIL_QUALITY_JPEG = 70  # JPEG fallback quality
THUMBNAIL_FORMAT = "webp"  # Primary format

# Speaker matching confidence thresholds
SPEAKER_CONFIDENCE_HIGH = 0.75  # Auto-accept (green)
SPEAKER_CONFIDENCE_MEDIUM = 0.50  # Requires validation (yellow)
SPEAKER_CONFIDENCE_LOW = 0.0  # Requires user input (red)

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

# LLM service settings
LLM_DEFAULT_MAX_TOKENS = 2000
LLM_DEFAULT_TEMPERATURE = 0.3
LLM_DEFAULT_TIMEOUT = 60

# OpenSearch settings
OPENSEARCH_DEFAULT_SIZE = 20
OPENSEARCH_MAX_RESULT_WINDOW = 50000


def get_speaker_index() -> str:
    """Get the speaker index alias name.

    This is an OpenSearch alias that points to whichever versioned index
    is currently active (speakers_v3 or speakers_v4). All reads should
    go through this alias. Writes should target the versioned index directly.
    """
    from app.core.config import settings

    return settings.OPENSEARCH_SPEAKER_INDEX


def get_speaker_index_v3() -> str:
    """Get the v3 speaker embedding index name (512-dim, pyannote/embedding)."""
    from app.core.config import settings

    return f"{settings.OPENSEARCH_SPEAKER_INDEX}_v3"


def get_speaker_index_v4() -> str:
    """Get the v4 speaker embedding index name (256-dim, WeSpeaker)."""
    from app.core.config import settings

    return f"{settings.OPENSEARCH_SPEAKER_INDEX}_v4"


def get_speaker_index_v3_backup() -> str:
    """Get the legacy v3 backup index name (from pre-alias migrations)."""
    from app.core.config import settings

    return f"{settings.OPENSEARCH_SPEAKER_INDEX}_v3_backup"


# Search & RAG constants
SEARCH_DEFAULT_PAGE_SIZE = 20
SEARCH_MAX_PAGE_SIZE = 100
SEARCH_MAX_SNIPPETS_PER_FILE = 10  # Top occurrences per file (reduces memory/latency)
SEARCH_MAX_SEMANTIC_SNIPPETS_PER_FILE = 2  # Display limit for card view (deprecated)
# SEARCH_HYBRID_MIN_SCORE lives in config.Settings (env-tunable, default 0.005) —
# hybrid_search_service reads settings.SEARCH_HYBRID_MIN_SCORE. A same-named
# constant here was dead and shadowed the real value when tuning.
SEARCH_CACHE_TTL_SECONDS = 300
SEARCH_CACHE_MAX_SIZE = 256

# OpenSearch Native Neural Search Model Registry
# These models are registered and deployed directly in OpenSearch via ML Commons plugin
# Organized by quality tier (Fast → Balanced → Best) and language support (English / Multilingual)
# Every model listed here is VERIFIED by scripts/verify-embedding-models.py: it must
# register, deploy, return its declared dimension from a real prediction, and — for the
# multilingual tiers — place a translation nearer than an unrelated sentence.
#
# ⚠️ Candidates MEASURED AND REJECTED (2026-08-18, opensearch 3.4.0). Do not add these
# back without re-measuring; each failed for a specific reason:
#   paraphrase-multilingual-mpnet-base-v2  not an OpenSearch-provided model at all —
#                                          REGISTER FAILED at 1.0.0/1.0.1/1.0.2 (#504)
#   msmarco-distilbert-base-tas-b          dot-product model: two UNRELATED sentences
#                                          score 0.703 under cosine
#   multi-qa-mpnet-base-dot-v1             dot-product model, control 0.385
#   paraphrase-MiniLM-L3-v2                DEPLOY FAILED
# The two dot-product rejections matter because the chunks index maps
# `"space_type": "cosinesimil"` — such a model is ranked by a metric it was never
# trained for, and the scores look perfectly plausible while being wrong.
#
# Verified working but not offered (no distinct use case over what is here):
#   all-MiniLM-L12-v2 (384d), paraphrase-mpnet-base-v2 (768d).
OPENSEARCH_EMBEDDING_MODELS = {
    # === FAST TIER (384 dimensions) ===
    # Low latency, lower memory. Good for keyword-focused searches.
    "huggingface/sentence-transformers/all-MiniLM-L6-v2": {
        "name": "MiniLM - Fast (English Only)",
        "dimension": 384,
        "size_mb": 80,
        "languages": ["en"],
        "model_format": "TORCH_SCRIPT",
        "default": True,
        "requires_prefix": False,
        "tier": "fast",
        "language_type": "english",
        "description": "Fast, lightweight English model. Good baseline for keyword-heavy searches.",
    },
    "huggingface/sentence-transformers/all-MiniLM-L12-v2": {
        "name": "MiniLM L12 - Higher quality (English Only)",
        "dimension": 384,
        "size_mb": 120,
        "languages": ["en"],
        "model_format": "TORCH_SCRIPT",
        "default": False,
        "requires_prefix": False,
        "tier": "fast",
        "language_type": "english",
        "description": (
            "The default MiniLM with all 12 layers instead of 6 — the L6 default is "
            "literally this model with every other layer removed. Published sbert average "
            "59.8 vs 58.8, for roughly half the encode throughput. Same 384 dimensions, so "
            "switching is a re-embed with no index recreation and a trivial rollback."
        ),
    },
    "huggingface/sentence-transformers/multi-qa-MiniLM-L6-cos-v1": {
        "name": "MiniLM - Retrieval-tuned (English Only)",
        "dimension": 384,
        "size_mb": 80,
        "languages": ["en"],
        "model_format": "TORCH_SCRIPT",
        "default": False,
        "requires_prefix": False,
        "tier": "fast",
        "language_type": "english",
        "description": (
            "Trained for semantic SEARCH rather than general sentence similarity. Same "
            "384 dimensions as the default, so switching is a re-embed and not an index "
            "recreation. Retrieval quality on this corpus is NOT yet benchmarked."
        ),
    },
    "huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": {
        "name": "MiniLM - Fast (Multilingual, 50+ Languages)",
        "dimension": 384,
        "size_mb": 420,
        "languages": ["multilingual"],
        "model_format": "TORCH_SCRIPT",
        "default": False,
        "requires_prefix": False,
        "tier": "fast",
        "language_type": "multilingual",
        "description": "Fast multilingual model. 50+ languages with good quality.",
    },
    # === BALANCED TIER (768 dimensions) ===
    # Good balance of speed and semantic quality.
    "huggingface/sentence-transformers/all-mpnet-base-v2": {
        "name": "MPNet - Balanced (English Only)",
        "dimension": 768,
        "size_mb": 420,
        "languages": ["en"],
        "model_format": "TORCH_SCRIPT",
        "default": False,
        "requires_prefix": False,
        "tier": "balanced",
        "language_type": "english",
        "description": "Better semantic understanding. Good balance of speed and quality.",
    },
    # REMOVED: paraphrase-multilingual-mpnet-base-v2 (issue #504).
    #
    # It is NOT an OpenSearch-provided pretrained model and never was, so offering it
    # here gave admins a choice that could only fail. Measured against
    # opensearchproject/opensearch:3.4.0 at versions 1.0.0, 1.0.1 and 1.0.2, all three:
    #     REGISTER -> FAILED: "This model is not in the pre-trained model list,
    #                          please check your parameters."
    # and its artifact config.json 403s. OpenSearch's provided list contains
    # `paraphrase-multilingual-MiniLM-L12-v2` (multilingual) and
    # `paraphrase-mpnet-base-v2` (English-only); this name conflates the two.
    #
    # Do not re-add it without tracing and uploading it as a CUSTOM model — it is a
    # legitimate 768d/50-language model, it simply is not one OpenSearch ships.
    # Every model listed here must pass scripts/verify-embedding-models.py.
    # === BEST QUALITY TIER ===
    # Highest retrieval quality, recommended for semantic-heavy searches.
    "huggingface/sentence-transformers/all-distilroberta-v1": {
        "name": "DistilRoBERTa - Best Quality (English Only)",
        "dimension": 768,
        "size_mb": 290,
        "languages": ["en"],
        "model_format": "TORCH_SCRIPT",
        "default": False,
        "requires_prefix": False,
        "tier": "best",
        "language_type": "english",
        "description": "Best retrieval quality for English. Excellent semantic understanding.",
    },
    "huggingface/sentence-transformers/distiluse-base-multilingual-cased-v1": {
        "name": "DistilUSE - Best Quality (Multilingual, 15 Languages)",
        "dimension": 512,
        "size_mb": 480,
        "languages": ["multilingual"],
        "model_format": "TORCH_SCRIPT",
        "default": False,
        "requires_prefix": False,
        "tier": "best",
        "language_type": "multilingual",
        "description": "Best quality for common languages. 15 languages with excellent accuracy.",
    },
}

# Default OpenSearch neural model for new installations
OPENSEARCH_DEFAULT_MODEL = "huggingface/sentence-transformers/all-MiniLM-L6-v2"

# Neural ingest pipeline name
OPENSEARCH_NEURAL_PIPELINE = "transcript-neural-ingest"

# WebSocket notification types for search
NOTIFICATION_TYPE_REINDEX_PROGRESS = "reindex_progress"
NOTIFICATION_TYPE_REINDEX_COMPLETE = "reindex_complete"
NOTIFICATION_TYPE_REINDEX_STOPPED = "reindex_stopped"

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

# Sharing notifications
NOTIFICATION_TYPE_COLLECTION_SHARED = "collection_shared"
NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED = "collection_share_revoked"
NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED = "collection_share_updated"
NOTIFICATION_TYPE_GROUP_MEMBER_ADDED = "group_member_added"
NOTIFICATION_TYPE_GROUP_MEMBER_REMOVED = "group_member_removed"

# Task statuses
TASK_STATUS_PENDING = "pending"
TASK_STATUS_PROCESSING = "processing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_ERROR = "error"

# Embedding dimensions
SENTENCE_TRANSFORMER_DIMENSION = 384  # sentence-transformers/all-MiniLM-L6-v2
PYANNOTE_EMBEDDING_DIMENSION = 512  # Legacy v3 dimension (pyannote/embedding)

# Speaker embedding mode constants (PyAnnote v3/v4 compatibility)
# Typed as Literal for mypy compatibility with EmbeddingMode type
EMBEDDING_MODE_V3: str = "v3"  # pyannote/embedding, 512-dim
EMBEDDING_MODE_V4: str = "v4"  # WeSpeaker, 256-dim

# PyAnnote embedding dimensions by version
PYANNOTE_EMBEDDING_DIMENSION_V3 = 512  # pyannote/embedding model
PYANNOTE_EMBEDDING_DIMENSION_V4 = 256  # WeSpeaker ResNet34-LM model

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
DEFAULT_BACKUP_INCLUDE_OPENSEARCH = False  # OS is derived/rebuildable; pg-only this round

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

# Diarization source options
VALID_DIARIZATION_SOURCES = ("provider", "local", "pyannote", "off")
DEFAULT_DIARIZATION_SOURCE = "provider"

# =============================================================================
# Language Settings (Multilingual Support)
# =============================================================================

# Default language settings
DEFAULT_SOURCE_LANGUAGE = "auto"
DEFAULT_TRANSLATE_TO_ENGLISH = False
DEFAULT_LLM_OUTPUT_LANGUAGE = "en"

# Human-readable language names from OpenAI whisper source
# Source: https://github.com/openai/whisper/blob/main/whisper/tokenizer.py
# Names are title-cased for display purposes
_WHISPER_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "zh": "Chinese",
    "de": "German",
    "es": "Spanish",
    "ru": "Russian",
    "ko": "Korean",
    "fr": "French",
    "ja": "Japanese",
    "pt": "Portuguese",
    "tr": "Turkish",
    "pl": "Polish",
    "ca": "Catalan",
    "nl": "Dutch",
    "ar": "Arabic",
    "sv": "Swedish",
    "it": "Italian",
    "id": "Indonesian",
    "hi": "Hindi",
    "fi": "Finnish",
    "vi": "Vietnamese",
    "he": "Hebrew",
    "uk": "Ukrainian",
    "el": "Greek",
    "ms": "Malay",
    "cs": "Czech",
    "ro": "Romanian",
    "da": "Danish",
    "hu": "Hungarian",
    "ta": "Tamil",
    "no": "Norwegian",
    "th": "Thai",
    "ur": "Urdu",
    "hr": "Croatian",
    "bg": "Bulgarian",
    "lt": "Lithuanian",
    "la": "Latin",
    "mi": "Maori",
    "ml": "Malayalam",
    "cy": "Welsh",
    "sk": "Slovak",
    "te": "Telugu",
    "fa": "Persian",
    "lv": "Latvian",
    "bn": "Bengali",
    "sr": "Serbian",
    "az": "Azerbaijani",
    "sl": "Slovenian",
    "kn": "Kannada",
    "et": "Estonian",
    "mk": "Macedonian",
    "br": "Breton",
    "eu": "Basque",
    "is": "Icelandic",
    "hy": "Armenian",
    "ne": "Nepali",
    "mn": "Mongolian",
    "bs": "Bosnian",
    "kk": "Kazakh",
    "sq": "Albanian",
    "sw": "Swahili",
    "gl": "Galician",
    "mr": "Marathi",
    "pa": "Punjabi",
    "si": "Sinhala",
    "km": "Khmer",
    "sn": "Shona",
    "yo": "Yoruba",
    "so": "Somali",
    "af": "Afrikaans",
    "oc": "Occitan",
    "ka": "Georgian",
    "be": "Belarusian",
    "tg": "Tajik",
    "sd": "Sindhi",
    "gu": "Gujarati",
    "am": "Amharic",
    "yi": "Yiddish",
    "lo": "Lao",
    "uz": "Uzbek",
    "fo": "Faroese",
    "ht": "Haitian Creole",
    "ps": "Pashto",
    "tk": "Turkmen",
    "nn": "Nynorsk",
    "mt": "Maltese",
    "sa": "Sanskrit",
    "lb": "Luxembourgish",
    "my": "Myanmar",
    "bo": "Tibetan",
    "tl": "Tagalog",
    "mg": "Malagasy",
    "as": "Assamese",
    "tt": "Tatar",
    "haw": "Hawaiian",
    "ln": "Lingala",
    "ha": "Hausa",
    "ba": "Bashkir",
    "jw": "Javanese",
    "su": "Sundanese",
    "yue": "Cantonese",
}

# Build full WHISPER_LANGUAGES mapping with auto-detect option
WHISPER_LANGUAGES: dict[str, str] = {"auto": "Auto-detect"}
WHISPER_LANGUAGES.update(_WHISPER_LANGUAGE_NAMES)

# Validate against imported codes if available
if WHISPER_LANGUAGE_CODES is not None:
    _missing_names = WHISPER_LANGUAGE_CODES - set(_WHISPER_LANGUAGE_NAMES.keys())
    if _missing_names:
        _logger.warning(f"Missing language names for codes: {sorted(_missing_names)}")
    _extra_names = set(_WHISPER_LANGUAGE_NAMES.keys()) - WHISPER_LANGUAGE_CODES
    if _extra_names:
        _logger.warning(f"Extra language names not in faster_whisper: {sorted(_extra_names)}")

# Common languages shown at the top of dropdowns for convenience
COMMON_LANGUAGES = [
    "auto",
    "en",
    "es",
    "fr",
    "de",
    "it",
    "pt",
    "nl",
    "ru",
    "zh",
    "ja",
    "ko",
    "ar",
]

# Languages supported for LLM output (subset of common languages)
LLM_OUTPUT_LANGUAGES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
}

# =============================================================================
# Reasoning ("thinking") as a per-MODEL capability (issue #64)
# =============================================================================
# A provider accepting an "off" parameter is NOT evidence the model honours it.
# Measured against a real vLLM serving gemma-4-e4b, `enable_thinking: false` was
# byte-identical to omitting the key: HTTP 200, and 931 characters of reasoning
# either way. So the off-switch is *probed*, per model, and the UI control only
# exists where the probe proved it works. Everything below is the probe's
# instrument settings; there are deliberately no `.env` vars, and the recorded
# verdict is a measurement rather than an admin-editable setting (see
# `services/llm_reasoning.py`).

#: `SystemSettings` key prefix. The suffix is a fingerprint of
#: (provider, base_url, model) — see `llm_reasoning.capability_key`.
LLM_REASONING_CAPABILITY_KEY_PREFIX = "llm.reasoning.off_switch."

#: What an unprobed model reports. "unknown" must render no control at all:
#: a toggle the user believes turns reasoning off, over a model that reasons
#: anyway, is worse than having no toggle.
DEFAULT_LLM_REASONING_OFF_SWITCH = "unknown"

#: Temperature for every probe arm. Not a "make it deterministic" claim —
#: greedy decoding merely removes sampling as an explanation for a difference
#: between the arms, which is the whole measurement.
LLM_REASONING_PROBE_TEMPERATURE = 0.0

#: Ceiling per arm. Large enough that a truncated thought is not read as a
#: suppressed one (the measured "on" arm spent 1,123 tokens).
LLM_REASONING_PROBE_MAX_TOKENS = 1200

#: Per-arm HTTP timeout. Three arms, so the whole probe is bounded well inside
#: a request's patience; a slow endpoint yields UNKNOWN rather than hanging.
LLM_REASONING_PROBE_TIMEOUT_S = 120

#: The "off" arm must remove at least 90% of the reasoning the control arms
#: produced before the switch is called real. Justification: the control is
#: labelled *off*. A switch that halves the thinking still leaves the user
#: reading a claim that is false, which is the failure this whole capability
#: exists to prevent — so 90% suppression is the weakest threshold under which
#: "off" is an honest word. The measured negative sits at 1.00 of the omitted
#: control and 0.56 of the activated arm, i.e. 5.6x clear of the boundary on
#: the tighter of the two conditions.
LLM_REASONING_PROBE_SUPPRESSION_RATIO = 0.1

#: Below this many characters the "reasoning" is a stray boundary token rather
#: than a chain of thought, and a ratio computed over it is noise. Observed
#: arms were 931-1,656 characters, ~30x above it.
LLM_REASONING_PROBE_MIN_CHARS = 32

#: Elicits multi-step arithmetic in a couple of hundred tokens. Deliberately
#: not a transcript question: the probe must not depend on retrieval, on the
#: user's library, or on anything that could send recorded content to a
#: provider as a side effect of pressing a button.
LLM_REASONING_PROBE_PROMPT = (
    "A meeting had three speakers. Two of them each spoke for 12 minutes and "
    "the third spoke for half as long as one of them. How many minutes were "
    "spoken in total? Show your working."
)

# --- Context-window discovery (issue #533) — the reasoning probe's sibling. ---
# Same design: the RESULT is a measurement in `SystemSettings`, keyed by the
# (provider, base_url, model) fingerprint, written only by the probe. The values
# below are the instrument's settings, not tunables.

#: `SystemSettings` key prefix. Suffix = the same fingerprint scheme as
#: `llm_reasoning.capability_key`, under its own prefix so the two measurements
#: can never shadow each other.
LLM_CONTEXT_WINDOW_KEY_PREFIX = "llm.context_window."

#: One metadata HTTP call (`/v1/models` or `/api/show`) — no generation, so a
#: short timeout: a slow endpoint yields "unreachable", it does not hang the
#: settings page.
LLM_CONTEXT_WINDOW_PROBE_TIMEOUT_S = 10

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

# =============================================================================
# Content Redaction Settings
# =============================================================================
# Detection models (pre-downloaded at build/startup; loaded only by the
# celery-redaction worker when PRELOAD_REDACTION_MODELS=true).
REDACTION_PII_GLINER_MODEL = "knowledgator/gliner-pii-base-v1.0"
# Standard-architecture toxicity classifiers (load natively, NO trust_remote_code —
# avoids executing arbitrary remote code). toxic-bert is multi-label
# (toxic/severe_toxic/obscene/threat/insult/identity_hate).
REDACTION_TOXICITY_MODEL_EN = "unitary/toxic-bert"
REDACTION_TOXICITY_MODEL_MULTI = "unitary/multilingual-toxic-xlm-roberta"

# Current detection "model version" — bump to invalidate cached spans on a model
# upgrade (the admin re-index targets files whose redaction_model_version differs).
REDACTION_MODEL_VERSION = "v1"

# Detector identifiers (which detectors a user/admin can toggle).
REDACTION_DETECTORS = ["profanity", "pii", "toxicity", "llm"]

# Language coverage per detector (ISO codes). A detector is SKIPPED for a transcript
# whose language is not listed, and the skip is reported to the user. The LLM detector
# is provider-dependent (no fixed list). GLiNER itself is multilingual but our Presidio
# NLP engine is English-only, so PII is en-only until a multilingual engine is added.
REDACTION_PROFANITY_LANGUAGES = ["en"]
REDACTION_PII_LANGUAGES = ["en"]
REDACTION_TOXICITY_LANGUAGES = ["en", "es", "fr", "it", "pt", "tr", "ru"]

# PII name/entity detection engine. Default uses spaCy NER (fast: ~10-20 ms/segment,
# bundled in Presidio). GLiNER is a higher-accuracy but much slower enhancement
# (~130 ms/segment on CPU); enable it only when accuracy on diverse/non-Western names
# matters more than throughput. Ops knob (env-overridable), read by the worker at load.
REDACTION_PII_USE_GLINER = _os.environ.get("REDACTION_PII_USE_GLINER", "false").lower() == "true"

# Compute device policy for the redaction ML models (toxicity, and GLiNER if enabled).
#   auto (default) — use GPU automatically WHENEVER it has free VRAM, else CPU. Checked
#                    at runtime per scan (no restart needed); the model moves GPU<->CPU
#                    as availability changes. Requires the worker to see a GPU.
#   cpu            — always CPU.
#   cuda / cuda:N  — prefer that GPU (still falls back to CPU if it's short on VRAM).
REDACTION_DEVICE = _os.environ.get("REDACTION_DEVICE", "auto").lower()

# Minimum free VRAM (GB) required to place the redaction models on GPU. Below this, the
# worker falls back to CPU so it never starves transcription/diarization on a shared GPU.
# GLiNER (~0.5 GB) + toxic-bert (~0.5 GB) need ~1 GB; 1.5 GB leaves headroom.
REDACTION_MIN_FREE_VRAM_GB = float(_os.environ.get("REDACTION_MIN_FREE_VRAM_GB", "1.5"))

# Categories that can be redacted (a span's `category`).
REDACTION_CATEGORIES = ["profanity", "pii", "toxicity", "custom"]

# PII entity types we surface (Presidio entity names + GLiNER labels mapped to these).
REDACTION_PII_ENTITIES = [
    "NAME",
    "EMAIL",
    "PHONE",
    "SSN",
    "CREDIT_CARD",
    "ADDRESS",
    "BANK_ACCOUNT",
    "IP_ADDRESS",
    "IBAN",
    "LOCATION",
    "ORGANIZATION",
]

# Mask styles.
REDACTION_STYLES = ["label", "asterisks", "first_letter", "blur"]

# Per-user redaction defaults (coded — NO env vars). Mirrors DEFAULT_TRANSCRIPTION_*.
# Redaction is OPT-OUT by default (off): it adds processing time and delays transcript
# display until the scan completes, so users enable it explicitly. Admins can force it.
DEFAULT_REDACTION_ENABLED = False
DEFAULT_REDACTION_DETECTORS = ["profanity", "pii", "toxicity"]  # llm opt-in
# PII is deliberately NOT a default masking category: name masking is aggressive on
# conversational transcripts (every "[NAME]" interrupts reading) and the primary ask
# is profanity/toxicity masking. PII spans are still DETECTED and cached (detectors
# above), so enabling the category later applies instantly at read time.
DEFAULT_REDACTION_CATEGORIES = ["profanity", "toxicity", "custom"]
# ORGANIZATION is INCLUDED, and the reason is measured (issue #499).
#
# It used to be excluded, on the grounds that spaCy over-tags acronyms as ORG and that
# "org names are rarely sensitive PII". The first half is true; the second missed that a
# PERSON'S NAME is sometimes what gets tagged ORG, and excluding the entity meant that
# name shipped in clear to a user who had explicitly asked for PII masking.
#
# MEASURED against the shipped detector (`en_core_web_sm`, the model this app configures
# — NOT the `en_core_web_lg` a bare AnalyzerEngine downloads):
#
#     "Blackwell will follow up"      -> ORGANIZATION @ 0.85   <- a surname. The leak.
#     "Acme Corporation", "Microsoft" -> ORGANIZATION @ 0.85   <- correct, now masked too
#     "SSN", "API", "CPU"             -> ORGANIZATION @ 0.85   <- noise, now masked too
#
# ⚠️ **A confidence threshold cannot separate those.** Presidio's own FAQ recommends
# tuning the acceptance threshold for exactly this problem, and it does not work here:
# `en_core_web_sm` returns a flat **0.85 for every NER hit**, real or noise. That is why
# `DEFAULT_REDACTION_PII_CONFIDENCE` is not the lever, and raising it only loses recall.
#
# The trade is therefore deliberate and one-directional: company names and acronyms get
# masked so that a misfiled person's name does not leak. That is the recall-over-precision
# choice the PII literature recommends (a missed name is a privacy incident; a masked
# company name is noise), and it only ever applies to users who opted into PII masking —
# `pii` is not in DEFAULT_REDACTION_CATEGORIES, so this is opt-in twice over.
#
# ⚠️ **This does NOT make name detection exhaustive, and the UI says so.** The same
# measurement found `en_core_web_sm` missing person names ENTIRELY — "Dax Okonkwo",
# "Rivera" and "Sterling" produced no span at all, not even a mislabelled one. Adding
# ORGANIZATION cannot recover those. The higher-recall path is the GLiNER detector
# (`REDACTION_PII_USE_GLINER`, default off), which is a PII-specific model rather than a
# general-purpose NER.
DEFAULT_REDACTION_PII_ENTITIES = list(REDACTION_PII_ENTITIES)
DEFAULT_REDACTION_STYLE = "label"
DEFAULT_REDACTION_TOXICITY_THRESHOLD = 0.5
DEFAULT_REDACTION_REDACT_BEFORE_LLM = True
DEFAULT_REDACTION_DEFAULT_EXPORT_REDACTED = True

# Substituted for a segment whose masking raised. Never fall back to the original
# text: under redact_before_llm the whole point is that unmaskable content must not
# reach a third-party provider. See services/redaction/llm_guard.py.
# =============================================================================
# Amazon Bedrock
# =============================================================================
# Cross-region inference profiles are addressed by a geography-prefixed model ID.
# The prefix is derived from the AWS region's leading segment so an operator only
# has to set a bare model ID; a fully-qualified ID or profile ARN bypasses this.
# `us-gov` is intentionally absent: GovCloud regions split as "us"/"gov"/"west" and
# would otherwise pick up the commercial "us." prefix, so GovCloud deployments must
# set BEDROCK_MODEL_NAME to an explicit `us-gov.`-prefixed ID.
BEDROCK_GEO_PREFIX_BY_REGION = {
    "us": "us.",
    "eu": "eu.",
    "ap": "apac.",
    "ca": "us.",  # Canada routes into the US geography
    "sa": "us.",  # South America likewise
    "me": "eu.",  # Middle East routes into the EU geography
    "af": "eu.",
}

REDACTION_LLM_FAILSAFE_TEXT = "[redacted — masking unavailable]"
# How many times an LLM task may defer itself waiting for detection spans before
# failing. 10 × 60s covers a slow CPU scan; past that something is wrong, and a
# loud failure beats an unbounded retry loop.
REDACTION_LLM_MAX_DEFERRALS = 10

# PII detection confidence floor (Presidio/GLiNER scores below this are dropped).
DEFAULT_REDACTION_PII_CONFIDENCE = 0.4

# WebSocket notification type for redaction completion.
NOTIFICATION_TYPE_REDACTION_STATUS = "redaction_status"

# Redaction lifecycle statuses (MediaFile.redaction_status).
REDACTION_STATUS_PENDING = "pending"
REDACTION_STATUS_PROCESSING = "processing"
REDACTION_STATUS_DONE = "done"
REDACTION_STATUS_FAILED = "failed"

# =============================================================================
# RAG chat (issue #52) — coded defaults for DB-backed SystemSettings.
# Every knob below is editable in the admin UI under its `chat.*` settings key
# with no restart; there are deliberately NO `.env` vars for chat.
# =============================================================================

# Retrieval shape. The candidate pool is what OpenSearch returns before
# reranking; final_chunks is what actually reaches the prompt. Capping chunks
# per file keeps one long recording from crowding out the rest of a multi-file
# selection — the whole point of chatting across transcripts.
DEFAULT_CHAT_RAG_CANDIDATE_POOL = 48  # chat.rag.candidate_pool
# 40/12 measured (#531, 2026-08-21): ~1.8-2x AMI-81 answer recall over 12/4 on two
# corpora, judge-corroborated, negative controls intact, +13% latency. With ~4 files
# in scope the per-file cap binds first, so raising final_chunks alone does nothing.
DEFAULT_CHAT_RAG_FINAL_CHUNKS = 40  # chat.rag.final_chunks
DEFAULT_CHAT_RAG_MAX_CHUNKS_PER_FILE = 12  # chat.rag.max_chunks_per_file

# Cross-encoder reranking (CPU-only, lazily loaded in the backend container).
DEFAULT_CHAT_RAG_RERANK_ENABLED = True  # chat.rag.rerank_enabled
DEFAULT_CHAT_RAG_RERANK_MAX_PAIRS = 50  # chat.rag.rerank_max_pairs
CHAT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Multilingual reranker arm (issue #453/ML1) — a SELECTABLE second model, default OFF.
# `chat/reranker.py` skips reranking outright when the candidate pool is predominantly
# non-English, because CHAT_RERANKER_MODEL above is an English MS MARCO model and
# `rerank()` OVERWRITES `hit.score`, so letting it run destroys a correct retrieval
# order using a model that cannot read the text. This constant is the mechanism for a
# MEASURED alternative to that skip, not the decision to use it — the skip stays the
# shipped behaviour (this defaults False) until a sweep says otherwise.
#
# Licence, verified 2026-08-20 (huggingface.co model cards, not memory): bge-reranker-v2-m3
# is Apache-2.0. The only other shippable multilingual candidate is bge-reranker-base (MIT) —
# `jina-reranker-v2` is CC-BY-NC (non-commercial, unshippable in a published image) and the
# `gte`-family rerankers require `trust_remote_code=True` (arbitrary code execution from a
# downloaded model repo). Neither of those two is acceptable here.
DEFAULT_CHAT_RAG_MULTILINGUAL_RERANK_ENABLED = False  # chat.rag.multilingual_rerank_enabled
CHAT_RERANKER_MODEL_MULTILINGUAL = "BAAI/bge-reranker-v2-m3"  # Apache-2.0

# Conversational query rewriting: expands pronouns/references ("what about her?")
# into a standalone query before retrieval.
DEFAULT_CHAT_RAG_QUERY_REWRITE_ENABLED = True  # chat.rag.query_rewrite_enabled

# Retrieval caching. Tier 1 is an exact-query cache; tier 2 (opt-in) reuses
# results for semantically near-identical questions.
DEFAULT_CHAT_RAG_CACHE_TTL_SECONDS = 300  # chat.rag.cache_ttl_seconds
DEFAULT_CHAT_RAG_SEMANTIC_CACHE_ENABLED = False  # chat.rag.semantic_cache_enabled
DEFAULT_CHAT_RAG_SEMANTIC_CACHE_THRESHOLD = 0.97  # chat.rag.semantic_cache_threshold

# Conversation shape and abuse controls.
DEFAULT_CHAT_HISTORY_MAX_TURNS = 10  # chat.history_max_turns
DEFAULT_CHAT_MESSAGES_PER_HOUR = 120  # chat.limits.messages_per_hour
DEFAULT_CHAT_MAX_CONCURRENT_STREAMS = 2  # chat.limits.max_concurrent_streams
DEFAULT_CHAT_RETENTION_DAYS = 0  # chat.retention_days (0 = keep forever)

# W2.4: the aggregation tier's speaker-facet/speaker-stats fixes. Both default
# OFF — they change what an existing question mechanism answers (facet scope)
# or add a new one (talk-time stats), and either is a measurement-gated rollout
# rather than a safe-by-construction default.
DEFAULT_CHAT_AGGREGATE_SPEAKER_FACET_CONTENT_SCOPE = (
    False  # chat.aggregate.speaker_facet_content_scope
)
DEFAULT_CHAT_AGGREGATE_SPEAKER_STATS_ENABLED = False  # chat.aggregate.speaker_stats_enabled

# #464: prefer each file's LLM summary over its digest in the bounded-scope map
# tier (`chat/mapreduce.scope_digest_hits`), when the summary is FRESH (its
# stored source_fingerprint matches the file's current file_facts row) — an
# absent or stale summary falls back to the digest unconditionally. Default OFF:
# on-by-default needs measured answer-quality evidence this flag does not yet
# have (a separate, later change).
DEFAULT_CHAT_MAP_TIER_SUMMARIES = False  # chat.rag.map_tier_summaries

# A provider that accepts the request but never emits a first token would
# otherwise hold the stream open until the read timeout.
DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S = 90

# Per-user preferences (UserSetting keys `chat.system_prompt`,
# `chat.use_context_default`, `chat.default_search_mode`).
DEFAULT_CHAT_SYSTEM_PROMPT = ""
DEFAULT_CHAT_USE_CONTEXT = True
DEFAULT_CHAT_SEARCH_MODE = "hybrid"

# Resolved-scope ceiling: a selection resolving to more files than this is
# rejected (HTTP 400) rather than silently truncated.
CHAT_MAX_SCOPE_FILES = 500

# W2.2: deterministic, Postgres-only speaker-mention resolution
# (`services/chat/speaker_resolver.py`) — matches a name typed in the question
# text against the caller's accessible roster and, on a unique match paired
# with a speaker-verb frame ("what did X say"), adds a PARALLEL speaker-scoped
# retrieval leg. Never replaces or narrows the main leg, and an explicit
# checkbox scope (`ChatScope.speakers`) is unaffected either way. Default OFF:
# this is a new, unmeasured retrieval shape.
DEFAULT_CHAT_SPEAKER_RESOLVER_ENABLED = False  # chat.speaker_resolver_enabled

# W2.3: extends #464's map-tier-summaries pattern to the per-speaker map
# (`chat/mapreduce.scope_speaker_digest_hits`). When a fresh LLM summary exists
# for a file, prefer its `summary_data.speakers_analysis[]` entry (plus
# owner-matched action items) for the focus speaker over the per-sentence
# digest fallback. Default OFF for the same reason #464 is: on-by-default
# needs measured answer-quality evidence this flag does not yet have.
DEFAULT_CHAT_MAP_TIER_SPEAKER_SUMMARIES = False  # chat.rag.map_tier_speaker_summaries

# W2.5: cross-meeting recurrence detection — "what keeps coming up across our
# meetings". Gates BOTH the router's recurrence lexicon (`router.classify`)
# and the `<recurrence>` evidence block (`aggregation_service.answer_recurrence`),
# so flag-off is byte-identical to before this feature existed on every layer,
# not just the shape. Default OFF: a new, unmeasured retrieval/synthesis shape
# whose masking subject also follows an unresolved-in-general policy question
# (issue #402) — see `services/chat/CLAUDE.md`.
DEFAULT_CHAT_RECURRENCE_ENABLED = False  # chat.recurrence_enabled

# W2.6: the LLM query planner + parallel-leg fan-out
# (`services/chat/planner.py` + `services/chat/legs.py`). OFF by default: with
# no LLM configured (or the flag off) every turn routes exactly as it did
# before this module existed — rules-only routing, D6's "no LLM provider is
# still a first-class deployment" holds because nothing here is on the
# no-flag path. `needs_plan()` is pure and costs nothing to evaluate; only
# actually BUILDING a plan (an LLM call) is gated on this flag.
DEFAULT_CHAT_PLANNER_ENABLED = False  # chat.planner_enabled
# Ceiling on the process-wide leg executor (`legs.get_executor`). Sized once,
# at first use — a later change to this setting takes a process restart, the
# same trade every other lazy singleton in this package makes (see
# `reranker.py`).
DEFAULT_CHAT_PLANNER_MAX_PARALLEL_LEGS = 4  # chat.planner.max_parallel_legs
# W2.6: a single bounded non-streaming call that reconciles merged evidence
# from a multi-leg fan-out into a `<synthesis>` block. OFF by default and
# INDEPENDENT of `chat.planner_enabled` — a deployment can run the fan-out
# without ever paying for the extra reconciliation call.
DEFAULT_CHAT_ENRICHMENT_ENABLED = False  # chat.enrichment_enabled

# Issue #523: read-time "small-to-big" context expansion. A short retrieved
# chunk (`services/chat/context_expansion.py`'s `SHORT_CHUNK_WORD_THRESHOLD`)
# is widened to its surrounding exchange BY TIMESTAMP before it reaches
# `redactor.mask_chunks` — masking still applies to every widened word (the
# widened time range is what the cached-span rebuild reads), and the growth
# is bounded so it comes out of the SAME excerpt budget without silently
# evicting other files' evidence (`MAX_EXPANSION_SEGMENTS`/
# `MAX_EXPANDED_WORDS`). Default OFF: a new, unmeasured retrieval shape, same
# posture as every other W2.x flag above.
DEFAULT_CHAT_CONTEXT_EXPANSION_ENABLED = False  # chat.rag.context_expansion_enabled

# --- #532 synthesis-gap EXPERIMENT flags. -----------------------------------
# The measured defect: retrieval OFFERS 99% of a multi-file scope, the answer
# cites 75% — and the worst observed turn cited one excerpt for every claim
# while holding an overview of all 4 recordings. These three flags are the
# one-variable-at-a-time arms of that experiment, matching the published
# multi-document "dispersion"/position-bias literature. ⚠️ EXPERIMENT flags,
# not features: after measurement each is either promoted to default and the
# flag DELETED, or reverted and DELETED with its arm table on #532. Do not
# build on them.
DEFAULT_CHAT_OVERVIEW_CITABLE = False  # chat.rag.overview_citable
DEFAULT_CHAT_OVERVIEW_BLOCK_RULE = False  # chat.rag.overview_block_rule
DEFAULT_CHAT_OVERVIEW_AFTER_EXCERPTS = False  # chat.rag.overview_after_excerpts


# =============================================================================
# Document ingestion (issue #362 / #403 Stage 6)
# =============================================================================
# Two env vars only, and both are *wiring* (where a container lives), not policy —
# per the repo rule, everything a user or admin would tune is a DB-backed
# `SystemSettings` row with a `DEFAULT_DOCUMENT_*` coded default below.

# Which parsing tier to use: auto | slim | serve | tika.
#   auto  — the sidecar when DOCUMENT_PARSER_URL health-checks, else the in-worker
#           slim tier, else Tika. This is the single branch point
#           (`services/documents/registry.get_parser_for`).
#   slim  — in-worker only. No OCR, no layout model, no table structure.
#   serve — sidecar only. Unreachable becomes a RETRYABLE failure, not a parse failure.
#   tika  — the legacy OLE2/RTF tier, on its own, for exercising that path.
DOCUMENT_PARSER_BACKEND = _os.environ.get("DOCUMENT_PARSER_BACKEND", "auto").lower()

# Base URL of the docling-serve sidecar. Empty (the default) disables the tier entirely,
# which is what a lean deployment wants; `./opentr.sh start dev --with-documents` sets it to
# http://docling-serve:5001. The sidecar converts arbitrary user bytes with no
# authentication, so docker-compose.documents.yml publishes it on **127.0.0.1 only**
# (DOCLING_SERVE_PORT, default 5197) — never on 0.0.0.0. It is published at all for one
# reason: host-side pytest has to drive the OCR path against a real sidecar, because a mock
# would only prove the mock.
DOCUMENT_PARSER_URL = _os.environ.get("DOCUMENT_PARSER_URL", "").strip()

# Base URL of the optional Apache Tika container. Empty (the default) means legacy OLE2
# and RTF uploads are refused with "convert to .docx or .pdf first" — a better answer
# than a worse parse. Same loopback-only publication as the sidecar above (TIKA_PORT,
# default 5198), for the same reason.
DOCUMENT_TIKA_URL = _os.environ.get("DOCUMENT_TIKA_URL", "").strip()

# --- DB-backed defaults (SystemSettings keys in the comments) ----------------

# `documents.ocr_enabled` — the global OCR switch. On by default: OCR is day-one scope,
# and a scanned PDF that silently indexes as empty is the failure mode this exists to
# avoid.
DEFAULT_DOCUMENT_OCR_ENABLED = True

# `documents.ocr_policy` — auto | force | never. `auto` OCRs only what has no usable
# text layer.
DEFAULT_DOCUMENT_OCR_POLICY = "auto"

# `documents.ocr_text_threshold` — characters per page below which a PDF is treated as
# having no usable text layer. Measured over the corpora: olmOCR-bench `old_scans` sits
# at 0 chars/page across 60 PDFs, `tables` at ~2,300, so 100 separates them by an order
# of magnitude at both ends.
DEFAULT_DOCUMENT_OCR_TEXT_THRESHOLD = 100

# `documents.ocr_shard_pages` — pages per OCR shard. The whole point of sharding is
# fairness, not throughput: a 500-page scan becomes ~25 interleaved ~1-minute tasks
# instead of one 25-minute queue-starver (worker_prefetch_multiplier=1 is global).
DEFAULT_DOCUMENT_OCR_SHARD_PAGES = 20

# `documents.ocr_batch_size` — pages per ONNX batch INSIDE one shard. Follows the
# SEARCH_NEURAL_BATCH_SIZE precedent: batch by default, retry once unbatched on failure
# so one malformed page cannot fail a whole shard. Bounded by GPU HOLD TIME, not just
# VRAM — a bigger batch holds the admission lock longer, which is exactly the contention
# transcription must not lose.
DEFAULT_DOCUMENT_OCR_BATCH_SIZE = 4

# `documents.max_pages` — hard page ceiling. A trip truncates WITH a warning rather than
# failing: half of a 5,000-page document beats none of it, as long as it is said.
DEFAULT_DOCUMENT_MAX_PAGES = 2000

# `documents.max_upload_bytes` — separate from MAX_UPLOAD_BYTES (15 GB, sized for video).
# A 15 GB "document" is an attack, not a use case.
DEFAULT_DOCUMENT_MAX_UPLOAD_BYTES = 256 * 1024 * 1024

# `documents.chunk_target_words` — deliberately reads the transcript chunker's target at
# call time rather than declaring a second number: heterogeneous chunk lengths distort
# RRF, and two settings that must agree are one that will not.
