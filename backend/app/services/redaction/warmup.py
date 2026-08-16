"""Warm the PII analyzer in the API process (issue #74).

**The API process runs Presidio too**, and until this module existed it always paid
the cold build on a user-facing request. Three live paths reach a detector here
rather than on ``celery-redaction``:

1. ``services/chat/redactor._mask_inline`` — the fail-closed fallback, now taken for
   any file whose scan ran without the ``pii`` detector (``v392``
   ``redaction_coverage``), so it is reached *more* often than it used to be.
2. ``services/chat/output_redactor`` — masks what the model writes, gated on
   ``cfg.enabled and cfg.enabled_categories``, which is broader than the egress gate.
3. ``RedactionService.redetect_edited_segment`` — a segment edit re-detects inline.

Measured cost of the first one of those in a fresh process: ~10.1 s to build the
``AnalyzerEngine`` plus ~0.22 s for the first ``analyze()``; the second call is
~0.009 s. So the cost is the **build**, not inference, and the singleton is correct
once warm — there is nothing to fix in the detector except *when* it is paid.

Three constraints shape what this does, and each rules out an obvious alternative:

- **Startup must not block.** The backend has a healthcheck and other services order
  themselves behind it, so ~10 s of synchronous work in the lifespan risks the health
  window. Everything here — including the gate query — runs on a daemon thread, so
  the lifespan pays only ``Thread.start()``.
- **It must not warm unconditionally.** Redaction is opt-out
  (``DEFAULT_REDACTION_ENABLED`` is False), so most deployments never run a detector
  and must not be charged ~500 MB of RAM and a busy core at boot for it. The gate is
  :func:`~app.services.redaction.config.redaction_is_in_use`, a *derived* fact rather
  than a setting — see this package's CLAUDE.md for why it is not an env var.
- **A failure must not break startup.** Presidio is optional. Every failure here is
  logged and swallowed; ``_get_analyzer`` still returns ``None`` and its callers still
  fail closed. This is an optimisation and never a startup dependency.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# Short, obviously-synthetic, and carries the entity types the recognizers actually
# exercise, so the first real call pays neither the build nor the first-analyze cost.
_WARM_SAMPLE = "Contact Jane Doe at jane.doe@example.com or 555-867-5309."

_warmup_thread: threading.Thread | None = None


def warm_pii_analyzer() -> bool:
    """Build the analyzer and run one throwaway ``analyze()``.

    The throwaway call is not decoration: the build is ~10.1 s but the *first*
    ``analyze()`` costs a further ~0.22 s against ~0.009 s warm, and leaving that
    on the first real request would leave a visible fraction of the stall in place.

    Returns:
        True if the analyzer is loaded and usable afterwards.
    """
    from app.services.redaction.config import detection_config_for_all
    from app.services.redaction.detectors import pii_presidio

    started = time.perf_counter()
    if not pii_presidio.preload():
        logger.warning(
            "PII analyzer warm-up could not build the analyzer; the inline maskers "
            "will fail closed as they already do (Presidio may not be installed)"
        )
        return False

    try:
        pii_presidio.detect_pii(_WARM_SAMPLE, None, detection_config_for_all())
    except Exception:  # noqa: BLE001 — a warm-up must never be load-bearing
        logger.warning("PII analyzer built, but the warm-up probe failed", exc_info=True)

    logger.info("PII analyzer warmed in %.2f s", time.perf_counter() - started)
    return True


def _warm_if_in_use() -> None:
    """Gate on the DB, then warm. Runs entirely on the background thread."""
    from app.db.session_utils import session_scope
    from app.services.redaction.config import redaction_is_in_use

    try:
        with session_scope() as db:
            in_use = redaction_is_in_use(db)
    except Exception:  # noqa: BLE001 — never let a warm-up decision surface
        logger.warning("Could not decide whether to warm the PII analyzer", exc_info=True)
        return

    # The session is closed before the model load starts, deliberately: a build is
    # ~10 s of CPU and holding a transaction across it is this repo's most repeated
    # defect (see scripts/audit-session-lifetime.py).
    if not in_use:
        logger.info(
            "PII analyzer warm-up skipped: no user has redaction enabled and no admin "
            "force floor is set. The first user to enable it pays one cold load."
        )
        return

    try:
        warm_pii_analyzer()
    except Exception:  # noqa: BLE001 — an optimisation must not raise into startup
        logger.warning("PII analyzer warm-up failed", exc_info=True)


def start_pii_warmup() -> threading.Thread:
    """Start the background warm-up. Returns the thread, or the one already running.

    A **daemon** thread, not an asyncio task and not ``run_in_threadpool``: the build
    is CPU-bound, so on the event loop it would block every request for its whole
    duration, and it cannot be cancelled once started — a daemon thread says that
    outright instead of leaving a shutdown handler pretending otherwise.
    """
    global _warmup_thread
    running = _warmup_thread
    if running is not None and running.is_alive():
        return running
    thread = threading.Thread(target=_warm_if_in_use, name="pii-analyzer-warmup", daemon=True)
    _warmup_thread = thread
    thread.start()
    return thread
