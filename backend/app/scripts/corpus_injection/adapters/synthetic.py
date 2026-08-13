"""Synthetic-tier adapter — the generator's own on-disk format, read directly.

``backend/tests/eval/synthetic`` writes ``meetings/part-*.jsonl`` with
``turns[].content`` and a ``meeting_key``; this adapter reads exactly that. No
conversion step, no second copy of a 233 MB corpus, and nothing that can drift
from what the generator emitted.

**``meeting_id`` is the generator's ``meeting_key``, and that is load-bearing.**
``tests/eval/harness/corpora.load_synthetic_queries`` joins the corpus's own
``file_uuid`` to the one injection derives *through* ``meeting_key``. Key the
adapter on anything else and every gold set silently fails to resolve, which
presents as "no scoreable queries" rather than as an error.

**Timings.** The generator supplies ``start``/``end`` per turn and they are kept
— the pacing and turn-length structure are deliberate — but
:func:`~..timings.resolve_timings` records them as ``synthetic`` with generator
``corpus_supplied_v1`` and NULLs every word-level timing. A generated number is
not a measurement however plausible it looks (``.rag-403/corpus-injection.md``
§3).

Why a budget exists
-------------------

The default rung is 2,000 meetings / 22.4 M words — **10.6x QMSum**. Injecting it
whole would make the synthetic tier the index rather than an addition to it, and
every QMSum number measured beside it would be a new control rather than a
comparison. So the meeting count is a parameter.

A budget cannot be a plain truncation. Gold sets for the two classes this tier
exists to make measurable span files: multi-file components live in distinct
sessions of one series, and aggregation markers are planted with
``rng.sample(org.sessions, k)`` over the **whole corpus**. The harness drops any
query whose gold set is only partly on the stack (correctly — its recall
denominator would silently shrink), so a first-N-by-key subset scores close to
zero aggregation queries. :func:`select_gold_closure` therefore selects *whole
queries* and takes the union of the files they need.
"""

from __future__ import annotations

import json
import logging
from functools import cached_property
from pathlib import Path
from typing import Any

from app.scripts.corpus_injection.adapters.base import CorpusAdapter
from app.scripts.corpus_injection.model import CorpusInfo
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn

logger = logging.getLogger(__name__)

#: Default meeting budget. 200 meetings of ``otsynth-core-v1`` is 2.31 M words
#: (1.10x QMSum) and 101,620 turns (0.79x QMSum) — comparable to the existing
#: corpus rather than dominant over it — and yields 25 multi-file and 21
#: aggregation scoreable queries, the two classes with no ground truth anywhere
#: else. Measured, not guessed; the numbers are in the PR body.
DEFAULT_MEETING_BUDGET = 200

#: Classes the budget is spent on, in cycle order. multi-file and aggregation
#: are the two #403 classes with no publishable real-data ground truth, so they
#: are what a synthetic corpus is *for*; lookup and summarize are already
#: measurable on QMSum and are scored here only where a selected meeting happens
#: to close one of them.
DEFAULT_SELECT_FOR = ("multi_file", "aggregation")

#: Files whose presence identifies a generated corpus directory.
_REQUIRED = ("config.json", "queries.jsonl")


class SyntheticCorpusError(FileNotFoundError):
    """The directory does not hold a corpus this adapter can read."""


def locate_corpus(root: Path) -> Path:
    """Resolve the directory holding ``config.json`` + ``meetings/``.

    ``$RAG_EVAL_DATA_DIR/synthetic`` holds one directory per generated rung
    (``otsynth-core-v1/``, ``otsynth-n5000/``, ...), so the registry's default
    root is one level above the corpus. Accepting either keeps ``--corpus-root``
    usable for a relocated copy without a version-pinned path — the same shape
    :class:`~.qmsum.QMSumAdapter` uses for its extracted-archive level.

    Raises:
        SyntheticCorpusError: No corpus directory at or under ``root``.
    """
    root = Path(root)
    if all((root / name).is_file() for name in _REQUIRED):
        return root
    candidates = sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and all((child / name).is_file() for name in _REQUIRED)
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SyntheticCorpusError(
            f"No generated corpus under {root} (looked for {' + '.join(_REQUIRED)}). "
            f"Generate one with: python3 -m tests.eval.synthetic generate --out <dir>"
        )
    names = ", ".join(c.name for c in candidates)
    raise SyntheticCorpusError(
        f"{len(candidates)} generated corpora under {root} ({names}). "
        f"Pick one with --corpus-root; injecting several at once would mix their "
        f"query sets under one manifest."
    )


