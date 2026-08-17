"""Read-time masking of search-result snippets (issue #86).

A snippet is a fragment of transcript text pulled straight out of the
``transcript_chunks`` index, which stores transcript text **UNREDACTED** by
design. So a snippet can and does contain PII, and search spans collection and
group shares — a share recipient sees previews of recordings whose full
transcript view would have been masked for them.

Three things make this path different from every other masker in the app, and
each of them is why this module exists rather than a few more lines in
``hybrid_search_service``:

1. **There are no cached spans to apply.** Cached spans address
   ``transcript_segment.text``; a snippet is a *highlighted, HTML-escaped,
   re-fragmented* rendering of it, so no stored offset addresses it. Detection
   therefore runs live over the snippet, exactly as ``chat/redactor._mask_inline``
   does — which also means ``media_file.redaction_status`` is irrelevant here.
   Nothing on this path reads a cached span, so nothing can be stale.
2. **The text contains ``<mark>`` markup.** OpenSearch highlighting wraps matched
   terms, and it does **not** wrap whole words — real output includes
   ``<mark>budget ? Not the </mark>original``. A detector run over that text can
   return a span that starts or ends inside a tag; masking it would emit an
   orphan ``</mark>`` and the frontend sanitizer would then drop the fragment.
   So the tags are **split out before detection and restored after**, and a span
   spanning a tag masks each side of it separately.
3. **Detection is per snippet, and batching it is FORBIDDEN.** Batching a whole
   page into shared ``analyze()`` calls is 2.2-3.0x faster and loses PII, because
   spaCy reports each distinct ``PERSON`` once per *document* — see
   :func:`_detect`, which carries the measurement. The cost of doing it correctly
   is real and is stated in this package's ``CLAUDE.md``.

**Toxicity cannot be masked here and is deliberately absent from
:data:`MASKABLE_CATEGORIES`.** The toxicity detector emits a per-segment SCORE,
never a span (``redaction/config._DETECTOR_CATEGORIES`` maps it to the empty
set); its consumer is ``formatting_service``, which turns it into a UI flag. The
``toxicity`` *category*'s maskable spans come from the ``llm`` detector, which is
a provider round-trip and has no business on a search request. A user whose only
enabled category is ``toxicity`` therefore gets unmasked snippets — correctly,
because there is nothing on this path that could produce a toxicity span to mask.
"""

from __future__ import annotations

import dataclasses
import html
import logging
import re

from app.services.redaction.config import EffectiveRedactionConfig

logger = logging.getLogger(__name__)

#: The categories a search snippet can actually be masked for. ``toxicity`` is
#: absent on purpose — see the module docstring.
MASKABLE_CATEGORIES = frozenset({"pii", "profanity", "custom"})

# Capturing group: `re.split` then yields text runs at even indices and the tags
# themselves at odd ones, so the split is lossless and the snippet reassembles
# byte-for-byte when nothing is masked.
_MARK_TAG = re.compile(r"(</?mark\b[^>]*>)")


class SnippetMaskingUnavailableError(RuntimeError):
    """A detector feeding one of the user's enabled categories could not run.

    Raised instead of returning partially-masked text: the caller withholds the
    page's snippets, the same disposition it already takes when the redaction
    config itself cannot be resolved.
    """


class _Snippet:
    """One snippet, split into maskable text runs and the ``<mark>`` tags between them.

    ``plain`` is what the detectors see: the text runs concatenated, with HTML
    entities decoded so ``John&#x27;s`` reaches spaCy as ``John's``. Masked runs
    are re-escaped with :func:`html.escape`, which emits exactly the five entities
    ``_sanitize_html`` produces; a run with nothing masked is emitted as its
    ORIGINAL bytes, so the escape round-trip is never in the path of an
    unmodified snippet.
    """

    __slots__ = ("original", "plain", "raw_runs", "runs", "spans", "tags")

    def __init__(self, snippet: str) -> None:
        parts = _MARK_TAG.split(snippet)
        self.original = snippet
        self.raw_runs = parts[0::2]
        self.tags = parts[1::2]
        self.runs = [html.unescape(run) for run in self.raw_runs]
        self.plain = "".join(self.runs)
        self.spans: list[dict] = []

    def render(self, cfg: EffectiveRedactionConfig) -> str:
        """Reassemble the snippet with each run masked against the spans inside it."""
        from app.services.redaction.service import RedactionService

        out: list[str] = []
        cursor = 0
        for index, run in enumerate(self.runs):
            end = cursor + len(run)
            # A span crossing a tag is clipped to this run and masked again in
            # the next one. Two adjacent labels look odd; a mask that swallowed
            # the tag between them would emit unbalanced markup.
            local = [
                {
                    **span,
                    "char_start": max(span["char_start"], cursor) - cursor,
                    "char_end": min(span["char_end"], end) - cursor,
                }
                for span in self.spans
                if span["char_start"] < end and span["char_end"] > cursor
            ]
            masked, applied = RedactionService.mask_segment(run, local, None, cfg, set())
            out.append(html.escape(masked) if applied else self.raw_runs[index])
            cursor = end
            if index < len(self.tags):
                out.append(self.tags[index])
        return "".join(out)


