"""ASR provider catalog: the admin UI's provider/model/price/capability table.

Split from ``factory.py``; the factory resolves and builds providers, this module only
holds the data and the one function that reads a capability out of an entry.
"""

from __future__ import annotations

from app.core.local_whisper_catalog import provider_model_dicts

# Two capability fields every provider entry carries (a model entry overrides where it
# differs), both filled from what the provider MODULE does, never from vendor docs:
#
# - ``speaker_count_hints``: which of min/max/num speakers the module actually sends.
#   ``tests/unit/test_asr_catalog_speaker_hints.py`` derives the set from each module's
#   source and fails on a mismatch.
# - ``timestamp_granularity``: ``word`` (per-word times), ``segment`` (segment times only)
#   or ``none`` (text with no usable times, so speakers cannot be assigned to it).
#   ``tests/unit/test_asr_catalog_timestamp_granularity.py`` feeds each parser a fixture
#   response and fails when the parsed result cannot honour the declared value.
#
# ``services/pipeline_plan.py`` reads both to decide how speaker turns reach the text.
ASR_PROVIDER_CATALOG: dict = {
    "local": {
        "id": "local",
        "display_name": "Local (GPU)",
        "requires_api_key": False,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        # Honoured by the in-process PyAnnote engine only; the diar-native engine ignores
        # them. pipeline_plan resolves the real set from engine.diarizer_backend.
        "speaker_count_hints": ["min", "max", "num"],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": True,
        "description": "Local GPU transcription via faster-whisper + PyAnnote",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Excellent — PyAnnote provides best-in-class speaker diarization",
        "models": provider_model_dicts(),
    },
    "deepgram": {
        "id": "deepgram",
        "display_name": "Deepgram",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": [],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Nova-3 flagship model with medical terminology support",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Fair — may detect fewer speakers than local PyAnnote. Consider using local diarization for multi-speaker content.",
        "models": [
            {
                "id": "nova-3",
                "display_name": "Nova-3",
                "description": "Flagship, best accuracy, 36+ languages",
                "price_per_min_batch": 0.0043,
                "price_per_min_stream": 0.0077,
                "languages": 36,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
            {
                "id": "nova-3-medical",
                "display_name": "Nova-3 Medical",
                "description": "Medical terminology optimized, English only",
                "price_per_min_batch": 0.0043,
                "price_per_min_stream": 0.0077,
                "languages": 1,
                "is_medical": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
            {
                "id": "nova-2",
                "display_name": "Nova-2",
                "description": "Legacy, stable, 36 languages",
                "price_per_min_batch": 0.0043,
                "price_per_min_stream": 0.0077,
                "languages": 36,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
        ],
    },
    "assemblyai": {
        "id": "assemblyai",
        "display_name": "AssemblyAI",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": ["min", "max", "num"],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Best-in-class diarization with the Universal speech models",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Good — AssemblyAI offers strong diarization with speaker count support",
        # Only universal-3-pro and universal-2 are accepted by the current speech_models API;
        # slam-1 / nano were rejected live ("must be one of universal-3-pro, universal-2").
        "models": [
            {
                "id": "universal",
                "display_name": "Universal-3 Pro",
                "description": "Highest accuracy — English, Spanish, Portuguese, French, German, Italian",
                "price_per_min_batch": 0.0045,
                "languages": 6,
                "is_default": True,
                "supports_diarization": True,
            },
            {
                "id": "universal-multilingual",
                "display_name": "Universal-2 (99 languages)",
                "description": "Multilingual with automatic language detection",
                "price_per_min_batch": 0.0045,
                "languages": 99,
                "supports_diarization": True,
            },
        ],
    },
    "openai": {
        "id": "openai",
        "display_name": "OpenAI",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": False,
        "speaker_count_hints": [],
        # gpt-4o-transcribe / gpt-4o-mini-transcribe (and any model not listed) are asked
        # for "json", which carries no times; the parser makes one 0.0/0.0 segment.
        "timestamp_granularity": "none",
        "supports_vocabulary": False,
        "supports_translation": True,
        "description": "Whisper-1 and GPT-4o Transcribe (25 MB file limit)",
        "status": "experimental",
        "status_note": "This provider has not been fully tested with real API keys. Results may vary.",
        "diarization_quality": "",
        "models": [
            {
                "id": "gpt-4o-transcribe",
                "display_name": "GPT-4o Transcribe",
                "description": "Higher accuracy than Whisper-1",
                "price_per_min_batch": 0.006,
                "is_default": True,
                "max_file_size_mb": 25,
                "supports_confidence": True,
                "supports_translation": False,
            },
            {
                "id": "gpt-4o-transcribe-diarize",
                "display_name": "GPT-4o Transcribe + Diarize",
                "description": "Built-in speaker labels",
                "price_per_min_batch": 0.006,
                "max_file_size_mb": 25,
                "supports_diarization": True,
                "supports_translation": False,
                "timestamp_granularity": "segment",
            },
            {
                "id": "whisper-1",
                "display_name": "Whisper-1",
                "description": "99 languages, supports translation to English",
                "price_per_min_batch": 0.006,
                "languages": 99,
                "max_file_size_mb": 25,
                "supports_translation": True,
                # verbose_json without timestamp_granularities: segment times only.
                "timestamp_granularity": "segment",
            },
        ],
    },
    "google": {
        "id": "google",
        "display_name": "Google Cloud Speech",
        "requires_api_key": False,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": ["min", "max"],
        "timestamp_granularity": "word",
        "supports_vocabulary": False,
        "supports_translation": False,
        "description": "Chirp 3 model with 100+ language support (uses service account credentials)",
        "status": "experimental",
        "status_note": "This provider has not been fully tested with real API keys. Results may vary.",
        "diarization_quality": "Unknown — supports min/max speaker count parameters",
        "models": [
            {
                "id": "chirp-3",
                "display_name": "Chirp 3",
                "description": "Best multilingual, 100+ languages",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "is_default": True,
                "supports_diarization": True,
            },
            {
                "id": "long",
                "display_name": "Long Audio",
                "description": "Optimised for long recordings",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "supports_diarization": True,
            },
            {
                "id": "short",
                "display_name": "Short Audio",
                "description": "Optimised for short clips",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "supports_diarization": True,
            },
        ],
    },
    "azure": {
        "id": "azure",
        "display_name": "Azure Speech",
        "requires_api_key": True,
        "requires_region": True,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": [],
        # Word times are parsed only when the SDK fills NBest[0].Words, which is never
        # explicitly requested; treated as segment until a live response proves otherwise.
        "timestamp_granularity": "segment",
        "supports_vocabulary": False,
        "supports_translation": False,
        "description": "Azure Speech Services — Whisper and Standard models",
        "status": "experimental",
        "status_note": "This provider has not been fully tested with real API keys. Results may vary.",
        "diarization_quality": "Unknown — uses ConversationTranscriber for diarization",
        "models": [
            {
                "id": "whisper",
                "display_name": "Azure Whisper",
                "description": "Whisper via Azure, 99 languages",
                "price_per_min_batch": 0.017,
                "languages": 99,
                "is_default": True,
                "supports_diarization": True,
            },
            {
                "id": "standard",
                "display_name": "Standard",
                "description": "Azure's own model, 100+ languages",
                "price_per_min_batch": 0.017,
                "languages": 100,
                "supports_diarization": True,
            },
        ],
    },
    "aws": {
        "id": "aws",
        "display_name": "Amazon Transcribe",
        "requires_api_key": True,  # the AWS Secret Access Key (stored in api_key, encrypted)
        "requires_access_key_id": True,  # the AWS Access Key ID (encrypted, AWS-only)
        "requires_region": True,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": ["max"],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Standard and Medical transcription (HIPAA-eligible)",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Supports up to 30 speakers",
        "models": [
            {
                "id": "standard",
                "display_name": "Standard (single language)",
                "description": "One language per file — uses your source language, or "
                "auto-detects the dominant language when set to Auto",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
            {
                "id": "multilingual",
                "display_name": "Multilingual (code-switching)",
                "description": "Multiple languages within one file — detects and transcribes "
                "each language (e.g. a bilingual speaker switching mid-conversation)",
                "price_per_min_batch": 0.024,
                "languages": 100,
                "supports_diarization": True,
                "supports_vocabulary": False,
            },
            {
                "id": "medical",
                "display_name": "Medical",
                "description": "HIPAA-eligible, medical vocabulary",
                "price_per_min_batch": 0.075,
                "is_medical": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
        ],
    },
    "speechmatics": {
        "id": "speechmatics",
        "display_name": "Speechmatics",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": [],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "Enterprise-grade with 55+ languages and speaker diarization",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Strong — enterprise speaker diarization",
        "models": [
            {
                "id": "standard",
                "display_name": "Standard",
                "description": "55+ languages, includes diarization",
                "price_per_min_batch": 0.004,
                "languages": 55,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
        ],
    },
    "gladia": {
        "id": "gladia",
        "display_name": "Gladia",
        "requires_api_key": True,
        "requires_region": False,
        # True since issue #594 wired UserASRSettings.base_url into the real request
        # path (GladiaProvider._resolve_base_url, guarded by
        # ASR_ALLOW_PRIVATE_ENDPOINTS). Every other provider below still drops a
        # configured base_url silently, so their entries stay False — flipping one
        # without wiring the field would resurface #594 under a different vendor.
        "supports_custom_url": True,
        "supports_diarization": True,
        "speaker_count_hints": [],
        "timestamp_granularity": "word",
        "supports_vocabulary": True,
        "supports_translation": False,
        "description": "All features bundled, 100+ languages",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Good — diarization included in all plans",
        "models": [
            {
                "id": "standard",
                "display_name": "Standard",
                "description": "All features included, 100+ languages",
                "price_per_min_batch": 0.010,
                "languages": 100,
                "is_default": True,
                "supports_diarization": True,
                "supports_vocabulary": True,
            },
        ],
    },
    "pyannote": {
        "id": "pyannote",
        "display_name": "pyannote.ai",
        "requires_api_key": True,
        "requires_region": False,
        "supports_custom_url": False,
        "supports_diarization": True,
        "speaker_count_hints": ["min", "max", "num"],
        "timestamp_granularity": "word",
        "supports_vocabulary": False,
        "supports_translation": False,
        "description": "STT Orchestration — premium diarization + transcription in one API call",
        "status": "tested",
        "status_note": "",
        "diarization_quality": "Excellent — premium cloud diarization from pyannote.ai",
        "models": [
            {
                "id": "parakeet",
                "display_name": "Parakeet (NVIDIA)",
                "description": "Fast, accurate, 100 languages, bundled with premium diarization",
                "price_per_min_batch": 0.027,
                "languages": 100,
                "is_default": True,
                "supports_diarization": True,
            },
            {
                "id": "whisper-large-v3-turbo",
                "display_name": "Whisper Large V3 Turbo",
                "description": "OpenAI Whisper via pyannote, bundled with premium diarization",
                "price_per_min_batch": 0.027,
                "languages": 100,
                "supports_diarization": True,
            },
        ],
    },
}


def _capabilities(provider_entry: dict, model_entry: dict | None) -> dict:
    """One catalog entry's capabilities; a model entry's value wins over its provider's."""
    model = model_entry or {}

    def pick(key: str, default):
        return model.get(key, provider_entry.get(key, default))

    return {
        "supports_translation": pick("supports_translation", True),
        "language_support": model.get("language_support", "multilingual"),
        "languages": model.get("languages"),
        "supports_diarization": bool(pick("supports_diarization", False)),
        "speaker_count_hints": list(pick("speaker_count_hints", [])),
        "timestamp_granularity": pick("timestamp_granularity", "none"),
    }
