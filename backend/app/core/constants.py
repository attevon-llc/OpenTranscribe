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
SEARCH_HYBRID_MIN_SCORE = 0.01
SEARCH_CACHE_TTL_SECONDS = 300
SEARCH_CACHE_MAX_SIZE = 256

# OpenSearch Native Neural Search Model Registry
# These models are registered and deployed directly in OpenSearch via ML Commons plugin
# Organized by quality tier (Fast → Balanced → Best) and language support (English / Multilingual)
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
    "huggingface/sentence-transformers/paraphrase-multilingual-mpnet-base-v2": {
        "name": "MPNet - Balanced (Multilingual, 50+ Languages)",
        "dimension": 768,
        "size_mb": 1100,
        "languages": ["multilingual"],
        "model_format": "TORCH_SCRIPT",
        "default": False,
        "requires_prefix": False,
        "tier": "balanced",
        "language_type": "multilingual",
        "description": "Higher quality multilingual embeddings. Good semantic search.",
    },
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

# Tag/collection source identifiers
TAG_SOURCE_MANUAL = "manual"
TAG_SOURCE_AUTO_AI = "auto_ai"
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
DEFAULT_REDACTION_CATEGORIES = ["profanity", "pii", "toxicity", "custom"]
# ORGANIZATION is excluded from defaults: spaCy NER over-tags acronyms/common nouns as
# ORG (e.g. "SSN" → ORGANIZATION), and org names are rarely sensitive PII. Still selectable.
DEFAULT_REDACTION_PII_ENTITIES = [e for e in REDACTION_PII_ENTITIES if e != "ORGANIZATION"]
DEFAULT_REDACTION_STYLE = "label"
DEFAULT_REDACTION_TOXICITY_THRESHOLD = 0.5
DEFAULT_REDACTION_REDACT_BEFORE_LLM = True
DEFAULT_REDACTION_DEFAULT_EXPORT_REDACTED = True

# PII detection confidence floor (Presidio/GLiNER scores below this are dropped).
DEFAULT_REDACTION_PII_CONFIDENCE = 0.4

# WebSocket notification type for redaction completion.
NOTIFICATION_TYPE_REDACTION_STATUS = "redaction_status"

# Redaction lifecycle statuses (MediaFile.redaction_status).
REDACTION_STATUS_PENDING = "pending"
REDACTION_STATUS_PROCESSING = "processing"
REDACTION_STATUS_DONE = "done"
REDACTION_STATUS_FAILED = "failed"