def select_gold_closure(
    queries: list[dict[str, Any]],
    key_by_uuid: dict[str, str],
    budget: int,
    classes: tuple[str, ...] = DEFAULT_SELECT_FOR,
) -> dict[str, list[str]]:
    """Choose meetings so that whole queries stay scoreable within ``budget``.

    Queries are visited class by class in ``classes`` order, one per cycle, and
    within a class in ``query_id`` order — which is generation order, so the
    realised gold-set sizes track the full corpus's rather than skewing to the
    cheap end. A query whose files would overflow the budget is skipped and the
    next one tried; the budget only shrinks, so the pass terminates.

    ``related_files`` count towards a query's requirement as well as
    ``gold_files``. R7 ("how many meetings in <month> discussed X") is only hard
    while the out-of-month mentions are also in the index; injecting the gold
    set alone would turn a filtered count into an unfiltered one.

    Args:
        queries: Parsed ``queries.jsonl`` records.
        key_by_uuid: The corpus's own ``file_uuid`` -> ``meeting_key``.
        budget: Maximum number of meetings to select.
        classes: Query classes to spend the budget on, in cycle order.

    Returns:
        ``meeting_key -> [query_id, ...]`` — the queries that pulled each
        meeting in, sorted, for the manifest's audit trail.
    """
    by_class: dict[str, list[tuple[str, list[str]]]] = {name: [] for name in classes}
    for query in sorted(queries, key=lambda q: str(q.get("query_id", ""))):
        bucket = by_class.get(str(query.get("query_class")))
        if bucket is None:
            continue
        wanted = set(query.get("gold_files") or []) | set(query.get("related_files") or [])
        needed = sorted({key_by_uuid[uuid] for uuid in wanted if uuid in key_by_uuid})
        # A query referencing a file this corpus copy does not contain cannot be
        # satisfied at any budget; selecting for it would spend meetings on a
        # query the harness will drop anyway.
        if needed and len(needed) == len(wanted):
            bucket.append((str(query["query_id"]), needed))

    chosen: dict[str, list[str]] = {}
    cursor = dict.fromkeys(classes, 0)
    advanced = True
    while advanced:
        advanced = False
        for name in classes:
            entries = by_class[name]
            while cursor[name] < len(entries):
                query_id, needed = entries[cursor[name]]
                cursor[name] += 1
                if len(set(chosen) | set(needed)) > budget:
                    continue
                for key in needed:
                    chosen.setdefault(key, []).append(query_id)
                advanced = True
                break
    return {key: sorted(ids) for key, ids in chosen.items()}


