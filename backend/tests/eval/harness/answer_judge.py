"""LLM-as-judge for answer quality, and the calibration that makes it usable.

`ami_recall` scores an answer by lexical overlap. It is honest but it is a FLOOR:
an answer that conveys "priced at twenty-five euros" for a reference that says
"25 Euro dollars" scores as a miss. To get an absolute number rather than a lower
bound, something has to read the answer — which means a judge, which means the
judge itself has to be measured before anything is tuned on it.

⚠️ **RAW AGREEMENT IS NOT CALIBRATION.** On a skewed label distribution — and
answer-quality labels are always skewed, because most items are misses — two
annotators who never actually agree can still "agree" 80% of the time purely by
both guessing the majority class. Cohen's Kappa corrects for that chance
agreement, and the gap between the two is routinely 30+ points. Report Kappa. If
you find yourself quoting a percentage, you are quoting the wrong number.

Interpretation used here (Landis & Koch, the convention this repo follows):

    < 0.20  slight    — the judge is noise; do not tune on it
    0.21-0.40 fair    — directional only
    0.41-0.60 moderate — usable for ranking arms, not for absolute claims
    0.61-0.80 substantial — usable
    > 0.80  almost perfect

**The judge never sees which system produced the answer.** It is given the
question, the human reference and one answer. Anything that identifies the arm —
a config name, a run label, an ordering convention — is a channel through which a
preference can leak, and a judge that knows which arm is "new" is not a judge.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from typing import Literal

#: The label set. Three levels, not two: collapsing PARTIAL into either extreme
#: throws away the distinction that matters most for this system, whose failure
#: mode is answering a quarter of the question rather than answering wrongly.
Label = Literal["FULL", "PARTIAL", "NONE", "REFUSED"]

LABELS: tuple[Label, ...] = ("FULL", "PARTIAL", "NONE", "REFUSED")

#: Kept deliberately terse and mechanical. A rubric that invites the judge to
#: reason about "helpfulness" or "quality" measures the judge's taste; this one
#: asks only whether the reference's content is present, which is checkable and
#: is what the product claims.
JUDGE_PROMPT = """You are grading one answer against a human-written reference.

The reference is authoritative: it was written by a human annotator who watched \
the recordings. Grade ONLY whether the reference's content appears in the answer. \
Do not reward fluency, structure, length, or extra material not in the reference.

Return EXACTLY one JSON object, no other text:
{"label": "FULL"|"PARTIAL"|"NONE"|"REFUSED", "covered": <int>, "total": <int>, \
"why": "<one short sentence>"}

label meanings:
  FULL     - essentially every distinct point in the reference appears in the answer
  PARTIAL  - at least one distinct point appears, but not most of them
  NONE     - the answer is on-topic but carries none of the reference's points
  REFUSED  - the answer declines to answer (says it lacks the information)

Paraphrase COUNTS as covered. "priced at twenty-five euros" covers "25 Euro dollars".
A point the answer contradicts is NOT covered.
REFUSED takes precedence: if the answer declines, label REFUSED even if it then \
volunteers some related material.

QUESTION:
{question}

REFERENCE (authoritative):
{reference}

