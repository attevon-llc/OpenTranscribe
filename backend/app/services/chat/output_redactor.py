"""Redact what the MODEL writes, not just what it was given.

Offset-based redaction masks known spans in *stored* text. It cannot touch a
model that reads ``"John Smith, SSN 123-45-6789"`` and writes *"the number he
gave was 123-45-6789"* in its own words: that is a new string, at offsets that
exist in no ``TranscriptSegment`` row, and every cached-span masker in the
codebase renders it clean. Input masking (``redactor.mask_chunks``) is an
**egress** control; this module is the **display** control that has to sit
beside it.

**Why this is a streaming problem.** The answer leaves as SSE ``delta`` frames
as it is generated, so by the time a span is recognisable it has already been
sent. Three options were weighed:

1. Buffer to a sentence boundary, mask the completed sentence, then emit — one
   sentence of added latency. **This module.**
2. Mask at persist + reload only. Cheapest and wrong: the user already read it.
3. Refuse to stream on redacted deployments. Honest, and a large UX regression.

(1) is correct because PII rarely straddles a sentence boundary and the boundary
is where the detector's context ends anyway. The measured cost is a warm
detector call per sentence (~8.5 ms) plus however long the model takes to finish
the sentence.

**The gate is DISPLAY, not egress.** This runs on ``cfg.enabled`` and
``cfg.enabled_categories`` — the same gate ``RedactionService.mask_segment``
self-applies at every other display surface — and deliberately *not* on
``cfg.redact_before_llm``. A user with redaction on and ``redact_before_llm``
off has a masked transcript view; rendering an unmasked SSN into the chat answer
would contradict it. The narrower gate is the plausible-looking wrong choice.

**Fail closed.** A sentence whose detectors could not run is replaced by
``REDACTION_LLM_FAILSAFE_TEXT``, never emitted raw. Note that
``detect_segment_spans`` *swallows* a PII-detector failure and returns the spans
it did get, so "no spans" and "the detector was unavailable" are the same value
— the ``failures`` sink (issue #324) is the only thing that tells them apart,
and this module treats a failure of a detector feeding an **enabled category**
as unmaskable.
"""

from __future__ import annotations

import logging
import re
import time

from app.core import constants as C  # noqa: N812
from app.services.redaction.config import EffectiveRedactionConfig

logger = logging.getLogger(__name__)

# A sentence terminator that has already been followed by whitespace, or any
# newline. Requiring the trailing whitespace is what makes "3.5" and a
# still-arriving "123.4" safe: without it, a terminator at the very end of the
# buffer would be treated as final when the next delta may continue the number.
_BOUNDARY_RE = re.compile(r"(?:[.!?…]+[\"'’”)\]]*\s+|\n+)")

