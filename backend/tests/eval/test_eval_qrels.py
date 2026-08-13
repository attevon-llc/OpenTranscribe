"""Qrels-builder tests: gold turn ranges -> chunk-level judgements.

The mapping is the part of Stage 1 a reviewer can legitimately challenge, so
every rule it encodes has a hand-worked example here: the inclusive end, the
speaker restriction that makes overlapping speech harmless, the word-share
grading, and the fast path's agreement with a naive scan.

These tests need no OpenSearch and no metric engine — the fixture is the same
shape ``index_reader.fetch_chunks`` returns.
"""

from __future__ import annotations

import json

import pytest

from tests.eval.harness.qrels import ChunkDoc
from tests.eval.harness.qrels import GoldSpan
from tests.eval.harness.qrels import QrelsBuilder
from tests.eval.harness.qrels import RelevancePolicy
from tests.eval.harness.qrels import TurnIndex
from tests.eval.harness.qrels import TurnRow
from tests.eval.harness.qrels import chunk_turn_weights
from tests.eval.harness.qrels import coverage

FILE = "file-0001"


def _turns() -> list[TurnRow]:
    """Four turns; the last is long enough for the indexer to split it."""
    return [
        TurnRow(FILE, 0, "A", 0.0, 10.0, 10),
        TurnRow(FILE, 1, "B", 10.0, 20.0, 10),
        TurnRow(FILE, 2, "A", 20.0, 30.0, 10),
        TurnRow(FILE, 3, "A", 30.0, 40.0, 30),
    ]


def _chunks() -> list[ChunkDoc]:
    """What ``chunk_transcript_by_speaker_turns`` produces from those turns:
    turns 2 and 3 are consecutive same-speaker material and land in one chunk."""
    return [
        ChunkDoc(FILE, 0, "A", 0.0, 10.0),
        ChunkDoc(FILE, 1, "B", 10.0, 20.0),
        ChunkDoc(FILE, 2, "A", 20.0, 40.0),
    ]


def _builder(policy: RelevancePolicy | None = None) -> QrelsBuilder:
    return QrelsBuilder({FILE: _turns()}, {FILE: _chunks()}, policy)


def test_gold_span_end_is_inclusive():
    """QMSum's ``relevant_text_span`` and the synthetic ``gold_turns`` both mean
    ``[start, end]`` with END INCLUSIVE. Reading it as exclusive silently drops
    the last turn of every span — invisible in aggregate, wrong in every row."""
    assert GoldSpan(FILE, 0, 1).turn_indices() == {0, 1}
    assert GoldSpan(FILE, 5, 5).turn_indices() == {5}
    assert GoldSpan(FILE, 3, 2).turn_indices() == set()

    judged = _builder().judgements([GoldSpan(FILE, 0, 1)])
    assert judged == {f"{FILE}_0": 2, f"{FILE}_1": 2}, (
        "turn 1 was dropped — the end index is being treated as exclusive"
    )


def test_coverage_is_the_gold_word_share_of_the_chunk():
    """Chunk 2 is 40 words: 10 from turn 2 and 30 from turn 3."""
    weights = chunk_turn_weights(_chunks()[2], _turns())
    assert weights == {2: 10.0, 3: 30.0}
    assert coverage(weights, {2}) == pytest.approx(0.25)
    assert coverage(weights, {3}) == pytest.approx(0.75)
    assert coverage(weights, {2, 3}) == pytest.approx(1.0)
    assert coverage(weights, {9}) == 0.0


def test_overlap_maps_to_graded_relevance_at_the_documented_thresholds():
    graded = _builder().judgements([GoldSpan(FILE, 2, 2)])  # coverage 0.25
    assert graded == {f"{FILE}_2": 1}, "a quarter-covered chunk should be marginal, not full"

    graded = _builder().judgements([GoldSpan(FILE, 3, 3)])  # coverage 0.75
    assert graded == {f"{FILE}_2": 2}


