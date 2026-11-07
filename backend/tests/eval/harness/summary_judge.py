"""LLM-as-judge for the summary quality gate: only where lexical scoring cannot decide.

Three jobs (plan 5.3): paraphrase recall of items the lexical floor marked missed, whether a
prefilter-hit sentence really claims the recording is incomplete, and whether a failed span is
disclosed or declared empty. Judge discipline is inherited from ``answer_judge``:

* **blind**: a prompt contains only the item list and the summary text, never an arm, input id or
  run label, and items are shuffled with a fixed seed;
* **not the model under test**, ``temperature`` fixed at :data:`JUDGE_TEMPERATURE` (callers whose
  model rejects a non-default temperature record that in the judge provenance);
* **a degraded reply is counted, never silently scored**: an unparseable reply returns
  ``degraded=True`` and the caller excludes it from any kappa.

The judge is a plain ``Callable[[str], str]`` (prompt in, raw reply out), so every test uses a
fake and no network or key is needed. :func:`llm_service_judge` adapts the app's own
``LLMService`` transport, the one the repo already uses for Anthropic, vLLM and Ollama.

The judge's validation (Cohen's kappa against hand labels, ``agreement_report``) decides how far
it may be trusted; :func:`judge_policy` encodes the plan's table.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from tests.eval.harness.answer_judge import JUDGE_TEMPERATURE
from tests.eval.harness.answer_judge import agreement_report
from tests.eval.harness.summary_detectors import split_sentences
from tests.eval.harness.summary_detectors import truncation_claims

JudgeFn = Callable[[str], str]

SHUFFLE_SEED = 20261105
NON_HIT_SAMPLE_FRACTION = 0.2

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

RECALL_PROMPT = """You are checking a meeting summary against human-written items.

For each numbered item, decide whether the SUMMARY conveys it. A paraphrase counts. A statement \
that contradicts the item, or merely mentions the topic without the item's content, does not.
Do not reward length or fluency.

Reply with JSON only: {"conveyed": [<numbers of the items the summary conveys>]}

ITEMS:
{items}

SUMMARY:
{summary}
"""

CLAIMS_PROMPT = """Below are numbered sentences taken from a meeting summary.

For each sentence, decide whether it claims that the RECORDING or TRANSCRIPT itself is \
incomplete, truncated, cut off, partial or a fragment. A sentence about unfinished work, a \
prototype, a budget cut or the meeting's own agenda is NOT such a claim.

Reply with JSON only: {"claims_incomplete": [<numbers of the sentences that make such a claim>]}

SENTENCES:
{sentences}
"""

NOTHING_DECIDED_PROMPT = """Below are numbered sentences taken from a meeting summary.

For each sentence, decide whether it states that the WHOLE MEETING reached no decisions, \
actions or conclusions. A sentence that says one topic is still undecided while the team agreed \
other things is NOT such a statement.

Reply with JSON only: {"claims_nothing_decided": [<numbers of the sentences that say so>]}

SENTENCES:
{sentences}
"""

DISCLOSURE_PROMPT = """Below is a meeting summary. One section of the meeting could not be \
processed, so its content is unavailable.

Answer two questions about the summary:
1. DISCLOSED: does it say that some part of the meeting could not be processed or is missing?
2. ABSENCE_CLAIM: does it say that nothing was discussed or decided in that part (or claim the \
meeting reached no decision on a topic) as if the part were known to be empty?

Reply with JSON only: {"disclosed": true|false, "absence_claim": true|false}

