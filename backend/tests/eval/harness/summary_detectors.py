"""Regex prefilters for the summary-merge quality gate: truncation claims, "nothing decided",
and failure disclosure.

**These are prefilters, never verdicts.** They exist for cheap triage and so the unit tests have
something concrete to pin; the label that counts comes from the judge (``summary_judge``) or a
human. That is the same stance as ``answer_judge._REFUSAL_RE``, whose comment records two false
alarms from a too-narrow pattern. A prefilter may over-fire (the judge will clear it) but a miss is
measured separately: the judge also grades a fixed sample of non-hits.

The four behaviours the multi-slice merge rules protect (plan section 1):

* (b) the merged result must not call the recording truncated unless a slice said so:
  :func:`truncation_claims`;
* (c) a trailing crosstalk slice must not empty the result: :func:`nothing_decided_claims`;
* (d) a failed slice must be disclosed, never reported as "nothing happened there":
  :func:`failure_disclosure`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

_SUBJECT = r"(?:recording|transcript|audio|meeting|conversation|discussion|excerpt|text|content)"
_TRUNCATED_WORD = (
    r"(?:truncated|cut\s+(?:off|short)|incomplete|partial|fragment(?:ary)?|excerpt|snippet|"
    r"unfinished)"
)

#: Each pattern needs the recording (or "this"/"it") as the grammatical subject. A bare
#: "incomplete prototype" or "cut the budget" in a summary of the meeting's CONTENT is not a claim
#: about the recording, and a subject-free pattern made exactly that mistake.
_TRUNCATION_PATTERNS = (
    re.compile(
        rf"\b{_SUBJECT}\b(?:\s+\w+){{0,3}}\s+(?:is|was|are|were|appears?|seems?|looks?)\s+"
        rf"(?:to\s+(?:be|have\s+been)\s+)?(?:only\s+|just\s+|merely\s+|somewhat\s+|clearly\s+)?"
        rf"(?:an?\s+)?{_TRUNCATED_WORD}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:this|it|that)\s+(?:is|was|appears\s+to\s+be|seems\s+to\s+be)\s+"
        r"(?:only\s+|just\s+|merely\s+)?(?:an?\s+)?"
        r"(?:fragment(?:ary)?|excerpt|snippet|truncated|cut\s+off|"
        r"partial\s+(?:recording|transcript|record|meeting|discussion))\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_SUBJECT}\b(?:\s+\w+){{0,2}}\s+(?:starts?|begins?|ends?|stops?|cuts?|breaks?)\s+"
        r"(?:off\s+)?(?:abruptly|mid-?\s?sentence|mid-?\s?conversation|mid-?\s?thought|"
        r"in\s+the\s+middle\s+of\s+(?:an?|the)\s+\w+)",
        re.IGNORECASE,
    ),
)

_NOTHING_DECIDED_PATTERNS = (
    re.compile(
        r"\bno\s+(?:\w+\s+)?(?:decisions?|conclusions?|action\s+items?|outcomes?)\s+"
        r"(?:were|was|have\s+been|has\s+been|could\s+be|can\s+be)\s+"
        r"(?:made|reached|taken|recorded|identified|determined|documented)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:did\s+not|didn't|have\s+not|has\s+not)\s+(?:make|reach|take|record|identify)\s+"
        r"any\s+(?:\w+\s+)?(?:decisions?|conclusions?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bnothing\s+(?:was|has\s+been)\s+(?:decided|concluded|resolved|agreed)", re.IGNORECASE
    ),
    # "no decisions." / "no decisions or action items" -- but NOT "no decision on colour yet":
    # a decision-free TOPIC is ordinary content; only a decision-free MEETING is the bug.
    re.compile(
        r"\bno\s+(?:key\s+|major\s+|specific\s+|clear\s+)?(?:decisions?|action\s+items?)\s*"
        r"(?:[.;]|$|or\s+(?:action\s+items?|decisions?|conclusions?))",
        re.IGNORECASE,
    ),
)

_PART = r"(?:section|part|portion|segment|slice|span|period)"

_DISCLOSURE_PATTERNS = (
    re.compile(
        rf"\b{_PART}\b[^.]{{0,80}}\b(?:could\s?n[o']t|cannot|can't|was\s+not|were\s+not|wasn't|"
        r"failed|unable|not\s+(?:be\s+)?(?:processed|analy[sz]ed|summari[sz]ed|available)|"
        r"missing|lost)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:failed\s+to\s+(?:process|summari[sz]e)|processing\s+(?:failed|error)|"
        r"could\s+not\s+be\s+(?:processed|summari[sz]ed))",
        re.IGNORECASE,
    ),
)

_ABSENCE_PATTERNS = (
    re.compile(
        r"\bnothing\s+(?:was\s+|were\s+)?(?:discussed|decided|happened|said|covered|recorded)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bno\s+(?:discussion|decisions?|activity|content|topics?|action\s+items?)\s+"
        rf"(?:(?:was|were)\s+(?:recorded|made|discussed|reached)\s+|occurred\s+|took\s+place\s+)?"
        rf"(?:in|during|for|within)\s+(?:that|this|the)\s+(?:\w+\s+)?{_PART}\b",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class FailureDisclosure:
    """The two prefilter labels for criterion (d)."""

    disclosed: bool
    absence_claim: bool

    @property
    def passes(self) -> bool:
        """Criterion (d): the failure is disclosed and the span is not declared empty."""
        return self.disclosed and not self.absence_claim


def split_sentences(text: str) -> list[str]:
    """Sentences of ``text``, split on terminal punctuation and newlines; empties dropped."""
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def _hits(patterns: tuple[re.Pattern[str], ...], text: str) -> list[str]:
    return [s for s in split_sentences(text) if any(p.search(s) for p in patterns)]


def truncation_claims(text: str) -> list[str]:
    """Sentences that call the recording truncated, cut off, a fragment or abruptly bounded.

    A prefilter: confirm with the judge before counting it against a result.
    """
    return _hits(_TRUNCATION_PATTERNS, text)


def nothing_decided_claims(text: str) -> list[str]:
    """Sentences stating that the meeting reached no decisions, actions or conclusions."""
    return _hits(_NOTHING_DECIDED_PATTERNS, text)


def empty_result(
    *, merged_decisions: list[str], slice_decisions: list[str], merged_prose: str
) -> bool:
    """Criterion (c): the merge emptied a result that its own slices said was not empty.

    True when the merged ``key_decisions`` is empty although a substantive slice reported a
    decision, or when the merged BLUF/brief summary says nothing was decided.
    """
    if not merged_decisions and slice_decisions:
        return True
    return bool(nothing_decided_claims(merged_prose))


def failure_disclosure(text: str) -> FailureDisclosure:
    """Criterion (d) prefilter labels: is the failed span disclosed, is it called empty?"""
    return FailureDisclosure(
        disclosed=bool(_hits(_DISCLOSURE_PATTERNS, text)),
        absence_claim=bool(_hits(_ABSENCE_PATTERNS, text)),
    )
