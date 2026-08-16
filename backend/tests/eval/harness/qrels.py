"""Gold turn ranges -> chunk-level relevance judgements.

**One adapter, two corpora.** QMSum's ``relevant_text_span`` is a list of
``[start, end]`` turn indices as decimal strings with an **inclusive end**; the
synthetic tier deliberately publishes ``gold_turns`` in the same convention
(``.rag-403/synthetic-tier-design.md`` §5.4). Anything that needs a second
adapter has mis-factored the problem.

The mapping is the intellectual content of Stage 1 and the part a reviewer can
legitimately challenge, so it is stated rather than buried:

**Turn -> chunk.** The indexer chunks by speaker turn
(``chunk_transcript_by_speaker_turns``) and the chunk document records
``speaker`` / ``start_time`` / ``end_time`` but **no segment ids**. A chunk is
therefore matched to source turns by *time overlap restricted to the chunk's own
speaker*. The speaker restriction is what makes this exact rather than
approximate: a chunk contains only its own speaker's segments, while overlapping
speech means another speaker's turn can share its time window. Dropping the
restriction would attribute a neighbour's words to the chunk.

**Coverage.** Each covered turn contributes ``word_count * (seconds of the turn
inside the chunk / turn duration)`` — the scaling matters because a long
monologue is split into sub-chunks mid-turn. Coverage is the gold share of that
total.

**Coverage -> graded relevance.** A parameter, not a magic constant
(:class:`RelevancePolicy`). The default ladder is ``>= 0.5 -> 2``,
``> 0.0 -> 1``, else 0: a chunk at least half made of gold material is fully
relevant; one that merely clips the edge of a span is marginal but not
irrelevant, because a retriever ranking it above unrelated material is behaving
correctly. Under linear gain a 2 is worth exactly twice a 1. ``--binary``
collapses the ladder for anyone who considers grading unjustified; both settings
are recorded in the results file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnRow:
    """One source turn, as recorded in the injection manifest's ``turns.jsonl``."""

    file_uuid: str
    turn_index: int
    speaker: str
    start: float
    end: float
    word_count: int


@dataclass(frozen=True)
class ChunkDoc:
    """One indexed chunk, as read back from the chunk index."""

    file_uuid: str
    chunk_index: int
    speaker: str
    start_time: float
    end_time: float
    doc_type: str = "chunk"

    @property
    def doc_id(self) -> str:
        return f"{self.file_uuid}_{self.chunk_index}"


@dataclass(frozen=True)
class GoldSpan:
    """An inclusive turn range in one file. ``end`` is INCLUSIVE (QMSum's rule)."""

    file_uuid: str
    start_turn: int
    end_turn: int

    def turn_indices(self) -> set[int]:
        """Every turn index in the span. Inclusive of both ends."""
        if self.end_turn < self.start_turn:
            return set()
        return set(range(self.start_turn, self.end_turn + 1))


@dataclass(frozen=True)
class RelevancePolicy:
    """Coverage -> graded relevance. Recorded in the results file verbatim."""

    high: float = 0.5
    low: float = 0.0
    binary: bool = False

    def grade(self, coverage: float) -> int:
        if coverage <= self.low:
            return 0
        if self.binary:
            return 1
        return 2 if coverage >= self.high else 1

    def as_dict(self) -> dict[str, float | bool | str]:
        return {
            "rule": "graded by gold word-share of the chunk",
            "high_threshold": self.high,
            "low_threshold": self.low,
            "binary": self.binary,
        }


def _overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


