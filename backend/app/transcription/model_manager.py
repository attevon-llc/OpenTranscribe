"""Warm model caching for batch processing.

Keeps transcription and diarization models loaded between Celery tasks
to avoid repeated model loading overhead. Supports both sequential
(concurrency=1) and concurrent (--pool=threads) modes.

Pattern matches speaker_embedding_service.py::get_cached_embedding_service().
"""

import logging
import threading
from typing import ClassVar
from typing import cast

from app.transcription.config import TranscriptionConfig
from app.transcription.diarizer import SpeakerDiarizer
from app.transcription.transcriber import Transcriber

logger = logging.getLogger(__name__)


class ModelManager:
    """Keeps models warm across Celery tasks for batch processing.

    Singleton that persists models between tasks in the same worker process.
    When config changes (e.g., different model), the old model is released
    and a new one loaded.

    In concurrent mode (concurrent_requests > 1), both models are kept
    loaded permanently to avoid reload overhead when multiple threads
    share the same GPU weights.
    """

    _instance: ClassVar["ModelManager | None"] = None

    def __init__(self):
        self._transcriber: Transcriber | None = None
        self._diarizer: SpeakerDiarizer | None = None
        self._transcriber_hash: str | None = None
        self._diarizer_hash: str | None = None
        self._lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> "ModelManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_transcriber(self, config: TranscriptionConfig) -> Transcriber:
        """Return cached transcriber if config matches, else load new one."""
        config_hash = config.config_hash()

        with self._lock:
            if self._transcriber is not None and self._transcriber_hash == config_hash:
                logger.info(f"Reusing cached transcriber (hash={config_hash})")
                return self._transcriber

            # Config changed or first load — release old and load new
            if self._transcriber is not None:
                logger.info("Transcriber config changed, releasing old model")
                self._transcriber.unload_model()
                self._cleanup_gpu()

            transcriber = Transcriber(config)
            transcriber.load_model()
            self._transcriber = transcriber
            self._transcriber_hash = config_hash
            return transcriber

    def get_diarizer(self, config: TranscriptionConfig) -> SpeakerDiarizer:
        """Return cached diarizer if config matches, else load new one."""
        config_hash = config.config_hash()

        with self._lock:
            if self._diarizer is not None and self._diarizer_hash == config_hash:
                if self._diarizer_current(self._diarizer, config):
                    logger.info(f"Reusing cached diarizer (hash={config_hash})")
                    return self._diarizer
                # The sidecar went away, or came back — release and rebuild on the
                # engine that can serve now.
                self._diarizer.unload_model()
                self._cleanup_gpu()
                self._diarizer = None
                self._diarizer_hash = None

            if self._diarizer is not None:
                logger.info("Diarizer config changed, releasing old model")
                self._diarizer.unload_model()
                self._cleanup_gpu()

            diarizer = self._build_diarizer(config)
            self._diarizer = diarizer
            self._diarizer_hash = config_hash
            return diarizer

    def _build_diarizer(self, config: TranscriptionConfig) -> SpeakerDiarizer:
        """Construct and load the configured diarization engine.

        ``config.diarizer_backend`` (SystemSettings ``engine.diarizer_backend`` -> env
        ``ENGINE_DIARIZER_BACKEND`` -> default ``"native"``, resolved once by
        ``TranscriptionConfig._resolve_diarizer_backend`` and validated against
        ``engine.backends.VALID_DIARIZER_BACKENDS``) is the single decision point selecting
        the diarizer — this used to also be checked via an ad-hoc ``DIARIZER_ENGINE`` env
        read here and in ``engine/stages.py``; both are gone (issue #58). ``"native"``
        selects the diar-native sidecar client; anything that keeps it from answering falls
        back to the in-process PyAnnote engine, which is also what ``"pyannote"`` pins
        directly.
        """
        if config.diarizer_backend.lower() == "native":
            try:
                from app.transcription.diarizer_native import NativeSpeakerDiarizer

                native = NativeSpeakerDiarizer(config)
                native.load_model()
                # Duck-typed drop-in: identical diarize/embed_window/unload_model surface.
                return cast(SpeakerDiarizer, native)
            except Exception as exc:  # noqa: BLE001 — any failure means "use the fork"
                logger.warning("Native diarizer unavailable (%s); falling back to PyAnnote", exc)

        diarizer = SpeakerDiarizer(config)
        diarizer.load_model()
        return diarizer

    @staticmethod
    def _diarizer_current(diarizer: SpeakerDiarizer, config: TranscriptionConfig) -> bool:
        """Whether the cached diarizer is still the engine that should serve this task.

        With ``config.diarizer_backend == "native"`` the sidecar is probed per task, which
        keeps the engine choice tracking reality in both directions: losing the sidecar
        mid-queue falls back to PyAnnote, and a recovered sidecar is picked up again instead
        of the worker staying degraded until it restarts.
        """
        native_selected = config.diarizer_backend.lower() == "native"
        native_cached = type(diarizer).__name__ == "NativeSpeakerDiarizer"
        if not native_selected:
            return not native_cached

        from app.transcription.diarizer_native import sidecar_healthy

        healthy = sidecar_healthy()
        if native_cached and not healthy:
            logger.warning("diar-native sidecar is unreachable; falling back to PyAnnote")
        elif healthy and not native_cached:
            logger.info("diar-native sidecar is healthy again; leaving the PyAnnote fallback")
        return native_cached == healthy

    def ensure_models_loaded(self, config: TranscriptionConfig) -> None:
        """Preload both models for concurrent mode.

        Called during worker_process_init to have models ready before
        any tasks arrive. Both models stay resident for the worker lifetime.
        """
        logger.info("Preloading models for concurrent GPU worker...")
        self.get_transcriber(config)
        self.get_diarizer(config)
        logger.info("Both models preloaded and ready")

    def release_transcriber(self) -> None:
        """Free transcriber VRAM for sequential mode.

        In sequential mode, transcriber is released before loading diarizer
        to minimize peak VRAM usage. Skipped in concurrent mode.
        """
        with self._lock:
            if self._transcriber is not None:
                self._transcriber.unload_model()
                self._transcriber = None
                self._transcriber_hash = None
                self._cleanup_gpu()
                logger.info("Transcriber released for sequential mode")

    def release_all(self) -> None:
        """Free all models and VRAM."""
        with self._lock:
            if self._transcriber is not None:
                self._transcriber.unload_model()
                self._transcriber = None
                self._transcriber_hash = None

            if self._diarizer is not None:
                self._diarizer.unload_model()
                self._diarizer = None
                self._diarizer_hash = None

            self._cleanup_gpu()
            logger.info("All models released")

    def _cleanup_gpu(self) -> None:
        """Run GPU memory cleanup."""
        try:
            from app.utils.hardware_detection import detect_hardware

            hw = detect_hardware()
            hw.optimize_memory_usage()
        except Exception as e:
            logger.debug(f"GPU cleanup skipped: {e}")
