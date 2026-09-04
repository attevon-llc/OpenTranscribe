"""Backend registry: maps backend names to implementation classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig

#: RESERVED extension point (E5, issue #366) — dynamic dispatch is exercised by
#: ``get_transcriber_backend`` below, but nothing in the pipeline currently calls it: the
#: transcription task selects an ASR provider directly (see
#: ``backend/app/services/asr/CLAUDE.md``), and ``FasterWhisperBackend`` is the only one of
#: the three below that is not a ``raise NotImplementedError`` stub. Kept, and documented as
#: the extension path in ``docs/combined-engine-design.md``, for issue #366 (NeMo/Parakeet
#: ASR) to register a fourth entry here rather than growing a second dispatch mechanism.
#: Do NOT delete this registry.
_TRANSCRIBER_REGISTRY: dict[str, str] = {
    "faster_whisper": (
        "app.transcription.engine.backends.transcribers.faster_whisper_backend.FasterWhisperBackend"
    ),
    "whisperx": ("app.transcription.engine.backends.transcribers.whisperx_backend.WhisperxBackend"),
    "cloud": ("app.transcription.engine.backends.transcribers.cloud_backend.CloudBackend"),
}

#: Unlike ``_TRANSCRIBER_REGISTRY``, there is deliberately no ``get_diarizer_backend()`` here
#: (issue #672 removed it) — the real runtime dispatch is ``ModelManager._build_diarizer``,
#: which constructs ``NativeSpeakerDiarizer`` / the in-process ``SpeakerDiarizer`` fork
#: directly to preserve their warm-cache lifecycle (see ``app/transcription/CLAUDE.md``), not
#: through this registry. ``NativeBackend``/``PyAnnoteBackend`` used to carry a ``diarize()``
#: method that was unreachable from anywhere, by any path — more thoroughly dead than a
#: merely-uncalled dispatcher — so it was deleted (#672); only ``warmup()`` remains on each.
#: The classes themselves are kept anyway, for a narrower reason than
#: ``_TRANSCRIBER_REGISTRY``'s: the dict's KEYS are load-bearing production code
#: (``VALID_DIARIZER_BACKENDS`` below feeds ``TranscriptionConfig._resolve_diarizer_backend``
#: and the admin engine-settings validator), and the two classes are that vocabulary's only
#: documentation of which implementation each key is supposed to name — deleting them would
#: leave "native"/"pyannote" as bare strings with no adapter to point at if a real per-backend
#: dispatch mechanism (rather than ModelManager's hardcoded branch) is ever built. Do NOT
#: delete this registry or the two adapter classes together with it.
_DIARIZER_REGISTRY: dict[str, str] = {
    # native (diar-native, Rust/speakrs) is the PRIMARY diarizer as of issue #58 — faster and
    # measurably better on AMI. "pyannote" is the explicit, documented failover: ModelManager
    # falls back to it automatically whenever the diar-native sidecar is unreachable, and an
    # admin can still pin it directly via this same registry key.
    "native": ("app.transcription.engine.backends.diarizers.native_backend.NativeBackend"),
    "pyannote": ("app.transcription.engine.backends.diarizers.pyannote_backend.PyAnnoteBackend"),
}

#: The valid set of ``engine.diarizer_backend`` / ``ENGINE_DIARIZER_BACKEND`` values — the
#: single vocabulary consumed by ``TranscriptionConfig._resolve_diarizer_backend`` (the
#: runtime decision point in ``ModelManager``) and by the admin engine-settings API (request
#: validation). Keeping both readers pointed at this tuple instead of a hardcoded list is what
#: makes the registry a real consolidation target rather than a second, driftable copy.
VALID_DIARIZER_BACKENDS: tuple[str, ...] = tuple(_DIARIZER_REGISTRY)

#: The valid set of ``engine.transcriber_backend`` / ``ENGINE_TRANSCRIBER_BACKEND`` values —
#: used only for admin-write validation (E5). The setting itself is presently read by a log
#: line only (see the reservation note above); this tuple stops
#: ``{"transcriber_backend": "anything"}`` from persisting silently.
VALID_TRANSCRIBER_BACKENDS: tuple[str, ...] = tuple(_TRANSCRIBER_REGISTRY)


def _import_class(dotted_path: str):
    module_path, class_name = dotted_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def get_transcriber_backend(name: str):
    """Instantiate and return the named transcriber backend."""
    if name not in _TRANSCRIBER_REGISTRY:
        raise ValueError(
            f"Unknown transcriber backend '{name}'. Available: {list(_TRANSCRIBER_REGISTRY)}"
        )
    cls = _import_class(_TRANSCRIBER_REGISTRY[name])
    return cls()