def test_relevance_thresholds_are_parameters_not_constants():
    """The mapping is a judgement call, so it must be tunable and recorded."""
    lenient = _builder(RelevancePolicy(high=0.2)).judgements([GoldSpan(FILE, 2, 2)])
    assert lenient == {f"{FILE}_2": 2}

    binary = _builder(RelevancePolicy(binary=True)).judgements([GoldSpan(FILE, 3, 3)])
    assert binary == {f"{FILE}_2": 1}

    strict = _builder(RelevancePolicy(low=0.3)).judgements([GoldSpan(FILE, 2, 2)])
    assert strict == {}, "coverage 0.25 should fall below a 0.3 floor"

    policy = RelevancePolicy(high=0.4, low=0.1, binary=False)
    assert policy.as_dict()["high_threshold"] == 0.4
    assert policy.as_dict()["low_threshold"] == 0.1


def test_a_chunk_only_matches_its_own_speaker():
    """Overlapping speech is why the speaker restriction exists.

    Speaker B's turn overlaps chunk 0 in time, but a chunk contains only its own
    speaker's segments. Without the restriction B's words would be attributed to
    A's chunk and a gold span covering B would mark A's chunk relevant.
    """
    turns = _turns()
    turns[1] = TurnRow(FILE, 1, "B", 9.0, 21.0, 10)  # B talks over A
    builder = QrelsBuilder({FILE: turns}, {FILE: _chunks()})

    assert chunk_turn_weights(_chunks()[0], turns) == {0: 10.0}
    assert builder.judgements([GoldSpan(FILE, 1, 1)]) == {f"{FILE}_1": 2}


def test_a_split_long_turn_shares_its_words_by_time():
    """A monologue split with a sliding window gives each sub-chunk a share."""
    turns = [TurnRow(FILE, 0, "A", 0.0, 40.0, 400)]
    first = chunk_turn_weights(ChunkDoc(FILE, 0, "A", 0.0, 22.0), turns)
    second = chunk_turn_weights(ChunkDoc(FILE, 1, "A", 18.0, 40.0), turns)

    assert first == {0: pytest.approx(220.0)}
    assert second == {0: pytest.approx(220.0)}
    # Both sub-chunks are fully gold when the turn is gold.
    assert coverage(first, {0}) == pytest.approx(1.0)


def test_spans_in_one_file_are_unioned_before_grading():
    """Two adjacent half-covering ranges must not each fail the threshold alone."""
    builder = _builder()
    split = builder.judgements([GoldSpan(FILE, 2, 2), GoldSpan(FILE, 3, 3)])
    whole = builder.judgements([GoldSpan(FILE, 2, 3)])
    assert split == whole == {f"{FILE}_2": 2}


def test_turn_index_agrees_with_a_linear_scan():
    """The fast path is an optimisation, so it needs a genuinely different control."""
    import random

    # Seeded, for reproducible test data — not a security context.
    rng = random.Random(20260812)  # noqa: S311
    turns = []
    clock = 0.0
    for index in range(400):
        speaker = f"S{index % 5}"
        start = clock + rng.uniform(-1.0, 2.0)
        end = start + rng.uniform(0.5, 30.0)
        turns.append(TurnRow(FILE, index, speaker, start, end, rng.randint(1, 60)))
        clock = max(clock, start + rng.uniform(0.5, 8.0))

    index_obj = TurnIndex(turns)
    for _ in range(200):
        speaker = f"S{rng.randrange(5)}"
        start = rng.uniform(0.0, clock)
        end = start + rng.uniform(0.1, 60.0)
        fast = sorted(row.turn_index for row in index_obj.overlapping(speaker, start, end))
        naive = sorted(
            row.turn_index
            for row in turns
            if row.speaker == speaker and row.end > start and row.start < end
        )
        assert fast == naive


