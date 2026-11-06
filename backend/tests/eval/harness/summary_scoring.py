"""Field extraction and per-cell scoring for the summary-merge quality gate.

A *cell* is one (input, arm, run) summary: the per-slice outputs the pipeline produced plus the
merged result. This module turns a cell into numbers, using only pure functions, so the verdict
logic and every test run with no model, no stack and no I/O.

Recall is lexical and therefore a **floor** (see ``ami_recall``): it ranks arms and finds
regressions, it is not an accuracy figure. Every item below the scorer's minimum token count is
counted in ``skipped``, never dropped, and a cell that failed to produce a result scores zero on
every metric (the ``trec_eval -c`` rule from ``docs-site/docs/developer-guide/rag-evaluation.md``).

Nothing here keeps corpus prose in a returned structure destined for a file: callers get item
*ids* for the missed items. The in-memory ``missed_text`` of :class:`RecallBreakdown` is for the
judge and for tests, and ``scores_row`` leaves it out.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from tests.eval.harness.ami_recall import score_answer
from tests.eval.harness.summary_detectors import empty_result
from tests.eval.harness.summary_detectors import failure_disclosure
from tests.eval.harness.summary_detectors import nothing_decided_claims
from tests.eval.harness.summary_detectors import truncation_claims

#: Keys a model may use for the text of a structured item (``{"text": ...}``, ``{"action": ...}``).
_ITEM_TEXT_KEYS = ("text", "description", "action", "decision", "item", "task", "title", "topic")
_MERGED_LIST_FIELDS = ("key_points", "key_decisions", "action_items", "follow_up_items")
_SLICE_DECISION_FIELDS = ("decisions", "key_decisions")
_SLICE_ACTION_FIELDS = ("action_items",)


def item_text(item: Any) -> str:
    """Text of one list element: a string as-is, a dict by its first text-like key."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        for key in _ITEM_TEXT_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return " ".join(str(v) for v in item.values() if isinstance(v, str)).strip()
    return ""


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, Sequence):
        return []
    return [t for t in (item_text(v) for v in value) if t]


@dataclass(frozen=True)
class MergedFields:
    """The fields the gate reads from a merged summary."""

    bluf: str
    brief_summary: str
    topics: list[str]
    key_points: list[str]
    key_decisions: list[str]
    action_items: list[str]
    follow_up_items: list[str]

    @property
    def prose(self) -> str:
        """BLUF and brief summary: where a "nothing was decided" claim would be made."""
        return "\n".join(t for t in (self.bluf, self.brief_summary) if t)

    @property
    def text(self) -> str:
        """Every field joined with newlines: the haystack for recall and claim detection."""
        parts = [self.bluf, self.brief_summary, *self.topics, *self.key_points]
        parts += [*self.key_decisions, *self.action_items, *self.follow_up_items]
        return "\n".join(p for p in parts if p)


def extract_merged(summary: Mapping[str, Any] | None) -> MergedFields:
    """Read a merged summary dict; a missing or malformed one yields empty fields."""
    summary = summary or {}
    topics: list[str] = []
    for topic in summary.get("major_topics") or []:
        if isinstance(topic, Mapping):
            topics.append(item_text({"topic": topic.get("topic", "")}))
            topics.extend(_items(topic.get("key_points")))
        else:
            topics.append(item_text(topic))
    return MergedFields(
        bluf=str(summary.get("bluf") or "").strip(),
        brief_summary=str(summary.get("brief_summary") or "").strip(),
        topics=[t for t in topics if t],
        key_points=_items(summary.get("key_points")),
        key_decisions=_items(summary.get("key_decisions")),
        action_items=_items(summary.get("action_items")),
        follow_up_items=_items(summary.get("follow_up_items")),
    )


@dataclass(frozen=True)
class SliceFields:
    """The fields the gate reads from one per-slice output."""

    index: int  # 1-based, as the pipeline numbers sections
    errored: bool
    decisions: list[str]
    actions: list[str]
    text: str


def extract_slice(index: int, output: Mapping[str, Any] | None) -> SliceFields:
    """Read one slice output; ``_error`` marks a pipeline failure placeholder."""
    output = output or {}
    decisions = [t for f in _SLICE_DECISION_FIELDS for t in _items(output.get(f))]
    actions = [t for f in _SLICE_ACTION_FIELDS for t in _items(output.get(f))]
    texts: list[str] = []
    for key, value in output.items():
        if key == "_error":
            continue
        texts.extend(_items(value))
    return SliceFields(
        index=index,
        errored=bool(output.get("_error")),
        decisions=decisions,
        actions=actions,
        text="\n".join(texts),
    )


def substantive_slices(
    outputs: Sequence[Mapping[str, Any] | None], *, tail_index: int | None = None
) -> list[SliceFields]:
    """Slices that count: no ``_error`` key and not the crosstalk tail (``tail_index``, 1-based)."""
    slices = [extract_slice(i, o) for i, o in enumerate(outputs, start=1)]
    return [s for s in slices if not s.errored and s.index != tail_index]


