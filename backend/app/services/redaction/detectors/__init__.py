"""Redaction detectors (wordlist, Presidio+GLiNER PII, toxicity, LLM)."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from collections.abc import Iterator

# Set (to a list that collects refusals) while a request thread runs a detector. A loader that
# would have to BUILD a model there returns "unavailable" instead, because a build is ~10 s of CPU
# plus HuggingFace Hub network round trips (measured 3.7 s on a segment edit). The dedicated
# redaction worker and the startup warm-up never set it, so they still load.
_refused_loads: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "redaction_refused_loads", default=None
)


@contextlib.contextmanager
def no_model_load() -> Iterator[list[str]]:
    """Forbid building a detector model inside this block; yields the refusals recorded.

    Already-resident models are used as normal. A detector that is not resident reports
    :class:`DetectorUnavailableError` (the existing, fail-closed "could not look" outcome) and
    its name lands in the yielded list so the caller can hand the work to the redaction worker.
    """
    refused: list[str] = []
    token = _refused_loads.set(refused)
    try:
        yield refused
    finally:
        _refused_loads.reset(token)
        if refused:
            _request_warmup()


_WARMUP_RETRY_SECONDS = 60.0
_last_warmup_request = float("-inf")


def _request_warmup() -> None:
    """Ask the API process to warm detectors in the background, at most once a minute.

    A refusal means this request failed closed because the models are not resident, which
    happens when redaction was first enabled AFTER startup (the startup warm-up is gated on
    redaction being in use). Without this the API process would refuse until restarted.
    ``start_pii_warmup`` re-checks the gate and builds on a daemon thread, off the request.
    """
    global _last_warmup_request
    now = time.monotonic()
    if now - _last_warmup_request < _WARMUP_RETRY_SECONDS:
        return
    _last_warmup_request = now
    try:
        from app.services.redaction.warmup import start_pii_warmup

        start_pii_warmup()
    except Exception:  # an optimisation, never load-bearing for the request that refused
        logging.getLogger(__name__).warning("Could not start detector warm-up", exc_info=True)


def refuse_model_load(name: str) -> bool:
    """True (and recorded) when the current context forbids building ``name``."""
    refused = _refused_loads.get()
    if refused is None:
        return False
    refused.append(name)
    return True


class DetectorUnavailableError(RuntimeError):
    """A detector could not run **at all**, so the text was never examined.

    Three outcomes have to stay distinguishable and only two of them used to be:

    ===========================  ========================================
    Outcome                      How it is reported
    ===========================  ========================================
    ran, found nothing           returns ``[]``
    ran, then raised             raises (recorded in ``failures``)
    could not run                raises **this** (``failures`` + ``unavailable``)
    ===========================  ========================================

    The third used to be the first. ``pii_presidio._get_analyzer`` catches an
    absent or unbuildable Presidio, logs "PII detection disabled" and returns
    ``None``; ``detect_pii`` then returned ``[]`` — indistinguishable from a
    clean segment. Issue #324's ``failures`` sink exists precisely to separate
    "found nothing" from "could not look", but it only ever saw exceptions that
    escaped a detector, and an absent Presidio escaped nothing. Measured on the
    real detector layer with only the analyzer removed, a user who had
    **enabled** ``pii`` still got chunks sent to their LLM provider unmasked
    while ``mask_chunks`` reported ``was_masked=True``.

    Unavailability and failure are then handled differently on purpose:

    * A **masker** treats both as "could not look" and withholds the text, via
      :func:`~app.services.redaction.config.blocking_detector_failures` — and
      only when the dead detector feeds a category that user actually masks, so
      a CPU-only deployment that never asked for PII loses nothing.
    * :meth:`RedactionService.detect_and_store` treats a failure as ``FAILED``
      (the scan is worth re-running) and unavailability as a **skip**. Re-running
      will not install the missing dependency, and ``FAILED`` is not inert:
      ``llm_guard.resolve_llm_masking`` raises a *non-retryable*
      ``RedactionNotReadyError`` on it, so marking every file ``FAILED`` on a
      deployment that simply has no Presidio would permanently break
      summarization, speaker identification and topic extraction for every user
      with ``redact_before_llm`` on.
    """