class SyntheticAdapter(CorpusAdapter):
    """Read ``otsynth-*`` shards, selecting a gold-closed subset by default."""

    key = "synthetic"
    subset_breaks_gold_closure = True

    def __init__(
        self,
        root: Path,
        meetings: int = DEFAULT_MEETING_BUDGET,
        select_for: tuple[str, ...] = DEFAULT_SELECT_FOR,
    ) -> None:
        super().__init__(locate_corpus(root))
        self.budget = int(meetings)
        self.select_for = tuple(select_for)
        self._uuid_to_key: dict[str, str] = {}

    # ---------------------------------------------------------------- layout

    @cached_property
    def config(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        return loaded

    @cached_property
    def shards(self) -> dict[str, tuple[Path, int, int]]:
        """``meeting_key -> (shard, byte offset, byte length)``.

        One streaming pass over every shard (2.6 s for 233 MB), so a meeting is
        then parsed on demand instead of holding the whole corpus in memory —
        22.4 M words of Python objects is several GB.
        """
        index: dict[str, tuple[Path, int, int]] = {}
        self._uuid_to_key = {}
        for shard in sorted((self.root / "meetings").glob("*.jsonl")):
            offset = 0
            with shard.open("rb") as handle:
                for raw in handle:
                    length = len(raw)
                    if raw.strip():
                        record = json.loads(raw)
                        key = str(record["meeting_key"])
                        index[key] = (shard, offset, length)
                        self._uuid_to_key[str(record["file_uuid"])] = key
                    offset += length
        return index

    @property
    def uuid_to_key(self) -> dict[str, str]:
        """The corpus's own ``file_uuid`` -> ``meeting_key``."""
        self.shards  # noqa: B018 — populates the map as a side effect of indexing
        return self._uuid_to_key

    @cached_property
    def queries(self) -> list[dict[str, Any]]:
        text = (self.root / "queries.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    @cached_property
    def selection(self) -> dict[str, list[str]]:
        """``meeting_key -> query ids that selected it``; all meetings if unbudgeted."""
        if self.budget <= 0 or self.budget >= len(self.shards):
            return {key: [] for key in sorted(self.shards)}
        chosen = select_gold_closure(self.queries, self.uuid_to_key, self.budget, self.select_for)
        logger.info(
            "Selected %d/%d meetings by gold closure over %s queries",
            len(chosen),
            len(self.shards),
            "+".join(self.select_for),
        )
        return chosen

    # -------------------------------------------------------------- contract

    def describe(self) -> CorpusInfo:
        config = self.config
        selection = (
            "all"
            if len(self.selection) == len(self.shards)
            else f"gold-closure[{'+'.join(self.select_for)}]"
        )
        return CorpusInfo(
            key=self.key,
            name=f"OpenTranscribe synthetic tier ({config.get('corpus_id', self.root.name)})",
            # The selection is part of the version because it is part of what was
            # injected: two budgets are two different index states and must not
            # record the same corpus version in a results file.
            version=(
                f"{config.get('corpus_id', self.root.name)}"
                f"@{config.get('generator_version', 'unknown')}"
                f"/seed={config.get('seed', 'unknown')}"
                f"/select={selection}"
                f"/meetings={len(self.selection)}of{len(self.shards)}"
            ),
            license_tier="A",
            root=str(self.root),
            citation=(
                "OpenTranscribe synthetic meeting corpus, generated by "
                "backend/tests/eval/synthetic (no third-party text, no LLM). "
                "AGPL-3.0-or-later. Method: .rag-403/synthetic-tier-design.md"
            ),
        )

    def meeting_ids(self) -> list[str]:
        return sorted(self.selection)

    def raw_record(self, meeting_id: str) -> dict[str, Any]:
        """One meeting exactly as the generator wrote it, read by byte offset."""
        shard, offset, length = self.shards[meeting_id]
        with shard.open("rb") as handle:
            handle.seek(offset)
            record: dict[str, Any] = json.loads(handle.read(length))
        return record

    def load(self, meeting_id: str) -> MeetingDoc:
        record = self.raw_record(meeting_id)
        turns = [
            Turn(
                turn_index=int(turn.get("index", position)),
                speaker=str(turn.get("speaker") or "Unknown").strip(),
                text=str(turn.get("content") or "").strip(),
                start=_as_float(turn.get("start")),
                end=_as_float(turn.get("end")),
            )
            for position, turn in enumerate(record.get("turns", []))
        ]
        return MeetingDoc(
            corpus=self.key,
            meeting_id=str(record["meeting_key"]),
            title=str(record.get("title") or record["meeting_key"]),
            turns=turns,
            language="en",
            # Never TIMING_REAL: a generated timestamp is not a measurement.
            timing=TimingInfo(source="synthetic", reference=None),
            extra={
                "license_tier": "A",
                "corpus_id": str(record.get("corpus_id") or ""),
                # The generator's own uuid. Recording it here is what lets the
                # corpus-side gold sets be resolved from the manifest alone.
                "corpus_file_uuid": str(record.get("file_uuid") or ""),
                "team_id": str(record.get("team_id") or ""),
                "series_id": str(record.get("series_id") or ""),
                "series_kind": str(record.get("series_kind") or ""),
                "register": str(record.get("register") or ""),
                "date": str(record.get("date") or ""),
                "near_duplicate_cluster": record.get("near_duplicate_cluster"),
                "selected_for": self.selection.get(meeting_id, []),
            },
        )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