@dataclass(frozen=True)
class ItemOutcome:
    """One reference item and whether the summary carried it."""

    key: str
    group: str  # the breakdown bucket the item belongs to (a layer, a meeting, a slice)
    recalled: bool | None  # None: below the scorer's minimum token count
    overlap: float
    text: str = field(default="", repr=False)


@dataclass(frozen=True)
class RecallBreakdown:
    """Recall over a set of items, overall and per group; a floor, see the module docstring."""

    outcomes: list[ItemOutcome]

    @property
    def scored(self) -> list[ItemOutcome]:
        return [o for o in self.outcomes if o.recalled is not None]

    @property
    def skipped(self) -> int:
        return len(self.outcomes) - len(self.scored)

    @property
    def total(self) -> int:
        return len(self.scored)

    @property
    def recalled(self) -> int:
        return sum(1 for o in self.scored if o.recalled)

    @property
    def recall(self) -> float | None:
        """``None`` when there is nothing scoreable, so "no items" never reads as "0% recalled"."""
        return self.recalled / self.total if self.total else None

    @property
    def missed(self) -> list[ItemOutcome]:
        return [o for o in self.scored if not o.recalled]

    def by_group(self) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[ItemOutcome]] = defaultdict(list)
        for outcome in self.outcomes:
            buckets[outcome.group].append(outcome)
        return {g: RecallBreakdown(buckets[g]).as_row() for g in sorted(buckets)}

    def as_row(self) -> dict[str, Any]:
        """Counts and missed item ids only: safe to write to a scores file."""
        return {
            "recall": self.recall,
            "recalled": self.recalled,
            "total": self.total,
            "skipped": self.skipped,
            "missed": sorted(o.key for o in self.missed),
        }


def _score_items(answer: str, items: Iterable[tuple[str, str, str, str]]) -> RecallBreakdown:
    """Score ``(key, group, tag, text)`` items one at a time so each keeps its identity.

    ``score_answer`` is called per item rather than once per layer because it silently drops
    items under its minimum token count and returns the rest without ids; one call per item keeps
    the key attached and turns a drop into a counted ``recalled=None``.
    """
    outcomes: list[ItemOutcome] = []
    for key, group, tag, text in items:
        result = score_answer(answer, f"[{tag}] {text}")
        if result.items:
            only = result.items[0]
            outcomes.append(ItemOutcome(key, group, only.recalled, only.overlap, text))
        else:
            outcomes.append(ItemOutcome(key, group, None, 0.0, text))
    return RecallBreakdown(outcomes)


def carry_through_recall(
    merged_text: str,
    slices: Sequence[SliceFields],
    *,
    layers: Sequence[str] = ("decisions", "actions"),
) -> RecallBreakdown:
    """Criterion (a): how many slice decisions/actions reached the merged text.

    The reference is the union of the substantive slices' decisions and actions of THIS run, so it
    is measured against what each arm's own slices said (the scope rules change the slice prompt,
    which is why gold recall is reported beside it).
    """
    items: list[tuple[str, str, str, str]] = []
    for s in slices:
        for layer, texts in (("decisions", s.decisions), ("actions", s.actions)):
            if layer not in layers:
                continue
            for ordinal, text in enumerate(texts, start=1):
                items.append((f"s{s.index}.{layer[0]}{ordinal}", layer, f"slice {s.index}", text))
    return _score_items(merged_text, items)


@dataclass(frozen=True)
class GoldRef:
    """One human gold item, placed in a slice when its time is known."""

    meeting_id: str
    layer: str  # "decisions" | "actions"
    sentence_id: str
    text: str
    slice_index: int | None  # 1-based; None: unlocated, still scored


def gold_recall(merged_text: str, gold: Sequence[GoldRef]) -> dict[str, Any]:
    """Recall of the human decisions and actions in the merged text.

    Returns one breakdown per layer (``decisions``/``actions``), plus per-meeting and per-slice
    breakdowns over both layers. A layer with no gold items yields ``None`` (ICSI has no actions
    layer), never zero. Unlocated items are scored and counted under ``slice: unlocated``.
    """
    by_layer: dict[str, RecallBreakdown] = {}
    for layer in ("decisions", "actions"):
        items = [g for g in gold if g.layer == layer]
        if items:
            by_layer[layer] = _score_items(
                merged_text, [(g.sentence_id, layer, g.meeting_id, g.text) for g in items]
            )
    both = [g for g in gold if g.layer in ("decisions", "actions")]
    per_meeting = _score_items(
        merged_text, [(g.sentence_id, g.meeting_id, g.meeting_id, g.text) for g in both]
    )
    per_slice = _score_items(
        merged_text,
        [(g.sentence_id, _slice_group(g), g.meeting_id, g.text) for g in both],
    )
    return {"layers": by_layer, "per_meeting": per_meeting, "per_slice": per_slice}


