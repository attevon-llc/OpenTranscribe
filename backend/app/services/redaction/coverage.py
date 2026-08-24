"""What a finished redaction scan actually EXAMINED, and who is allowed to trust it.

``redaction_status = done`` answers "did the scan finish". It stopped answering "was
this text examined" the moment an unavailable detector became a *skip* rather than a
failure (``e6048808``) — and that had to happen, because ``failed`` is turned into a
permanent, non-retryable refusal by :func:`~app.services.redaction.llm_guard
.resolve_llm_masking`, so flipping every file on a deployment that merely lacks Presidio
would break summarization, speaker identification and topic extraction for good.

So a scan can reach ``done`` having never run the PII detector, and a reader that trusts
``done`` alone will mask nothing, report success, and send the transcript on.
``media_file.redaction_coverage`` (v392) is the durable record of the difference:
the detectors whose results the cached spans reflect. Everything in this module is the
read half of it.

Nothing here re-probes for a detector. It cannot: the API process, the CPU worker and
``celery-redaction`` preload different models, so "can I load Presidio *here*" answers a
different question from "did the detector that produced these spans have it". Only the
scan can know, and only if it writes it down.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core import constants as C  # noqa: N812
from app.services.redaction.config import blocking_detector_failures
from app.services.redaction.config import detector_language_support

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.redaction.config import EffectiveRedactionConfig

logger = logging.getLogger(__name__)


def uncovered_detectors(media_file, cfg: EffectiveRedactionConfig) -> set[str]:
    """Detectors this policy relies on whose findings the cached spans do NOT contain.

    Composed from the two functions that already own the two halves of the question,
    rather than a third copy of either:

    * :func:`~app.services.redaction.config.detector_language_support` says which
      detectors could ever have run for this transcript's language. A detector that does
      not support the language was never going to run — that is a **declared capability**
      of the product (profanity and PII are English-only), identical on every future scan
      and unfixable by any operator action. Treating it as a gap would withhold every
      non-English transcript from every LLM feature, permanently, which is a different
      decision from the one this module implements. An *unavailable* detector is the
      opposite on every count: it is a deployment fault, the same file would have been
      examined on a properly provisioned box, and installing the dependency plus a
      re-scan fixes it.

      ⚠️ **It only excuses a detector for a language it could identify** (issue #545). Its
      normalizer used to echo anything it did not understand, so ``"eng"`` / ``"English"``
      / ``"en "`` were compared against ``REDACTION_PII_LANGUAGES`` (``{"en"}``), found
      absent, and subtracted here as a "language skip" — a permanent product limit that was
      nothing of the sort. It now returns every detector for an undeterminable language, so
      ``relied_on`` below stays full and whatever the scan did not run is reported as a real
      gap rather than excused. That is deliberately the *widest* of the three inputs to this
      function: over-reporting a gap costs an inline re-detection, under-reporting it sends
      an unexamined transcript to a provider.
    * :func:`~app.services.redaction.config.blocking_detector_failures` says which of
      the remaining gaps this policy actually cares about. That narrowness is the whole
      reason the control is safe to turn on: ``pii`` is not a default category, so a
      CPU-only deployment with no Presidio that never asked for PII masking loses
      nothing.

    ``llm`` is required only when the owner selected it, because it is an opt-in
    enhancement (``DEFAULT_REDACTION_DETECTORS`` excludes it) whose category mapping
    covers everything — required unconditionally, an absent LLM detector would block
    every redaction-enabled user on every deployment.

    Args:
        media_file: The scanned ``MediaFile``. Read for ``redaction_coverage`` and
            ``language`` only.
        cfg: The effective policy of the subject whose masking is at stake. For the
            LLM egress paths that is the **file owner** — the content is theirs, and
            resolving the caller's config would let an admin whose own redaction is off
            release someone else's unexamined transcript.

    Returns:
        The detector names that leave a gap this policy cares about. Empty means the
        cached spans cover everything ``cfg`` masks.
    """
    covered = getattr(media_file, "redaction_coverage", None)
    if covered is None:
        # ⚠️ RESIDUAL, and deliberate. Rows scanned before v392 carry no coverage, and
        # nothing can recover retroactively whether that deployment had Presidio.
        # Reading NULL as "nothing was covered" would refuse every pre-existing file on
        # the day of the upgrade — including on deployments that were never at risk —
        # so it is read as "no worse than yesterday". `redaction.reindex_all` re-scans
        # and re-scanning writes coverage; that is the remedy, and it already exists.
        return set()

    supported, _skipped = detector_language_support(getattr(media_file, "language", None))
    # A detector is RELIED ON when the owner selected it, or when it is one of the
    # always-run detectors and this policy masks a category it feeds. Both arms are
    # needed: `cfg.detectors` and `cfg.enabled_categories` are separate user settings and
    # a policy that masks `pii` while its detector list omits `pii` must not read as
    # covered.
    relied_on = (set(cfg.detectors) | set(C.DEFAULT_REDACTION_DETECTORS)) & supported
    return blocking_detector_failures(relied_on - set(covered), cfg.enabled_categories)


def describe_gap(media_file, gap: set[str]) -> str:
    """One operator-facing sentence naming the file, the gap and the remedy."""
    return (
        f"file {getattr(media_file, 'id', '?')} was scanned without detectors "
        f"{sorted(gap)}, so its cached spans do not cover categories this policy masks. "
        "Install the missing detector dependency and re-run redaction detection "
        "(admin → redaction re-index) to repair it."
    )