class TurnIndex:
    """One file's turns, bucketed by speaker and sorted by start time.

    A linear scan is O(chunks x turns) per file, which on the real corpus
    (120k chunks over 129k turns) dominates the whole run. Bucketing by speaker
    and binary-searching the start times makes it linear in the matches, with
    identical output — ``test_turn_index_agrees_with_a_linear_scan`` is the
    control that keeps the fast path honest.
    """

    def __init__(self, turns: list[TurnRow]) -> None:
        self._by_speaker: dict[str, list[TurnRow]] = {}
        for turn in turns:
            self._by_speaker.setdefault(turn.speaker, []).append(turn)
        self._starts: dict[str, list[float]] = {}
        self._max_duration: dict[str, float] = {}
        for speaker, rows in self._by_speaker.items():
            rows.sort(key=lambda row: (row.start, row.turn_index))
            self._starts[speaker] = [row.start for row in rows]
            self._max_duration[speaker] = max((row.end - row.start for row in rows), default=0.0)

    def overlapping(self, speaker: str, start: float, end: float) -> list[TurnRow]:
        """Turns by ``speaker`` whose interval intersects ``[start, end]``."""
        import bisect

        rows = self._by_speaker.get(speaker)
        if not rows:
            return []
        # Any overlapping turn starts no earlier than (start - longest turn).
        first = bisect.bisect_left(self._starts[speaker], start - self._max_duration[speaker])
        found: list[TurnRow] = []
        for row in rows[first:]:
            if row.start >= end:
                break
            if row.end > start:
                found.append(row)
        return found


def chunk_turn_weights(chunk: ChunkDoc, turns: list[TurnRow] | TurnIndex) -> dict[int, float]:
    """Which source turns this chunk is made of, and how much of each.

    Args:
        chunk: One indexed chunk.
        turns: Every turn of the same file (any order), or a prepared
            :class:`TurnIndex` over them.

    Returns:
        ``turn_index -> weight`` in words. Empty when the chunk matched nothing,
        which is itself a signal (a chunk whose speaker or times do not line up
        with the manifest).
    """
    index = turns if isinstance(turns, TurnIndex) else TurnIndex(turns)
    weights: dict[int, float] = {}
    for turn in index.overlapping(chunk.speaker, chunk.start_time, chunk.end_time):
        overlap = _overlap_seconds(turn.start, turn.end, chunk.start_time, chunk.end_time)
        if overlap <= 0.0:
            continue
        duration = max(turn.end - turn.start, 0.0)
        share = 1.0 if duration <= 0.0 else min(1.0, overlap / duration)
        weights[turn.turn_index] = turn.word_count * share
    return weights


def coverage(weights: dict[int, float], gold_turns: set[int]) -> float:
    """Gold share of a chunk, in words. 0.0 when the chunk matched no turn."""
    total = sum(weights.values())
    if total <= 0.0:
        return 0.0
    return sum(weight for index, weight in weights.items() if index in gold_turns) / total


class QrelsBuilder:
    """Builds chunk-level qrels for one indexed corpus.

    Chunk->turn weights are computed once per file and reused across every query
    that touches it: with 1,576 QMSum queries over 232 meetings the naive
    recomputation dominates the run.
    """

    def __init__(
        self,
        turns_by_file: dict[str, list[TurnRow]],
        chunks_by_file: dict[str, list[ChunkDoc]],
        policy: RelevancePolicy | None = None,
    ) -> None:
        self.turns_by_file = turns_by_file
        self.chunks_by_file = chunks_by_file
        self.policy = policy or RelevancePolicy()
        self._weights: dict[str, list[tuple[ChunkDoc, dict[int, float]]]] = {}

    def _file_weights(self, file_uuid: str) -> list[tuple[ChunkDoc, dict[int, float]]]:
        cached = self._weights.get(file_uuid)
        if cached is None:
            index = TurnIndex(self.turns_by_file.get(file_uuid, []))
            cached = [
                (chunk, chunk_turn_weights(chunk, index))
                for chunk in self.chunks_by_file.get(file_uuid, [])
            ]
            self._weights[file_uuid] = cached
        return cached

    def judgements(self, spans: list[GoldSpan]) -> dict[str, int]:
        """Chunk-level judgements for one query's gold spans.

        Spans in the same file are unioned before grading, so two adjacent
        ranges cannot each contribute a fractional coverage that neither reaches
        the threshold alone.
        """
        gold_by_file: dict[str, set[int]] = {}
        for span in spans:
            gold_by_file.setdefault(span.file_uuid, set()).update(span.turn_indices())

        judged: dict[str, int] = {}
        for file_uuid, gold_turns in gold_by_file.items():
            for chunk, weights in self._file_weights(file_uuid):
                grade = self.policy.grade(coverage(weights, gold_turns))
                if grade > 0:
                    judged[chunk.doc_id] = grade
        return judged