# Trailing token before a candidate boundary that means the period was not a
# sentence end. Splitting "Mr. Smith" into two spans would hand the detector
# "Smith" alone and could lose the NAME match that spans the honorific.
_TITLES = ("mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "rev", "hon", "gov", "sen", "rep")
_RANKS = ("gen", "col", "capt", "lt", "sgt")
_BUSINESS = ("inc", "ltd", "co", "corp", "dept", "est", "fig", "no", "vs", "cf", "al", "approx")
_MONTHS = ("jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec")
_DAYS = ("mon", "tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun")
_ABBREVIATIONS = frozenset(_TITLES + _RANKS + _BUSINESS + _MONTHS + _DAYS)

# Force a flush once the buffer passes this without an acceptable boundary, so a
# model writing one long unpunctuated paragraph still streams.
_MAX_BUFFER_CHARS = 1200
# ...but never cut within this many characters of the live end, so an entity
# straddling the forced cut is not split in half and half-missed.
_HOLD_TAIL_CHARS = 96

_TRAILING_WORD_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")

# Detector → the categories it produces, mirroring ``redaction/config.py``. Used
# to decide whether a detector failure is one this user's policy cares about.
_DETECTOR_CATEGORIES: dict[str, set[str]] = {
    "profanity": {"profanity", "custom"},
    "pii": {"pii"},
    "toxicity": {"toxicity"},
    "llm": {"pii", "toxicity", "profanity", "custom"},
}


def _is_abbreviation(text_before: str) -> bool:
    """Whether the token ending at ``text_before`` means this period isn't final."""
    match = _TRAILING_WORD_RE.search(text_before)
    if match is None:
        return False
    token = match.group(1).rstrip(".").lower()
    if not token:
        return False
    # A lone letter is an initial ("J. Smith"), which is part of a name.
    if len(token) == 1:
        return True
    # "e.g", "i.e", "a.m" arrive here with their inner dots already stripped.
    return token in _ABBREVIATIONS or token.replace(".", "") in {"eg", "ie", "am", "pm", "us"}


def _split_at_last_boundary(buffer: str) -> int:
    """Index just past the last usable sentence boundary in ``buffer`` (0 = none)."""
    cut = 0
    for match in _BOUNDARY_RE.finditer(buffer):
        if match.group(0).startswith("\n") or not _is_abbreviation(buffer[: match.start()]):
            cut = match.end()
    return cut


def _forced_cut(buffer: str) -> int:
    """Where to cut an over-long, boundary-free buffer (0 = keep waiting)."""
    if len(buffer) <= _MAX_BUFFER_CHARS:
        return 0
    limit = len(buffer) - _HOLD_TAIL_CHARS
    cut = buffer.rfind(" ", 0, limit)
    return cut + 1 if cut > 0 else 0


class OutputRedactor:
    """Sentence-buffered masking of one model-generated stream.

    Not thread-safe and not reusable across turns: one instance per stream (the
    answer and the reasoning channel each get their own, since both are
    rendered).

    Usage — ``buffer`` is cheap string work and safe on the event loop, ``mask``
    runs detectors and must go through a threadpool::

        if redactor.active:
            span = redactor.buffer(delta)
            text = await run_in_threadpool(redactor.mask, span) if span else ""
        else:
            text = delta
    """

    def __init__(self, cfg: EffectiveRedactionConfig | None) -> None:
        """Args:
        cfg: The requesting user's effective config, or ``None`` when it could
            not be resolved — which activates masking with every category on,
            because "we cannot tell whether this needs masking" must not mean
            "send it raw".
        """
        if cfg is None:
            logger.warning("Chat output redaction: no effective config; masking everything")
            cfg = EffectiveRedactionConfig(
                enabled=True,
                enabled_categories={"pii", "toxicity", "profanity", "custom"},
                pii_entities=set(C.REDACTION_PII_ENTITIES),
            )
        self._cfg = cfg
        # Nothing to mask → a pure pass-through that costs the stream nothing.
        self.active = bool(cfg.enabled and cfg.enabled_categories)
        self._buffer = ""
        self._drained = False
        self.masked_spans = 0
        self.withheld_spans = 0
        self.mask_ms = 0

    def buffer(self, text: str) -> str:
        """Accept a delta; return the text now safe to hand to :meth:`mask`.

        Returns ``""`` while the current sentence is still arriving.
        """
        if not self.active:
            return text
        if not text:
            return ""
        self._buffer += text
        cut = _split_at_last_boundary(self._buffer) or _forced_cut(self._buffer)
        if not cut:
            return ""
        ready, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return ready

    def drain(self) -> str:
        """Return the unemitted tail exactly once (``""`` on any later call).

        Idempotent because the tail is flushed from the normal post-loop path
        *and* from the shielded teardown that persists a cancelled turn; a
        second flush would duplicate it into the stored answer.
        """
        if self._drained:
            return ""
        self._drained = True
        tail, self._buffer = self._buffer, ""
        return tail

    def mask(self, span: str) -> str:
        """Mask one completed span. CPU-bound — call via a threadpool.

        Returns ``REDACTION_LLM_FAILSAFE_TEXT`` when the span could not be
        established as safe. A visible placeholder beats a silent hole: the
        alternative leaves the reader with a sentence that simply vanished.
        """
        if not self.active or not span.strip():
            return span
        started = time.monotonic()
        try:
            masked = self._detect_and_mask(span)
        except Exception:  # noqa: BLE001 — fail closed, never emit the raw span
            self.withheld_spans += 1
            logger.exception("Chat output masking failed; withholding a generated span")
            return C.REDACTION_LLM_FAILSAFE_TEXT
        finally:
            self.mask_ms += int((time.monotonic() - started) * 1000)
        self.masked_spans += 1
        return masked

    def _detect_and_mask(self, span: str) -> str:
        """Detect over generated text and apply the user's masking policy.

        Toxicity classification is skipped for the same reason the chunk path
        skips it: it is the expensive detector and loading it mid-answer would
        blow the latency budget. PII and profanity are what this guards.
        """
        from app.services.redaction.config import detection_config_for_all
        from app.services.redaction.service import RedactionService

        failures: list[str] = []
        spans, _toxicity = RedactionService.detect_segment_spans(
            span, None, detection_config_for_all(), run_toxicity=False, failures=failures
        )
        blocking = self._blocking_failures(failures)
        if blocking:
            raise RuntimeError(f"detectors unavailable for enabled categories: {sorted(blocking)}")
        masked, _applied = RedactionService.mask_segment(span, spans, None, self._cfg, set())
        return masked

    def _blocking_failures(self, failures: list[str]) -> set[str]:
        """Which detector failures actually matter for this user's categories.

        A PII detector that could not load is irrelevant to a user who does not
        have ``pii`` enabled — and PII is *not* in the default categories, so
        treating every failure as blocking would withhold answers wholesale on
        deployments that never asked for PII masking in the first place.

        ``failures`` names DETECTORS and ``enabled_categories`` names
        CATEGORIES; they coincide for ``pii`` and diverge for ``profanity``
        (which also produces ``custom``), so the mapping is written out rather
        than assumed.
        """
        enabled = self._cfg.enabled_categories
        return {name for name in failures if _DETECTOR_CATEGORIES.get(name, {name}) & enabled}
