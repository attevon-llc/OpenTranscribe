"""Read-time masking of AI-generated summaries (issue #465).

``media_file.summary_data`` was rendered with no redaction anywhere: not in
``api/endpoints/summarization.py``, not in ``schemas/summary.py``, not in any
formatting service. So a user whose policy masks PII saw a **masked transcript**
beside an **unmasked AI summary of that same transcript** — and because the
summary is abstractive, it restates the same PII in the model's own words. A
phone number the transcript view redacts could appear verbatim in the BLUF.

The admin force floor was bypassed identically, since the floor only exists
inside ``resolve_effective_config``.

Why the cached spans do not help
--------------------------------
``redactions`` is a column on ``TranscriptSegment`` holding character offsets
into *segment* text. A summary is different, LLM-authored text — those offsets
address nothing in it. Masking a summary requires **detecting over it**, exactly
as ``search/snippet_redaction`` and ``chat/output_redactor`` do. That is what
made this look expensive; Presidio being warmed in the API process is what made
it cheap (~12.5 ms for a short text, against a 9.9 s cold load).

⚠️ Detect per leaf string, never batched
----------------------------------------
``en_core_web_sm`` reports each distinct ``PERSON`` **once per document**. Joining
a summary's sections into one ``analyze()`` call therefore returns one span for a
name that appears in three sections, leaking it from every section after the
first — **while the page shows a ``[NAME]`` label and looks masked**. Measured on
the snippet path when it was attempted there: the batched version leaked the name
in **31 of 32** snippets. It is 2-3x faster and wrong. :func:`_mask_leaf` is
therefore called once per string, and the unit is deliberately the leaf rather
than the section or the document.

The same property means a name repeated *within* one leaf is masked only at its
first mention. That residual is inherited from spaCy and is app-wide (cached
segment detection has it too), not specific to summaries.

Structure-agnostic on purpose
-----------------------------
``schemas/summary.py`` declares ``SummaryData`` with ``extra="allow"`` and
``summary_data: dict[str, Any]`` — a custom prompt may produce **any** JSON. So
this walks the tree and masks every string leaf it finds rather than naming
``bluf`` / ``brief_summary`` / ``key_points``, which would silently miss whatever
a custom prompt called its fields.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from app.services.redaction.config import EffectiveRedactionConfig

logger = logging.getLogger(__name__)

#: The categories a summary can actually be masked for. ``toxicity`` is absent for
#: the same reason as in ``search/snippet_redaction``: its detector emits a
#: per-segment SCORE and never a span, and the ``toxicity`` category's maskable
#: spans come from the ``llm`` detector — a provider round-trip with no business
#: on a read request.
MASKABLE_CATEGORIES = frozenset({"pii", "profanity", "custom"})

#: Top-level keys whose values are machine-generated processing metadata rather
#: than model-authored prose about the recording: provider, model name, token
#: counts, timings. Masking them is not a privacy gain and IS a correctness loss —
#: a ``DATE_TIME`` entity would rewrite ``created_at`` into ``[DATE]`` and the
#: admin UI would render a summary that claims it was generated at ``[DATE]``.
_UNMASKED_TOP_LEVEL_KEYS = frozenset({"metadata"})


class SummaryMaskingUnavailableError(RuntimeError):
    """A detector feeding one of the user's enabled categories could not run.

    Raised rather than returning partially-masked text. The endpoint turns this
    into a 503, matching what ``files/crud.py`` already does when the redaction
    config itself cannot be resolved: a summary that is *supposed* to be masked
    is withheld rather than served in the clear.
    """


def mask_summary(summary_data: dict[str, Any], cfg: EffectiveRedactionConfig) -> dict[str, Any]:
    """Mask every string leaf of a summary under one effective redaction policy.

    Args:
        summary_data: The summary as stored, free-form JSON.
        cfg: The requesting user's effective config (admin floor already folded in
            by ``resolve_effective_config``).

    Returns:
        A new dict with the same shape. When the policy does not apply, the input
        is returned unchanged so a redaction-disabled deployment is byte-identical.

    Raises:
        SummaryMaskingUnavailableError: A detector feeding an enabled category
            could not run, so some of this text was never examined.
    """
    policy = resolve_summary_leaf_policy(cfg)
    if policy is None:
        return summary_data
    run_profanity, run_pii, run_custom, label_cfg = policy

    failures: list[str] = []
    masked = {
        key: value
        if key in _UNMASKED_TOP_LEVEL_KEYS
        else _mask_node(
            value,
            label_cfg,
            failures,
            run_profanity=run_profanity,
            run_pii=run_pii,
            run_custom=run_custom,
        )
        for key, value in summary_data.items()
    }

    # `detect_segment_spans` SWALLOWS a detector exception and returns the spans it
    # did collect, so "found nothing" and "could not look" are the same return
    # value (issue #324). `failures` is the only thing that tells them apart, and
    # `blocking_detector_failures` decides whether a given failure is one THIS
    # policy cares about — a broken Presidio must not withhold a profanity-only
    # user's summary.
    from app.services.redaction.config import blocking_detector_failures

    blocking = blocking_detector_failures(failures, cfg.enabled_categories)
    if blocking:
        raise SummaryMaskingUnavailableError(
            f"detectors unavailable for enabled categories: {sorted(blocking)}"
        )

    return masked


def resolve_summary_leaf_policy(
    cfg: EffectiveRedactionConfig,
) -> tuple[bool, bool, bool, EffectiveRedactionConfig] | None:
    """Which detectors a summary-prose masker must run, or ``None`` for "none".

    Shared by :func:`mask_summary` (whole tree) and :func:`mask_summary_leaf`
    (one string), so the two cannot drift. They did: the recurrence detector
    grew its own leaf masker that ran ``detection_config_for_all()`` directly,
    which skipped the custom-wordlist spans and the label-style forcing below,
    and constructed Presidio even for a profanity-only user.

    Gate the detectors on the CATEGORIES, not just on ``enabled``. PII masking is
    opt-in twice over (redaction is opt-out, and ``pii`` is not in
    ``DEFAULT_REDACTION_CATEGORIES``), and constructing Presidio costs 0.8-2.1 s
    against 3-14 ms for the wordlist. A profanity-only user must never construct
    the analyzer.

    Returns:
        ``(run_profanity, run_pii, run_custom, label_cfg)``, or ``None`` when the
        policy masks nothing and the caller should return its input unchanged so
        a redaction-disabled deployment stays byte-identical.
    """
    cats = set(cfg.enabled_categories) & MASKABLE_CATEGORIES
    if not cfg.enabled or not cats:
        return None

    run_profanity = "profanity" in cats
    run_pii = "pii" in cats
    run_custom = "custom" in cats and bool(cfg.custom_words)
    if not (run_profanity or run_pii or run_custom):
        return None

    # Summaries are always label-styled, for the same reason snippets are: `blur`
    # emits markup the client does not sanitize for this surface, and
    # `first_letter` / `asterisks` are indistinguishable from the model's own
    # emphasis. Everything else about the policy — pii_entities, allowlist,
    # custom words, categories — is applied by `mask_segment`, the one masker.
    return run_profanity, run_pii, run_custom, dataclasses.replace(cfg, style="label")


def mask_summary_leaf(text: str, cfg: EffectiveRedactionConfig) -> str:
    """Mask ONE summary-derived string (an action item, a keyphrase, ...).

    The single-string entry point onto the same machinery :func:`mask_summary`
    uses for a whole tree. Cross-meeting recurrence needs this shape: it groups
    individual item strings, which have no ``segment_ids`` provenance to rebuild
    from, so each is detected live exactly like a summary leaf.

    Raises:
        SummaryMaskingUnavailableError: A detector feeding an enabled category
            could not run, so this text was never fully examined. Callers drop
            the whole file's items rather than pass half-masked text through.
    """
    policy = resolve_summary_leaf_policy(cfg)
    if policy is None or not text.strip():
        return text
    run_profanity, run_pii, run_custom, label_cfg = policy

    failures: list[str] = []
    masked = _mask_leaf(
        text,
        label_cfg,
        failures,
        run_profanity=run_profanity,
        run_pii=run_pii,
        run_custom=run_custom,
    )

    from app.services.redaction.config import blocking_detector_failures

    blocking = blocking_detector_failures(failures, cfg.enabled_categories)
    if blocking:
        raise SummaryMaskingUnavailableError(
            f"detectors unavailable for enabled categories: {sorted(blocking)}"
        )
    return masked


def _mask_node(
    node: Any,
    cfg: EffectiveRedactionConfig,
    failures: list[str],
    *,
    run_profanity: bool,
    run_pii: bool,
    run_custom: bool,
) -> Any:
    """Recursively mask every string leaf, preserving the container shape."""
    if isinstance(node, str):
        return _mask_leaf(
            node,
            cfg,
            failures,
            run_profanity=run_profanity,
            run_pii=run_pii,
            run_custom=run_custom,
        )
    if isinstance(node, dict):
        return {
            key: _mask_node(
                value,
                cfg,
                failures,
                run_profanity=run_profanity,
                run_pii=run_pii,
                run_custom=run_custom,
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [
            _mask_node(
                item,
                cfg,
                failures,
                run_profanity=run_profanity,
                run_pii=run_pii,
                run_custom=run_custom,
            )
            for item in node
        ]
    # int / float / bool / None pass through: nothing to detect over.
    return node


def _mask_leaf(
    text: str,
    cfg: EffectiveRedactionConfig,
    failures: list[str],
    *,
    run_profanity: bool,
    run_pii: bool,
    run_custom: bool,
) -> str:
    """Detect over ONE string and return it masked.

    ⚠️ One detector pass per leaf. Do not batch the summary's leaves into shared
    ``analyze()`` calls — see this module's docstring for the measurement. A
    summary is by construction a set of passages about the same people, which is
    precisely the input spaCy's once-per-document NER destroys.
    """
    if not text.strip():
        return text

    from app.services.redaction.config import detection_config_for_all
    from app.services.redaction.detectors import wordlist
    from app.services.redaction.service import RedactionService

    spans, _toxicity = RedactionService.detect_segment_spans(
        text,
        None,
        detection_config_for_all(),
        run_profanity=run_profanity,
        run_pii=run_pii,
        run_toxicity=False,
        failures=failures,
    )
    if run_custom:
        spans = list(spans) + [
            span.model_dump()
            for span in wordlist.find_custom_spans(text, cfg.custom_words, None, cfg.allowlist)
        ]

    masked, _applied = RedactionService.mask_segment(text, spans, None, cfg, set())
    return masked