def mask_snippets(snippets: list[str], cfg: EffectiveRedactionConfig) -> list[str]:
    """Mask every snippet on a search page under one effective redaction policy.

    Args:
        snippets: Snippet text as the search response would render it, ``<mark>``
            tags and HTML entities included.
        cfg: The requesting user's effective config (admin floor already folded
            in by ``resolve_effective_config``).

    Returns:
        The masked snippets, positionally 1:1 with ``snippets``. A snippet with
        nothing to mask comes back byte-identical.

    Raises:
        SnippetMaskingUnavailableError: A detector feeding an enabled category could
            not run, so some of these snippets were never examined.
    """
    cats = set(cfg.enabled_categories) & MASKABLE_CATEGORIES
    if not cfg.enabled or not cats:
        return list(snippets)

    docs = [_Snippet(snippet) for snippet in snippets]
    # Gate the detectors on the categories, not just on `enabled`: PII masking is
    # opt-in twice over (redaction is opt-out, and `pii` is not in
    # DEFAULT_REDACTION_CATEGORIES), and Presidio is 0.8-2.1 s on a full page
    # against 3-14 ms for the wordlist. A user who masks only profanity must not
    # pay one millisecond of it.
    run_profanity = "profanity" in cats
    run_pii = "pii" in cats
    run_custom = "custom" in cats and bool(cfg.custom_words)
    if run_profanity or run_pii or run_custom:
        _detect(docs, cfg, run_profanity=run_profanity, run_pii=run_pii, run_custom=run_custom)

    # Previews are always label-styled. `blur` emits a `<span class="redacted">`
    # that the snippet renderer's sanitizer does not allow, and `first_letter` /
    # `asterisks` would be indistinguishable from a highlighted match. Everything
    # else about the policy — pii_entities, allowlist, custom words, categories —
    # is applied by `mask_segment`, which stays the one masker.
    label_cfg = dataclasses.replace(cfg, style="label")
    return [doc.render(label_cfg) for doc in docs]


def _detect(
    docs: list[_Snippet],
    cfg: EffectiveRedactionConfig,
    *,
    run_profanity: bool,
    run_pii: bool,
    run_custom: bool = False,
) -> None:
    """Run the detectors over each snippet's plain text, ONE SNIPPET AT A TIME.

    ⚠️ **Do not batch these into shared ``analyze()`` calls.** It is the obvious
    optimisation — a page is 94-200 snippets and one call each costs 2.2-3.0x what
    a packed call does — and it silently loses PII. ``en_core_web_sm``'s NER
    reports each distinct ``PERSON`` **once per document**: joined with any
    separator, two snippets naming Talia Yarrow yield one span, three yield one,
    while three snippets naming three *different* people yield three. Measured
    through the live search API on a real page, batching left the name in clear in
    **31 of the 32 snippets containing it** — with ``[NAME]`` labels elsewhere on
    the same page, so the result looks masked. A search page is by construction a
    set of fragments about the same subject, which is exactly the input that
    property destroys.

    The same property is why the detector unit is the SNIPPET and not something
    smaller or larger: it also means a name repeated *within* one snippet is
    masked only at its first mention. That residual is inherited from spaCy and is
    app-wide (it applies to cached segment detection too), not specific to search.
    """
    from app.services.redaction.config import blocking_detector_failures
    from app.services.redaction.config import detection_config_for_all
    from app.services.redaction.detectors import wordlist
    from app.services.redaction.service import RedactionService

    det_cfg = detection_config_for_all()
    failures: list[str] = []
    for doc in docs:
        if not doc.plain:
            continue
        found, _toxicity = RedactionService.detect_segment_spans(
            doc.plain,
            None,
            det_cfg,
            run_profanity=run_profanity,
            run_pii=run_pii,
            run_toxicity=False,
            failures=failures,
        )
        doc.spans.extend(found)
        # Custom words must be matched over the WHOLE snippet, same as profanity/pii
        # above — matching per-run instead (as mask_segment's own inline rescan does)
        # misses a custom word split across a <mark> boundary, since it never appears
        # intact within either run. render() clips these spans per run just like the
        # others; mask_segment's inline rescan then only re-finds words that stayed
        # inside one run, a harmless duplicate that _merge_spans de-overlaps.
        if run_custom:
            doc.spans.extend(
                s.model_dump()
                for s in wordlist.find_custom_spans(
                    doc.plain, cfg.custom_words, None, cfg.allowlist
                )
            )

    # `detect_segment_spans` SWALLOWS a detector exception and returns the spans
    # it did collect, so "found nothing" and "could not look" are the same return
    # value (issue #324). `failures` is the only thing that tells them apart, and
    # `blocking_detector_failures` is the shared rule for whether a given failure
    # is one THIS policy cares about — a broken Presidio must not cost a
    # profanity-only user their snippets.
    blocking = blocking_detector_failures(failures, cfg.enabled_categories)
    if blocking:
        raise SnippetMaskingUnavailableError(
            f"detectors unavailable for enabled categories: {sorted(blocking)}"
        )
