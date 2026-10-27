"""Which diarization engine served a run, and why (issue #1184).

The native sidecar is the preferred engine; the in-process PyAnnote fork is the failover.
That failover works, which is exactly why it went unnoticed: a deployment could spend days on
the slower engine with every setting still reading "native". Every run is therefore recorded
here, and an UNPLANNED fallback is logged at WARNING on every run, with its reason.

``engine.diarizer_require_sidecar`` (see ``diarizer_native.diarizer_require_sidecar``) is the
strict mode: it refuses the fallback altogether. This module only reports; it never decides.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

REASON_SERVED = "served"
REASON_PINNED = "pinned"
REASON_NOT_READY = "sidecar_not_ready"
REASON_UNREACHABLE = "unreachable"
REASON_ERROR = "error"


def run_outcome(diarizer: Any) -> tuple[str, str]:
    """``(backend, reason)`` for the run ``diarizer`` just completed.

    Read off the diarizer instance after ``diarize()`` returned, like ``last_provider`` is
    (issue #706): never derived from the configured backend, which only says what was asked.
    """
    backend = str(getattr(diarizer, "last_provider", None) or "unknown")
    if backend == "native":
        return backend, REASON_SERVED
    # An instance that came from a fallback records why; one that did not was pinned.
    return backend, str(getattr(diarizer, "last_fallback_reason", None) or REASON_PINNED)


def record_diarization_run(diarizer: Any) -> None:
    """Count the run, and warn when it ran on the fallback. Never raises."""
    try:
        backend, reason = run_outcome(diarizer)
        from app.core.metrics import diarization_runs_total

        diarization_runs_total.labels(backend=backend, reason=reason).inc()
        if backend == "pyannote" and reason != REASON_PINNED:
            logger.warning(
                "Diarization ran on the in-process PyAnnote FALLBACK, not the diar-native "
                "sidecar (reason: %s). It is slower and holds GPU memory; check the sidecar. "
                "Set engine.diarizer_require_sidecar to fail instead of falling back.",
                reason,
            )
    except Exception:  # noqa: BLE001 - observability must never fail a transcription
        logger.debug("could not record the diarization run", exc_info=True)
