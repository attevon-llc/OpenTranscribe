"""The one catalog of local Whisper models a deployment can download and select.

Stdlib only, on purpose: ``schemas/media.py`` (the per-file model validator), the ASR provider
catalog, the discovery scan, the ``/asr-settings/local-model/set`` allow-list and
``scripts/download-models.py`` all read it, and none of them may pay for an import of
``app.services.asr`` (which pulls in every cloud provider) or of a model library.

Sizes are the sum of the files :data:`ALLOW_PATTERNS` selects, measured from the Hugging Face
file-metadata API on 2026-10-15 (``HfApi.model_info(repo, files_metadata=True)``). VRAM figures
are float16 inference *hints* carried over from the earlier hand-typed provider catalog; they
have not been measured here, so a UI must label them as estimates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Equal to the ``allow_patterns`` of ``faster_whisper.utils.download_model``; a unit test reads
# that function's source so a faster-whisper bump that changes the set fails by name.
ALLOW_PATTERNS: tuple[str, ...] = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
)

LanguageSupport = Literal["multilingual", "english_only", "english_optimized"]


@dataclass(frozen=True)
class LocalWhisperModel:
    """One downloadable local Whisper model.

    Attributes:
        id: What is stored in ``asr.local_model`` and sent by the UI.
        repo_id: Hugging Face repository holding the CTranslate2 build.
        display_name: Human label.
        approx_bytes: Sum of the :data:`ALLOW_PATTERNS` files in the repository.
        vram_hint_gb: Hint for float16 inference, not a measurement.
        language_support: ``multilingual``, ``english_only`` or ``english_optimized``.
        supports_translation: Whether the model can translate to English.
        is_default: Whether this is the model a fresh deployment uses.
    """

    id: str
    repo_id: str
    display_name: str
    approx_bytes: int
    vram_hint_gb: float
    language_support: LanguageSupport
    supports_translation: bool
    is_default: bool = False


def _m(
    model_id: str,
    repo_id: str,
    display_name: str,
    approx_bytes: int,
    vram_hint_gb: float,
    language_support: LanguageSupport,
    supports_translation: bool,
    is_default: bool = False,
) -> LocalWhisperModel:
    return LocalWhisperModel(
        model_id,
        repo_id,
        display_name,
        approx_bytes,
        vram_hint_gb,
        language_support,
        supports_translation,
        is_default,
    )


# Order is the order the UI lists them and the provider catalog serves them: the default first,
# then by size descending, then the English-only and legacy builds.
LOCAL_WHISPER_MODELS: tuple[LocalWhisperModel, ...] = (
    _m(
        "large-v3-turbo",
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "Large V3 Turbo",
        1_621_665_983,
        6,
        "english_optimized",
        False,
        True,
    ),
    _m(
        "large-v3",
        "Systran/faster-whisper-large-v3",
        "Large V3",
        3_090_835_702,
        10,
        "multilingual",
        True,
    ),
    _m(
        "large-v2",
        "Systran/faster-whisper-large-v2",
        "Large V2",
        3_089_578_858,
        10,
        "multilingual",
        True,
    ),
    _m(
        "large-v1",
        "Systran/faster-whisper-large-v1",
        "Large V1",
        3_089_578_414,
        10,
        "multilingual",
        True,
    ),
    _m(
        "medium",
        "Systran/faster-whisper-medium",
        "Medium",
        1_530_571_735,
        5,
        "multilingual",
        True,
    ),
    _m(
        "medium.en",
        "Systran/faster-whisper-medium.en",
        "Medium (English)",
        1_530_457_748,
        5,
        "english_only",
        False,
    ),
    _m(
        "small",
        "Systran/faster-whisper-small",
        "Small",
        486_212_372,
        2,
        "multilingual",
        True,
    ),
    _m(
        "small.en",
        "Systran/faster-whisper-small.en",
        "Small (English)",
        486_098_798,
        2,
        "english_only",
        False,
    ),
    _m(
        "base",
        "Systran/faster-whisper-base",
        "Base",
        147_882_941,
        1,
        "multilingual",
        True,
    ),
    _m(
        "base.en",
        "Systran/faster-whisper-base.en",
        "Base (English)",
        147_769_510,
        1,
        "english_only",
        False,
    ),
    _m(
        "tiny",
        "Systran/faster-whisper-tiny",
        "Tiny",
        78_203_619,
        1,
        "multilingual",
        True,
    ),
    _m(
        "tiny.en",
        "Systran/faster-whisper-tiny.en",
        "Tiny (English)",
        78_090_594,
        1,
        "english_only",
        False,
    ),
    # The CTranslate2 build is the only CrisperWhisper variant faster-whisper can load; the
    # PyTorch checkpoint nyrahealth/CrisperWhisper has no model.bin. It is a large-v3-sized
    # model, so the old catalog's "~3 GB VRAM" was almost certainly wrong; 10 is a hint to
    # be measured.
    _m(
        "nyrahealth/faster_CrisperWhisper",
        "nyrahealth/faster_CrisperWhisper",
        "CrisperWhisper (English)",
        3_091_111_230,
        10,
        "english_only",
        False,
    ),
)

LOCAL_WHISPER_MODEL_IDS: frozenset[str] = frozenset(m.id for m in LOCAL_WHISPER_MODELS)

_BY_ID: dict[str, LocalWhisperModel] = {m.id: m for m in LOCAL_WHISPER_MODELS}

ALIASES: dict[str, str] = {
    "crisperwhisper": "nyrahealth/faster_CrisperWhisper",
    "faster_crisperwhisper": "nyrahealth/faster_CrisperWhisper",
    "nyrahealth/CrisperWhisper": "nyrahealth/faster_CrisperWhisper",
}


def get(model_id: str) -> LocalWhisperModel | None:
    """Return the catalog entry for an exact id, or ``None``."""
    return _BY_ID.get(model_id)


def canonical_id(name: str) -> str | None:
    """Resolve a name to a catalog id, applying :data:`ALIASES`.

    Args:
        name: A catalog id or a known alias.

    Returns:
        The catalog id, or ``None`` when the name is not in the catalog. Repository ids that
        are not catalog entries are never accepted.
    """
    resolved = ALIASES.get(name, name)
    return resolved if resolved in _BY_ID else None


def provider_model_dicts() -> list[dict]:
    """Return the entries the ASR provider catalog serves as ``local.models``.

    The descriptions are fixed text built from the catalog's hints, so the provider catalog
    and the Settings model library cannot disagree about a model.
    """
    return [
        {
            "id": m.id,
            "display_name": m.display_name,
            "description": (
                f"~{m.vram_hint_gb:g} GB VRAM (estimate)" + (", default" if m.is_default else "")
            ),
            "price_per_min_batch": 0,
            "supports_diarization": True,
            "supports_translation": m.supports_translation,
            "language_support": m.language_support,
            "approx_bytes": m.approx_bytes,
            "vram_hint_gb": m.vram_hint_gb,
            **({"languages": 1} if m.language_support == "english_only" else {}),
            **({"is_default": True} if m.is_default else {}),
        }
        for m in LOCAL_WHISPER_MODELS
    ]
