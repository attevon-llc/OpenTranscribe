"""Recurrence scoring: planted-gold action-item groups (#461 W2.E1).

⚠️ **The planted shape is the one the DEFAULT SUMMARY PROMPT actually produces**,
``{item, owner, due_date, priority, context, mentioned_timestamp}`` — verified against
``backend/app/core/default_prompts.py`` lines 63-71 (the ``action_items`` block inside
``UNIVERSAL_CONTENT_ANALYZER_PROMPT``). It is deliberately **not**
``backend/app/schemas/summary.py``'s ``ActionItem`` (``{text, assigned_to, due_date,
priority, context, status}``, lines 44-52) — that schema is exported from
``app/schemas/__init__.py`` but grepping ``app/services`` and ``app/tasks`` finds no
caller that validates or renders a summary through it; ``SummaryData.action_items`` is
typed ``list[Any]`` and accepts whatever the prompt produces verbatim. Planting the
schema shape here would build a harness that stays green while the real ingestion path
produces the prompt shape and a recurrence detector reading ``ActionItem`` fields finds
zero groups — a harness that is wrong in the expensive direction, because it reports
success. See :data:`PLANTED_FIELDS` below, which is exactly the seven prompt keys.

**Recurrence detection itself does not exist in the product yet.** `chat/prompting.py`
already reserves a ``<recurrence>`` prompt block and `schemas/chat.py`'s
``RECURRENCE_UNAVAILABLE`` warning code, both explicitly commented "(Wave 2; no emitter
yet)". So today the only honest thing to submit against this harness is the ``none``
answerer's floor — zero groups, scoring 0 recall — exactly as Stage 4's aggregation
class established its own pre-product floor with the null answerer (see
``baselines/stage1-synthetic-answers/answers-null-control.md``). This module's scorer
does not know or care whether a detector exists; it scores whatever grouping is handed
to it, honest floor or real submission alike.

**Group precision/recall is pairwise (co-membership), not label matching.** Gold and
submitted group IDs are arbitrary and need not agree, so scoring cannot compare group
IDs directly — it compares, for every pair of items, whether the two groupings agree
on "same group or different groups". This is the standard clustering evaluation
(equivalent to B-cubed's pairwise form) and needs no bipartite matching between gold
and submitted group ids.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from dataclasses import field
from itertools import combinations
from typing import Any

#: The exact keys `UNIVERSAL_CONTENT_ANALYZER_PROMPT`'s ``action_items`` block
#: produces (`default_prompts.py` lines 63-71) — NOT `schemas/summary.py`'s
#: `ActionItem` fields. Planting any other key set tests a shape nothing emits.
PLANTED_FIELDS = ("item", "owner", "due_date", "priority", "context", "mentioned_timestamp")

MEASURES = ("group_precision", "group_recall")

#: A small, fixed vocabulary — deterministic and license-free, no corpus needed.
_RECURRING_TASKS = (
    "Update the roadmap",
    "Send the follow-up email",
    "Review the budget",
    "Schedule the next sync",
    "Finalize the vendor contract",
)
_DISTRACTOR_TASKS = (
    "Book the conference room",
    "Order new laptops",
    "Renew the office lease",
    "Print the handouts",
    "Reset the shared password",
)
_OWNERS = ("Alex Rivera", "Jordan Lee", "Sam Patel", "Casey Morgan")
_PRIORITIES = ("high", "medium", "low")


@dataclass(frozen=True)
class PlantedItem:
    """One planted action item, in the DEFAULT PROMPT's shape (see module docstring)."""

    item_id: str
    file_uuid: str
    item: str
    owner: str
    due_date: str
    priority: str
    context: str
    mentioned_timestamp: str

    def as_prompt_shape(self) -> dict[str, str]:
        """The exact dict shape the default summary prompt would have produced."""
        return {
            "item": self.item,
            "owner": self.owner,
            "due_date": self.due_date,
            "priority": self.priority,
            "context": self.context,
            "mentioned_timestamp": self.mentioned_timestamp,
        }


@dataclass(frozen=True)
class PlantedRecurrence:
    """A planted corpus: every item, plus the gold grouping.

    ``gold_groups`` maps a group id to the item ids that recur together across
    files. Distractor items (one-off, non-recurring) are NOT keys of any group —
    a correct submission groups nothing around them.
    """

    items: tuple[PlantedItem, ...]
    gold_groups: dict[str, frozenset[str]]

    @property
    def recurring_item_ids(self) -> frozenset[str]:
        return frozenset().union(*self.gold_groups.values()) if self.gold_groups else frozenset()