SUMMARY:
{summary}
"""


@dataclass(frozen=True)
class Judged:
    """A parsed judge reply. ``degraded`` replies carry no usable label."""

    value: Any
    degraded: bool = False


def _fill(template: str, **fields: str) -> str:
    """``str.replace`` substitution: the fields are transcript-derived and may contain braces."""
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out


def _numbered(texts: Sequence[str]) -> str:
    return "\n".join(f"{n}. {t}" for n, t in enumerate(texts, start=1))


def shuffled(items: Sequence[Any], *, seed: int = SHUFFLE_SEED) -> list[Any]:
    """A fixed-seed shuffle: the order carries no arm or ordering hint to the judge."""
    order = np.random.default_rng(seed).permutation(len(items))
    return [items[int(i)] for i in order]


def build_recall_prompt(
    summary: str, items: Sequence[tuple[str, str]], *, seed: int
) -> tuple[str, list[str]]:
    """The recall prompt and the item ids in the (shuffled) order they were numbered."""
    ordered = shuffled(items, seed=seed)
    prompt = _fill(RECALL_PROMPT, items=_numbered([text for _, text in ordered]), summary=summary)
    return prompt, [item_id for item_id, _ in ordered]


def _parse_json(raw: str) -> Mapping[str, Any] | None:
    match = _JSON_OBJECT.search(raw or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _numbers(payload: Mapping[str, Any] | None, key: str, upper: int) -> Judged:
    values = None if payload is None else payload.get(key)
    if not isinstance(values, list) or not all(
        isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= upper for v in values
    ):
        return Judged(None, degraded=True)
    return Judged(sorted(set(values)))


def judge_recall(
    judge: JudgeFn, summary: str, items: Sequence[tuple[str, str]], *, seed: int = SHUFFLE_SEED
) -> Judged:
    """Which of ``items`` ((id, text) pairs) does the summary convey? ``value`` is a set of ids."""
    if not items:
        return Judged(set())
    prompt, ids = build_recall_prompt(summary, items, seed=seed)
    parsed = _numbers(_parse_json(judge(prompt)), "conveyed", len(ids))
    if parsed.degraded:
        return parsed
    return Judged({ids[n - 1] for n in parsed.value})


def judge_claims(judge: JudgeFn, sentences: Sequence[str], *, seed: int = SHUFFLE_SEED) -> Judged:
    """Which of ``sentences`` claim the recording is incomplete? ``value`` is a list of sentences."""
    if not sentences:
        return Judged([])
    ordered = shuffled(sentences, seed=seed)
    parsed = _numbers(
        _parse_json(judge(_fill(CLAIMS_PROMPT, sentences=_numbered(ordered)))),
        "claims_incomplete",
        len(ordered),
    )
    if parsed.degraded:
        return parsed
    return Judged([ordered[n - 1] for n in parsed.value])


def judge_nothing_decided(
    judge: JudgeFn, sentences: Sequence[str], *, seed: int = SHUFFLE_SEED
) -> Judged:
    """Which prefilter-hit ``sentences`` say the whole meeting decided nothing?"""
    if not sentences:
        return Judged([])
    ordered = shuffled(sentences, seed=seed)
    parsed = _numbers(
        _parse_json(judge(_fill(NOTHING_DECIDED_PROMPT, sentences=_numbered(ordered)))),
        "claims_nothing_decided",
        len(ordered),
    )
    if parsed.degraded:
        return parsed
    return Judged([ordered[n - 1] for n in parsed.value])


def judge_disclosure(judge: JudgeFn, summary: str) -> Judged:
    """``value`` is ``(disclosed, absence_claim)``."""
    payload = _parse_json(judge(_fill(DISCLOSURE_PROMPT, summary=summary)))
    if payload is None or not all(
        isinstance(payload.get(k), bool) for k in ("disclosed", "absence_claim")
    ):
        return Judged(None, degraded=True)
    return Judged((payload["disclosed"], payload["absence_claim"]))


def sentence_id(sentence: str) -> str:
    """A short stable id for a judged sentence, so a label can be keyed without keeping prose."""
    return hashlib.sha256(sentence.encode("utf-8")).hexdigest()[:10]


def truncation_candidates(text: str, *, seed: int) -> tuple[list[str], list[str]]:
    """``(prefilter hits, a fixed 20% sample of the non-hits)`` for the (b) judge.

    The non-hit sample is how the prefilter's MISSES are measured: if the judge says a sentence
    the regex passed is a claim, the regex under-reports and the gate cannot trust it alone.
    """
    hits = truncation_claims(text)
    non_hits = [s for s in split_sentences(text) if s not in hits]
    count = (
        min(len(non_hits), max(1, round(len(non_hits) * NON_HIT_SAMPLE_FRACTION)))
        if non_hits
        else 0
    )
    picked = np.random.default_rng(seed).choice(len(non_hits), size=count, replace=False)
    sample = [non_hits[int(i)] for i in picked] if count else []
    return hits, sample


def judge_cell(
    judge: JudgeFn,
    *,
    summary_text: str,
    missed_items: Sequence[tuple[str, str]],
    slice_says_truncated: bool,
    failure_input: bool,
    seed: int,
    nothing_decided_hits: Sequence[str] = (),
    empty_decisions: bool = False,
) -> dict[str, Any]:
    """All judge verdicts for one cell. Takes no arm, input or run identity: the judge is blind.

    Returns ``{"recall_conveyed": [ids], "recall_labels": {id: bool}, "b_labels": {sentence_id: bool},
    "b_fail": bool, "c_fail": bool, "disclosed": bool,
    "absence_claim": bool, "degraded": [names]}`` (the last two only for a failure input); a
    degraded call contributes nothing and is listed. ``c_fail`` is true when the merge emptied
    the decisions (``empty_decisions``, no judgement needed) or the judge confirms a
    "nothing was decided" prefilter hit.
    """
    degraded: list[str] = []
    out: dict[str, Any] = {}

    recall = judge_recall(judge, summary_text, missed_items, seed=seed)
    if recall.degraded:
        degraded.append("recall")
    out["recall_conveyed"] = sorted(recall.value or [])
    out["recall_labels"] = {item_id: item_id in (recall.value or ()) for item_id, _ in missed_items}

    hits, sample = truncation_candidates(summary_text, seed=seed)
    claims = judge_claims(judge, [*hits, *sample], seed=seed)
    if claims.degraded:
        degraded.append("claims")
    confirmed = claims.value or []
    out["b_labels"] = {sentence_id(s): s in confirmed for s in [*hits, *sample]}
    out["b_fail"] = bool(confirmed) and not slice_says_truncated
    out["b_prefilter_missed"] = sum(1 for s in confirmed if s not in hits)

    nothing = judge_nothing_decided(judge, nothing_decided_hits, seed=seed)
    if nothing.degraded:
        degraded.append("nothing_decided")
    out["c_fail"] = empty_decisions or bool(nothing.value)

    if failure_input:
        disclosure = judge_disclosure(judge, summary_text)
        if disclosure.degraded:
            degraded.append("disclosure")
        disclosed, absence = disclosure.value or (False, False)
        out["disclosed"], out["absence_claim"] = disclosed, absence
    out["degraded"] = degraded
    return out


def judge_policy(kappa: float) -> str:
    """How far the judge may be used, from its kappa against hand labels (plan 5.3 table)."""
    if kappa >= 0.61:
        return "verdict"
    if kappa >= 0.41:
        return "rank"
    return "unused"


def calibration_report(
    judge_labels: Mapping[str, str], human_labels: Mapping[str, str]
) -> dict[str, Any]:
    """Kappa, confusion matrix and the allowed use, over the blind ids both sides labelled.

    Raises:
        ValueError: the two label sets cover different blind ids (a hole is a defect).
    """
    if set(judge_labels) != set(human_labels):
        raise ValueError(
            f"judge and human labels differ: {len(set(judge_labels) ^ set(human_labels))} blind "
            "id(s) are labelled by only one side"
        )
    ids = sorted(judge_labels)
    report = agreement_report([judge_labels[i] for i in ids], [human_labels[i] for i in ids])
    report["policy"] = judge_policy(report["cohens_kappa"])
    return report


def export_calibration(
    pool: Sequence[Mapping[str, Any]], *, recall_pairs: int, seed: int = SHUFFLE_SEED
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """A blind, shuffled calibration set and the key that maps it back.

    Args:
        pool: dicts with ``kind`` (``recall``/``b``/``d``), ``cell_id``, ``item_id``, ``text`` (the
            item or sentence; ``""`` for ``d``) and ``summary``. Recall entries are subsampled to
            ``recall_pairs``, balanced across inputs (taken from ``cell_id``'s first field); every
            ``b`` and ``d`` entry is kept.
        recall_pairs: how many recall pairs to keep (the plan asks for 40).

    Returns:
        ``(items, key)``. ``items`` carry only ``blind_id``, ``kind``, ``text`` and ``summary``;
        ``key`` maps ``blind_id`` to the cell and item and stays with the lead, never the labeller.
    """
    recall = [p for p in pool if p["kind"] == "recall"]
    by_input: dict[str, list[Mapping[str, Any]]] = {}
    for entry in recall:
        by_input.setdefault(str(entry["cell_id"]).split("|", 1)[0], []).append(entry)
    chosen: list[Mapping[str, Any]] = []
    buckets = [shuffled(v, seed=seed) for _, v in sorted(by_input.items())]
    while len(chosen) < recall_pairs and any(buckets):
        for bucket in buckets:
            if bucket and len(chosen) < recall_pairs:
                chosen.append(bucket.pop())
    chosen += [p for p in pool if p["kind"] != "recall"]
    chosen = shuffled(chosen, seed=seed)
    items: list[dict[str, Any]] = []
    key: dict[str, dict[str, Any]] = {}
    for n, entry in enumerate(chosen, start=1):
        blind = f"c{n:03d}"
        items.append(
            {
                "blind_id": blind,
                "kind": entry["kind"],
                "text": entry["text"],
                "summary": entry["summary"],
            }
        )
        key[blind] = {
            "cell_id": entry["cell_id"],
            "item_id": entry["item_id"],
            "kind": entry["kind"],
        }
    return items, key


def llm_service_judge(config: Any) -> tuple[JudgeFn, dict[str, Any]]:
    """Adapt the app's ``LLMService`` transport into a :data:`JudgeFn`, plus its provenance.

    ``config`` is an ``LLMConfig``. The service is built directly from it (no DB-backed
    resolution). The key never enters the provenance. Providers whose current models reject a
    non-default temperature (the newer Claude models do) get no ``temperature`` and the provenance
    says so, rather than recording a setting that was never sent.
    """
    from app.services.llm_service import LLMService

    service = LLMService(config)
    provider = config.provider.value
    send_temperature = provider != "anthropic" or not config.model.startswith(
        ("claude-haiku-5", "claude-sonnet-5", "claude-opus-5", "claude-fable", "claude-mythos")
    )

    def judge(prompt: str) -> str:
        kwargs: dict[str, Any] = {"max_tokens": 1024}
        if send_temperature:
            kwargs["temperature"] = JUDGE_TEMPERATURE
        # A judge call grades the benchmark; it is not a product feature, so it neither counts
        # against nor needs the deployment's token limits (the host runner has no database).
        return service.chat_completion(
            [{"role": "user", "content": prompt}], enforce_budget=False, **kwargs
        ).content

    provenance = {
        "provider": provider,
        "model": config.model,
        "base_url": config.base_url,
        "temperature": JUDGE_TEMPERATURE if send_temperature else "provider default (not settable)",
    }
    return judge, provenance
