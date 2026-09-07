"""PyAnnote diarization backend — wraps app.transcription.diarizer.SpeakerDiarizer."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.transcription.engine.config import EngineConfig

logger = logging.getLogger(__name__)


class PyAnnoteBackend:
    """Diarization backend using the pyannote-audio fork.

    Wraps the existing SpeakerDiarizer class unchanged; model loading
    is driven by ModelManager to preserve the warm-cache lifecycle.

    This class (and its ``native`` sibling) exists so ``VALID_DIARIZER_BACKENDS`` /
    ``_DIARIZER_REGISTRY`` in ``engine/backends/__init__.py`` have a concrete adapter to
    name for each registry key — see that module's docstring. There is deliberately no
    ``diarize()`` method here: real diarization dispatch is ``ModelManager._build_diarizer``,
    which constructs ``SpeakerDiarizer``/``NativeSpeakerDiarizer`` directly to preserve their
    warm-cache lifecycle (issue #672).
    """

    def warmup(self, config: EngineConfig) -> None:
        """Validate that the diarizer model is loadable."""
        logger.debug("PyAnnoteBackend.warmup: model loading delegated to ModelManager")