def _digest_seed(seed: int, *parts: str) -> int:
    """Deterministic sub-seed: stable across processes (PYTHONHASHSEED is unpinned
    here, so this never uses Python's built-in ``hash``)."""
    payload = f"{seed}:{':'.join(parts)}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def plant_recurrence(file_uuids: list[str], *, seed: int = 0) -> PlantedRecurrence:
    """Plant recurring + distractor action items across ``file_uuids``.

    Deterministic in ``(file_uuids, seed)``: the same inputs always plant the same
    items and the same gold groups, byte-for-byte, so a committed baseline over this
    generator reproduces exactly like every other corpus in this harness.

    Args:
        file_uuids: The recordings to plant items into, in the order given. Needs
            at least 2 for a recurrence to exist at all; with fewer, an empty
            :class:`PlantedRecurrence` is returned (no groups possible).
        seed: Selects which task text/owner/priority combination is used, so a
            second call with a different seed plants different-looking items over
            the same files without colliding.

    Returns:
        Every planted item (recurring and distractor) plus the gold grouping.
    """
    if len(file_uuids) < 2:
        return PlantedRecurrence(items=(), gold_groups={})

    items: list[PlantedItem] = []
    gold_groups: dict[str, frozenset[str]] = {}

    for task_index, task in enumerate(_RECURRING_TASKS):
        rank = _digest_seed(seed, "task", task) % len(file_uuids)
        # A recurring item appears in every file from `rank` onward — plausible
        # ("raised again next meeting") and guarantees at least 2 occurrences
        # whenever `rank <= len(file_uuids) - 2`.
        occurrence_files = file_uuids[rank:]
        if len(occurrence_files) < 2:
            continue
        member_ids: list[str] = []
        for occurrence_index, file_uuid in enumerate(occurrence_files):
            item_id = f"recur:{seed}:{task_index}:{occurrence_index}"
            owner = _OWNERS[_digest_seed(seed, "owner", task, file_uuid) % len(_OWNERS)]
            priority = _PRIORITIES[_digest_seed(seed, "prio", task, file_uuid) % len(_PRIORITIES)]
            items.append(
                PlantedItem(
                    item_id=item_id,
                    file_uuid=file_uuid,
                    item=task,
                    owner=owner,
                    due_date="Not specified",
                    priority=priority,
                    context=f"Raised again in this meeting, as in prior sessions ({task}).",
                    mentioned_timestamp="[00:00]",
                )
            )
            member_ids.append(item_id)
        gold_groups[f"group:{seed}:{task_index}"] = frozenset(member_ids)

    for distractor_index, task in enumerate(_DISTRACTOR_TASKS):
        file_uuid = file_uuids[_digest_seed(seed, "distractor", task) % len(file_uuids)]
        item_id = f"distractor:{seed}:{distractor_index}"
        owner = _OWNERS[_digest_seed(seed, "downer", task) % len(_OWNERS)]
        priority = _PRIORITIES[_digest_seed(seed, "dprio", task) % len(_PRIORITIES)]
        items.append(
            PlantedItem(
                item_id=item_id,
                file_uuid=file_uuid,
                item=task,
                owner=owner,
                due_date="Not specified",
                priority=priority,
                context=f"A one-off item mentioned once ({task}).",
                mentioned_timestamp="[00:00]",
            )
        )

    return PlantedRecurrence(items=tuple(items), gold_groups=gold_groups)


def _pairs(groups: dict[str, frozenset[str]]) -> set[frozenset[str]]:
    """Every unordered pair of items placed in the SAME group, across all groups."""
    out: set[frozenset[str]] = set()
    for members in groups.values():
        for a, b in combinations(sorted(members), 2):
            out.add(frozenset((a, b)))
    return out


def score_recurrence(
    gold_groups: dict[str, frozenset[str]], submitted_groups: dict[str, frozenset[str]]
) -> dict[str, float]:
    """Pairwise (co-membership) group precision/recall.

    Args:
        gold_groups: group id -> the item ids planted together.
        submitted_groups: group id -> the item ids the system grouped together.
            An empty dict (the honest pre-product floor: nothing submitted) scores
            0 recall, and precision is reported as 1.0 by the same "no submitted
            pairs, no false ones" convention :func:`answers._f1` already uses for
            an empty submitted set — never as an undefined/NaN value a mean could
            silently drop.

    Returns:
        A dict with ``group_precision`` and ``group_recall``.

    Raises:
        ValueError: ``gold_groups`` is empty — there is nothing to recall.
    """
    if not gold_groups:
        raise ValueError("score_recurrence: gold_groups is empty, nothing to score against")
    gold_pairs = _pairs(gold_groups)
    submitted_pairs = _pairs(submitted_groups)

    if not submitted_pairs:
        return {"group_precision": 1.0, "group_recall": 0.0}

    overlap = len(gold_pairs & submitted_pairs)
    precision = overlap / len(submitted_pairs)
    recall = overlap / len(gold_pairs) if gold_pairs else 0.0
    return {"group_precision": precision, "group_recall": recall}


@dataclass
class RecurrenceResult:
    """Per-file-scope and aggregate group precision/recall."""

    per_query: dict[str, dict[str, float]] = field(default_factory=dict)
    aggregate: dict[str, float] = field(default_factory=dict)
    query_count: int = 0


def evaluate_recurrence(
    gold: dict[str, dict[str, frozenset[str]]], submitted: dict[str, dict[str, frozenset[str]]]
) -> RecurrenceResult:
    """Score every scope in ``gold`` (e.g. one per corpus/run) against ``submitted``.

    Args:
        gold: scope id -> gold groups (group id -> item ids).
        submitted: scope id -> submitted groups. A scope absent here is scored as
            "nothing submitted" (the honest floor), never dropped from the mean.

    Returns:
        Per-scope and aggregate ``group_precision``/``group_recall``.

    Raises:
        ValueError: ``gold`` is empty.
    """
    if not gold:
        raise ValueError("evaluate_recurrence: gold is empty")
    result = RecurrenceResult(query_count=len(gold))
    for scope_id in sorted(gold):
        result.per_query[scope_id] = score_recurrence(gold[scope_id], submitted.get(scope_id, {}))
    for name in MEASURES:
        values = [row[name] for row in result.per_query.values()]
        result.aggregate[name] = sum(values) / len(values)
    return result


def as_json(planted: PlantedRecurrence) -> dict[str, Any]:
    """JSON-safe, deterministic form for a results/manifest file."""
    return {
        "planted_fields": list(PLANTED_FIELDS),
        "items": [
            {"item_id": item.item_id, "file_uuid": item.file_uuid, **item.as_prompt_shape()}
            for item in planted.items
        ],
        "gold_groups": {
            group_id: sorted(members) for group_id, members in sorted(planted.gold_groups.items())
        },
    }
