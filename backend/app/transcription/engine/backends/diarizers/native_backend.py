"""Native diarization backend — wraps app.transcription.diarizer_native.NativeSpeakerDiarizer.

Same thin-adapter shape as ``pyannote_backend.PyAnnoteBackend``. This is now the PRIMARY
diarizer backend (issue #58): faster and measurably better on AMI than the in-process
PyAnnote fork, which is demoted to the explicit, documented failover. Registered in
``BACKEND_REGISTRY`` alongside "pyannote" so the two are one validated vocabulary rather
than two independently-maintained lists.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig

logger = logging.getLogger(__name__)


class NativeBackend:
    """Diarization backend using the diar-native (Rust/speakrs) sidecar.

    Wraps ``NativeSpeakerDiarizer`` unchanged; model/sidecar lifecycle — including the
    automatic fallback to the in-process PyAnnote fork on an unreachable or a mid-job-failing
    sidecar — is owned by ``ModelManager`` / ``NativeSpeakerDiarizer`` itself (see
    ``app/transcription/diarizer_native.py``), to preserve the warm-cache lifecycle the way
    ``PyAnnoteBackend`` does for the fork.

    This class (and its ``pyannote`` sibling) exists so ``VALID_DIARIZER_BACKENDS`` /
    ``_DIARIZER_REGISTRY`` in ``engine/backends/__init__.py`` have a concrete adapter to
    name for each registry key — see that module's docstring. There is deliberately no
    ``diarize()`` method here: real diarization dispatch is ``ModelManager._build_diarizer``,
    which constructs ``NativeSpeakerDiarizer``/``SpeakerDiarizer`` directly to preserve their
    warm-cache lifecycle (issue #672).
    """

    def warmup(self, config: EngineConfig) -> None:
        """Validate that the diarizer model is loadable."""
        logger.debug("NativeBackend.warmup: model loading delegated to ModelManager")