ANSWER TO GRADE:
{answer}
"""

#: ⚠️ This pattern produced TWO false alarms before it was widened, and both were
#: reported as safety regressions before being checked. A model that answers
#:
#:     "there is no speaker named 'Legal Counsel'. The speakers present are ..."
#:     "the provided excerpts do not INCLUDE a speaker named 'Head of Procurement'"
#:
#: is refusing correctly — arguably better than a bare refusal, since it says who IS
#: present — but neither phrasing matched a pattern built around "do not CONTAIN".
#: A negative-control score computed with a narrow pattern reports hallucination
#: where there is none, which is the most alarming possible false positive.
#:
#: Treat this as a CHEAP PRE-FILTER, never as the verdict. The refusal label that
#: counts comes from the judge (`Judgement.label == "REFUSED"`), which reads the
#: sentence instead of pattern-matching it. This exists so a judge run is not
#: required just to triage a batch.
_REFUSAL_RE = re.compile(
    r"do(es)? not (contain|include|mention|cover|have|provide)"
    r"|no (relevant )?(information|mention|discussion|statements?|details?)"
    r"|not (mentioned|discussed|present|contained|included)"
    r"|there (is|are) no\b|no speaker named|does not appear|no such\b"
    r"|cannot find|can't find|don't have|do not have|unable to find",
    re.IGNORECASE,
)


def looks_like_refusal(answer: str) -> bool:
    """Cheap pre-filter for "the answer declined to answer".

    NOT a verdict — see `_REFUSAL_RE`. Use the judge for a label you will report.
    """
    return bool(_REFUSAL_RE.search(answer or ""))


@dataclass
class Judgement:
    """One graded answer."""

    label: Label
    covered: int
    total: int
    why: str
    #: True when the label came from the fallback rather than the model. Counted
    #: separately and never silently mixed in: a calibration computed over
    #: fallback labels measures the regex, not the judge.
    degraded: bool = False


def build_judge_prompt(question: str, reference: str, answer: str) -> str:
    """Render the grading prompt.

    Uses ``str.replace`` rather than ``format``/f-strings on purpose: the
    reference and answer are arbitrary transcript-derived text, and a literal
    ``{...}`` in either would raise or interpolate. Same reasoning as
    ``services/chat/prompting.py``.
    """
    return (
        JUDGE_PROMPT.replace("{question}", question or "")
        .replace("{reference}", reference or "")
        .replace("{answer}", answer or "")
    )


def parse_judgement(raw: str, *, answer: str = "") -> Judgement:
    """Parse the judge's reply, degrading to a refusal check rather than raising.

    A judge that returns unparseable text on one item must not abort a
    calibration run over 80 — but the fallback is marked ``degraded`` so those
    items can be excluded from the Kappa, because a Kappa computed partly over
    regex output is measuring the regex.
    """
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if match:
        try:
            payload = json.loads(match.group(0))
            label = str(payload.get("label", "")).upper()
            if label in LABELS:
                return Judgement(
                    label=label,  # type: ignore[arg-type]
                    covered=int(payload.get("covered") or 0),
                    total=int(payload.get("total") or 0),
                    why=str(payload.get("why") or "")[:200],
                )
        except (ValueError, TypeError):
            pass
    fallback: Label = "REFUSED" if _REFUSAL_RE.search(answer or "") else "NONE"
    return Judgement(
        label=fallback, covered=0, total=0, why="unparseable judge reply", degraded=True
    )


def cohens_kappa(a: list[str], b: list[str]) -> float:
    """Cohen's Kappa between two annotators' label sequences.

    Args:
        a: Annotator A's labels, aligned index-wise with ``b``.
        b: Annotator B's labels.

    Returns:
        Kappa in ``[-1, 1]``. Returns ``1.0`` when both annotators used a single
        identical label for everything — perfect agreement whose chance-corrected
        form is otherwise 0/0. That case is reported rather than hidden, because
        it means the sample had no label variety and the Kappa is uninformative
        either way.

    Raises:
        ValueError: The two sequences differ in length.
    """
    if len(a) != len(b):
        raise ValueError(f"annotator sequences differ in length: {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return 0.0
    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    count_a, count_b = Counter(a), Counter(b)
    expected = sum((count_a[k] / n) * (count_b[k] / n) for k in set(count_a) | set(count_b))
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)


def interpret_kappa(kappa: float) -> str:
    """Landis & Koch band for ``kappa``, plus what it licenses."""
    if kappa < 0.20:
        return "slight — the judge is noise; do NOT tune on it"
    if kappa < 0.41:
        return "fair — directional only"
    if kappa < 0.61:
        return "moderate — usable for RANKING arms, not for absolute claims"
    if kappa < 0.81:
        return "substantial — usable"
    return "almost perfect"


def agreement_report(judge: list[str], human: list[str]) -> dict[str, Any]:
    """Compare judge labels against human labels.

    Reports raw agreement ALONGSIDE Kappa, and the gap between them, precisely so
    the raw number cannot be quoted on its own — on a skewed label distribution it
    routinely overstates agreement by 30+ points.
    """
    n = len(judge)
    raw = sum(1 for x, y in zip(judge, human, strict=True) if x == y) / n if n else 0.0
    kappa = cohens_kappa(judge, human)
    confusion: Counter[tuple[str, str]] = Counter(zip(judge, human, strict=True))
    return {
        "n": n,
        "raw_agreement": raw,
        "cohens_kappa": kappa,
        "interpretation": interpret_kappa(kappa),
        "overstatement": raw - kappa,
        "judge_distribution": dict(Counter(judge)),
        "human_distribution": dict(Counter(human)),
        "confusion": {f"judge={j}|human={h}": c for (j, h), c in sorted(confusion.items())},
    }
