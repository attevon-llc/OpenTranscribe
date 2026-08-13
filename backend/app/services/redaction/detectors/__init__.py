"""Redaction detectors (wordlist, Presidio+GLiNER PII, toxicity, LLM)."""

from __future__ import annotations


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
