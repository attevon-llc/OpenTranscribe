"""OpenSearch embedding model tiers, speaker-index names and embedding dimensions."""

from app.core.config import settings


def get_speaker_index() -> str:
    """Get the speaker index alias name.

    This is an OpenSearch alias that points to whichever versioned index
    is currently active (speakers_v3 or speakers_v4). All reads should
    go through this alias. Writes should target the versioned index directly.
    """
    return settings.OPENSEARCH_SPEAKER_INDEX


def get_speaker_index_v3() -> str:
    """Get the v3 speaker embedding index name (512-dim, pyannote/embedding)."""
    return f"{get_speaker_index()}_v3"


def get_speaker_index_v4() -> str:
    """Get the v4 speaker embedding index name (256-dim, WeSpeaker)."""
    return f"{get_speaker_index()}_v4"


def get_speaker_index_v3_backup() -> str:
    """Get the legacy v3 backup index name (from pre-alias migrations)."""
    return f"{get_speaker_index()}_v3_backup"


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