@dataclass(frozen=True)
class CellScore:
    """Everything the verdict needs from one cell, with no corpus prose in it."""

    cell_id: str
    input_id: str
    arm: str
    run: int
    status: str  # "ok" | "error" | "void"
    void_reason: str | None
    carry: RecallBreakdown
    gold_decisions: RecallBreakdown | None
    gold_actions: RecallBreakdown | None
    gold_per_meeting: RecallBreakdown | None
    gold_per_slice: RecallBreakdown | None
    truncation_hits: list[str]
    slice_says_truncated: bool
    nothing_decided_hits: list[str]
    empty_decisions: bool
    empty_result: bool
    disclosed: bool
    absence_claim: bool

    @property
    def fail_b_prefilter(self) -> bool:
        return bool(self.truncation_hits) and not self.slice_says_truncated

    def scores_row(self) -> dict[str, Any]:
        """The prose-free row for ``scores.jsonl``/the verdict: ids and counts only."""
        return {
            "cell_id": self.cell_id,
            "input": self.input_id,
            "arm": self.arm,
            "run": self.run,
            "status": self.status,
            "void_reason": self.void_reason,
            "carry": self.carry.as_row(),
            "gold_decisions": self.gold_decisions.as_row() if self.gold_decisions else None,
            "gold_actions": self.gold_actions.as_row() if self.gold_actions else None,
            "gold_per_meeting": self.gold_per_meeting.by_group() if self.gold_per_meeting else None,
            "gold_per_slice": self.gold_per_slice.by_group() if self.gold_per_slice else None,
            "b": {
                "prefilter_hits": len(self.truncation_hits),
                "slice_says_truncated": self.slice_says_truncated,
                "fail": self.fail_b_prefilter,
            },
            "c": {
                "empty_decisions": self.empty_decisions,
                "prefilter_hits": len(self.nothing_decided_hits),
                "fail": self.empty_result,
            },
            "d": {"disclosed": self.disclosed, "absence_claim": self.absence_claim},
        }


def _slice_group(g: GoldRef) -> str:
    return f"slice {g.slice_index}" if g.slice_index else "unlocated"


def _zero_breakdown(
    items: Sequence[GoldRef], group: Callable[[GoldRef], str]
) -> RecallBreakdown | None:
    """A failed cell recalls nothing: zero over the same items, not "no data"."""
    if not items:
        return None
    return RecallBreakdown(
        [ItemOutcome(g.sentence_id, group(g), False, 0.0, g.text) for g in items]
    )


def _empty_breakdown_zero(items: Sequence[GoldRef], layer: str) -> RecallBreakdown | None:
    return _zero_breakdown([g for g in items if g.layer == layer], lambda g: g.layer)


def score_cell(
    *,
    cell_id: str,
    input_id: str,
    arm: str,
    run: int,
    status: str,
    void_reason: str | None,
    merged: Mapping[str, Any] | None,
    slice_outputs: Sequence[Mapping[str, Any] | None],
    gold: Sequence[GoldRef],
    tail_index: int | None,
) -> CellScore:
    """Score one cell. An ``error`` cell scores zero everywhere and counts as an empty result."""
    slices = substantive_slices(slice_outputs, tail_index=tail_index)
    if status == "error" or merged is None:
        return CellScore(
            cell_id=cell_id,
            input_id=input_id,
            arm=arm,
            run=run,
            status="error" if status != "void" else status,
            void_reason=void_reason,
            carry=carry_through_recall("", slices),
            gold_decisions=_empty_breakdown_zero(gold, "decisions"),
            gold_actions=_empty_breakdown_zero(gold, "actions"),
            gold_per_meeting=_zero_breakdown(gold, lambda g: g.meeting_id),
            gold_per_slice=_zero_breakdown(gold, _slice_group),
            truncation_hits=[],
            slice_says_truncated=False,
            nothing_decided_hits=[],
            empty_decisions=True,
            empty_result=True,
            disclosed=False,
            absence_claim=False,
        )
    fields = extract_merged(merged)
    text = fields.text
    disclosure = failure_disclosure(text)
    scored_gold = gold_recall(text, gold)
    return CellScore(
        cell_id=cell_id,
        input_id=input_id,
        arm=arm,
        run=run,
        status=status,
        void_reason=void_reason,
        carry=carry_through_recall(text, slices),
        gold_decisions=scored_gold["layers"].get("decisions"),
        gold_actions=scored_gold["layers"].get("actions"),
        gold_per_meeting=scored_gold["per_meeting"] if gold else None,
        gold_per_slice=scored_gold["per_slice"] if gold else None,
        truncation_hits=truncation_claims(text),
        slice_says_truncated=any(truncation_claims(s.text) for s in slices),
        nothing_decided_hits=nothing_decided_claims(fields.prose),
        empty_decisions=not fields.key_decisions and any(s.decisions for s in slices),
        empty_result=empty_result(
            merged_decisions=fields.key_decisions,
            slice_decisions=[d for s in slices for d in s.decisions],
            merged_prose=fields.prose,
        ),
        disclosed=disclosure.disclosed,
        absence_claim=disclosure.absence_claim,
    )