def _write_manifest(directory, file_uuid: str, meeting_id: str, extra: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "corpus": {
                    "key": "unit",
                    "name": "unit",
                    "version": "0",
                    "license_tier": "A",
                    "root": str(directory),
                }
            }
        ),
        encoding="utf-8",
    )
    (directory / "files.jsonl").write_text(
        json.dumps({"meeting_id": meeting_id, "file_uuid": file_uuid, "extra": extra}) + "\n",
        encoding="utf-8",
    )
    (directory / "turns.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "file_uuid": file_uuid,
                    "turn_index": turn.turn_index,
                    "speaker": turn.speaker,
                    "start": turn.start,
                    "end": turn.end,
                    "word_count": turn.word_count,
                }
            )
            + "\n"
            for turn in _turns()
        ),
        encoding="utf-8",
    )


def test_one_adapter_serves_qmsum_and_the_synthetic_tier(tmp_path):
    """QMSum ships decimal STRINGS, the synthetic tier ships ints — same convention.

    Both loaders must produce the same :class:`GoldSpan` and therefore the same
    judgements. A second overlap rule for the second corpus would be the signal
    that this has been mis-factored.
    """
    from tests.eval.harness import corpora

    # --- QMSum shape -------------------------------------------------------
    qmsum_dir = tmp_path / "qmsum"
    _write_manifest(qmsum_dir, FILE, "M1", {"domain": "Academic"})
    data = qmsum_dir / "data" / "Academic" / "all"
    data.mkdir(parents=True)
    (data / "M1.json").write_text(
        json.dumps(
            {
                "general_query_list": [{"query": "Summarize the whole meeting", "answer": "x"}],
                "specific_query_list": [
                    {"query": "What did A decide?", "relevant_text_span": [["2", "3"]]},
                    {"query": "Summarize the discussion", "relevant_text_span": [["0", "1"]]},
                    {"query": "unjudgeable", "relevant_text_span": []},
                ],
            }
        ),
        encoding="utf-8",
    )
    qmsum_corpus = corpora.load_manifest(qmsum_dir)
    qmsum_queries = corpora.load_qmsum_queries(qmsum_corpus)

    assert [q.query_class for q in qmsum_queries] == [corpora.LOOKUP, corpora.SUMMARIZE]
    assert qmsum_queries[0].spans == (GoldSpan(FILE, 2, 3),)
    assert qmsum_queries[0].license_tier == "A"

    # --- Synthetic shape ---------------------------------------------------
    synth_dir = tmp_path / "synthetic"
    _write_manifest(tmp_path / "synth-manifest", FILE, "T000-S0-0000", {})
    (synth_dir / "meetings").mkdir(parents=True)
    (synth_dir / "meetings" / "part-0000.jsonl").write_text(
        json.dumps({"meeting_key": "T000-S0-0000", "file_uuid": "corpus-side-uuid"}) + "\n",
        encoding="utf-8",
    )
    (synth_dir / "queries.jsonl").write_text(
        json.dumps(
            {
                "query_id": "lk-00000",
                "text": "What did A decide?",
                "query_class": "lookup",
                "scored_on": "retrieval",
                "gold_turns": {"corpus-side-uuid": [[2, 3]]},
            }
        )
        + "\n"
        # A query whose gold set is not fully on the stack must be dropped, not
        # scored against a silently shrunken recall denominator.
        + json.dumps(
            {
                "query_id": "lk-00001",
                "text": "elsewhere",
                "query_class": "lookup",
                "gold_turns": {"not-injected": [[0, 0]]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    synth_corpus = corpora.load_manifest(tmp_path / "synth-manifest")
    synth_queries = corpora.load_synthetic_queries(synth_corpus, synth_dir)

    assert [q.query_id for q in synth_queries] == ["synthetic:lk-00000"]
    assert synth_queries[0].spans == qmsum_queries[0].spans

    builder = _builder()
    assert builder.judgements(list(synth_queries[0].spans)) == builder.judgements(
        list(qmsum_queries[0].spans)
    )
