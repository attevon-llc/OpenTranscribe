"""The synthetic-tier adapter: format, selection, and the gold-span round trip.

Three claims are worth a test here, and each one has already had a way of being
silently wrong:

1. **``meeting_id`` is the generator's ``meeting_key``.** That string is the only
   join between the corpus's own ``file_uuid`` and the one injection derives.
   Key the adapter on anything else and ``load_synthetic_queries`` resolves
   nothing — which surfaces as *zero scoreable queries*, not as an error.
2. **A budget must preserve gold-set closure.** Aggregation markers are planted
   with ``rng.sample(org.sessions, k)`` over the whole corpus, so a first-N-by-key
   subset leaves nearly every aggregation query with a partial gold set, and the
   harness drops those. Every closure test below carries the **naive-subset
   negative control** so it cannot pass under the bug it exists to catch.
3. **Generator timestamps are not measurements.** They are kept (the pacing is
   deliberate) but recorded ``synthetic``, with word timings NULL.

The gold-span round trip runs the corpus through the **production** chunker
(``chunk_transcript_by_speaker_turns``), not a hand-rolled stand-in: the whole
point of the qrels mapping is that it lands on the chunks the real indexer
produces. What it cannot cover is OpenSearch actually storing them — that is
``tests/integration/test_corpus_injection_synthetic_e2e.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fixtures.synthetic_corpus import MEETING_KEYS
from fixtures.synthetic_corpus import QUERIES
from fixtures.synthetic_corpus import gold_meeting_keys
from fixtures.synthetic_corpus import meeting as fixture_meeting
from fixtures.synthetic_corpus import turn as fixture_turn
from fixtures.synthetic_corpus import write_fixture_corpus

from app.scripts.corpus_injection import ids
from app.scripts.corpus_injection import rows as rowbuild
from app.scripts.corpus_injection.adapters import build_adapter
from app.scripts.corpus_injection.adapters.synthetic import DEFAULT_MEETING_BUDGET
from app.scripts.corpus_injection.adapters.synthetic import SyntheticAdapter
from app.scripts.corpus_injection.adapters.synthetic import SyntheticCorpusError
from app.scripts.corpus_injection.adapters.synthetic import locate_corpus
from app.scripts.corpus_injection.adapters.synthetic import select_gold_closure
from app.scripts.corpus_injection.model import TIMING_SYNTHETIC
from app.scripts.corpus_injection.model import InjectionRecord
from app.scripts.corpus_injection.timings import resolve_timings

NAS_ROOT = Path(os.environ.get("RAG_EVAL_DATA_DIR", "/mnt/nas/opentranscribe-benchmarks"))
REAL_CORPUS = NAS_ROOT / "synthetic" / "otsynth-core-v1"


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    # Coerced because the fixtures package is not on mypy's typed path, so the
    # helper's own `-> Path` degrades to Any across the import boundary.
    return Path(write_fixture_corpus(tmp_path / "synthetic"))


# --------------------------------------------------------------- the format


class TestFormat:
    def test_meeting_ids_are_the_generator_meeting_keys(self, corpus_root: Path):
        """The join the harness makes is on ``meeting_key`` — nothing else."""
        adapter = SyntheticAdapter(corpus_root, meetings=0)
        assert adapter.meeting_ids() == sorted(MEETING_KEYS)

    def test_turns_are_read_from_content_with_index_and_generator_times(self, corpus_root: Path):
        doc = SyntheticAdapter(corpus_root, meetings=0).load("T000-S0-0000")

        assert [turn.turn_index for turn in doc.turns] == list(range(8))
        assert doc.turns[0].text.startswith("T000-S0-0000 turn 0:")
        assert doc.turns[0].speaker == "Ada Vance"
        assert (doc.turns[1].start, doc.turns[1].end) == (5.0, 9.0)
        assert doc.meeting_id == "T000-S0-0000"
        assert doc.title == "T000 — planning sync (T000-S0-0000)"

    def test_the_corpus_side_uuid_is_carried_but_is_not_the_injected_one(self, corpus_root: Path):
        """Two different uuids for one meeting, joined only by ``meeting_key``."""
        doc = SyntheticAdapter(corpus_root, meetings=0).load("T000-S0-0000")
        injected = str(ids.file_uuid("synthetic", "T000-S0-0000"))

        assert doc.extra["corpus_file_uuid"] == "corpusuuid-T000-S0-0000"
        assert doc.extra["corpus_file_uuid"] != injected

    def test_generator_times_are_kept_but_never_promoted_to_real(self, corpus_root: Path):
        doc = SyntheticAdapter(corpus_root, meetings=0).load("T000-S0-0000")
        resolve_timings(doc)

        assert doc.timing.source == TIMING_SYNTHETIC
        assert doc.timing.params == {"generator": "corpus_supplied_v1"}
        assert doc.turns[1].start == 5.0  # the generator's pacing, not regenerated
        assert all(turn.words is None for turn in doc.turns)

    def test_synthetic_meetings_write_no_word_timings_into_segment_rows(self, corpus_root: Path):
        """Layer 3 of the provenance defence: a word-timing metric gets no rows."""
        doc = SyntheticAdapter(corpus_root, meetings=0).load("T000-S0-0000")
        resolve_timings(doc)
        segment_rows, _, _ = rowbuild.build_segment_rows(doc, seed="", media_file_id=1)

        assert len(segment_rows) == 8
        assert [row["words"] for row in segment_rows] == [None] * 8

    def test_a_rung_directory_is_found_one_level_down(self, tmp_path: Path):
        """``$RAG_EVAL_DATA_DIR/synthetic`` holds rungs, not the corpus itself."""
        parent = tmp_path / "synthetic"
        corpus = write_fixture_corpus(parent)
        assert locate_corpus(parent) == corpus
        assert locate_corpus(corpus) == corpus

    def test_two_rungs_in_one_directory_are_refused_by_name(self, tmp_path: Path):
        parent = tmp_path / "synthetic"
        write_fixture_corpus(parent)
        second = parent / "otsynth-n5000"
        (second / "meetings").mkdir(parents=True)
        (second / "config.json").write_text("{}", encoding="utf-8")
        (second / "queries.jsonl").write_text("", encoding="utf-8")

        with pytest.raises(SyntheticCorpusError, match="otsynth-n5000"):
            locate_corpus(parent)

    def test_an_empty_directory_names_the_generator_command(self, tmp_path: Path):
        empty = tmp_path / "synthetic"
        empty.mkdir()
        with pytest.raises(SyntheticCorpusError, match="tests.eval.synthetic generate"):
            locate_corpus(empty)


# ------------------------------------------------------------- the selection


class TestGoldClosureSelection:
    def test_a_budget_keeps_whole_gold_sets_where_a_naive_subset_does_not(self, corpus_root: Path):
        """The negative control is the point: first-N-by-key breaks ``ag-00000``.

        Its gold spans the first and last meetings in sort order, which is the
        shape ``rng.sample(org.sessions, k)`` produces on the real corpus.
        """
        gold = set(gold_meeting_keys("ag-00000"))
        selection = SyntheticAdapter(corpus_root, meetings=4).selection

        naive = set(sorted(MEETING_KEYS)[:4])
        assert not gold <= naive, "control is broken: the naive subset already closes ag-00000"
        assert gold <= set(selection)

    def test_every_selected_query_has_its_whole_gold_set(self, corpus_root: Path):
        selection = set(SyntheticAdapter(corpus_root, meetings=5).selection)
        pulled_in = {
            query_id
            for ids_ in SyntheticAdapter(corpus_root, meetings=5).selection.values()
            for query_id in ids_
        }

        incomplete = sorted(
            query_id for query_id in pulled_in if not set(gold_meeting_keys(query_id)) <= selection
        )
        assert pulled_in, "no query drove the selection — the budget bought nothing"
        assert incomplete == []

    def test_related_files_are_selected_alongside_gold(self, corpus_root: Path):
        """R7 is a *filtered* count; without the out-of-month mentions it is not."""
        # Budget 7, not 8: at 8 the whole corpus is taken and no query drives
        # the selection, so the assertion below would pass vacuously.
        selection = SyntheticAdapter(corpus_root, meetings=7).selection
        assert "corpusuuid-T001-S0-0000" not in QUERIES[2]["gold_files"]
        assert "T001-S0-0000" in selection
        assert "ag-00001" in selection["T001-S0-0000"]

    def test_the_budget_is_never_exceeded(self, corpus_root: Path):
        budgets = (1, 2, 3, 5, 7)
        sizes = [len(SyntheticAdapter(corpus_root, meetings=n).meeting_ids()) for n in budgets]
        over = [(n, size) for n, size in zip(budgets, sizes, strict=True) if size > n]

        assert over == []
        # A budget that bought nothing would also satisfy "never exceeded".
        assert sizes[-1] >= 5

    def test_a_zero_budget_injects_the_whole_corpus(self, corpus_root: Path):
        assert SyntheticAdapter(corpus_root, meetings=0).meeting_ids() == sorted(MEETING_KEYS)

    def test_only_the_named_classes_spend_the_budget(self, corpus_root: Path):
        """``lookup`` is measurable on QMSum; it must not consume this budget."""
        selection = SyntheticAdapter(corpus_root, meetings=2, select_for=("multi_file",)).selection
        drivers = sorted({query_id for ids_ in selection.values() for query_id in ids_})
        assert drivers == ["mf-00000"]

    def test_a_query_naming_an_absent_file_is_never_selected_for(self):
        """It can never be closed, so spending meetings on it is pure waste."""
        chosen = select_gold_closure(
            [
                {
                    "query_id": "ag-99999",
                    "query_class": "aggregation",
                    "gold_files": ["present", "absent"],
                }
            ],
            {"present": "T000-S0-0000"},
            budget=8,
            classes=("aggregation",),
        )
        assert chosen == {}


class TestDeterminism:
    def test_the_same_corpus_selects_the_same_meetings_every_time(self, corpus_root: Path):
        first = SyntheticAdapter(corpus_root, meetings=5).selection
        second = SyntheticAdapter(corpus_root, meetings=5).selection
        assert first == second

    def test_the_derived_file_uuid_is_pinned(self, corpus_root: Path):
        """A changed uuid silently orphans every previously injected row."""
        assert SyntheticAdapter(corpus_root, meetings=0).meeting_ids()[0] == "T000-S0-0000"
        assert str(ids.file_uuid("synthetic", "T000-S0-0000")) == (
            "647c931e-d041-5d2b-b369-5a0a027b5f6d"
        )

    def test_content_and_segment_rows_are_byte_stable_across_loads(self, corpus_root: Path):
        adapter = SyntheticAdapter(corpus_root, meetings=0)
        built = []
        for _ in range(2):
            doc = adapter.load("T001-S0-0001")
            resolve_timings(doc)
            segment_rows, turn_rows, nudged = rowbuild.build_segment_rows(
                doc, seed="", media_file_id=7
            )
            built.append((ids.content_sha256(doc.turns), segment_rows, turn_rows, nudged))

        assert built[0][0] == built[1][0]
        assert built[0][1] == built[1][1]
        assert built[0][2] == built[1][2]

    def test_the_selection_is_recorded_in_the_corpus_version(self, corpus_root: Path):
        """Two budgets are two index states and must not share a version string."""
        small = SyntheticAdapter(corpus_root, meetings=4).describe().version
        whole = SyntheticAdapter(corpus_root, meetings=0).describe().version

        assert small == (
            "otsynth-fixture-v1@1.0.0/seed=20260812"
            "/select=gold-closure[multi_file+aggregation]/meetings=4of8"
        )
        assert whole == "otsynth-fixture-v1@1.0.0/seed=20260812/select=all/meetings=8of8"

    def test_the_selecting_queries_are_recorded_on_each_meeting(self, corpus_root: Path):
        doc = SyntheticAdapter(corpus_root, meetings=4).load("T000-S0-0000")
        assert doc.extra["selected_for"] == ["ag-00000", "mf-00000"]


class TestDuplicateSpans:
    def test_identical_turns_at_one_instant_are_separated(self, tmp_path: Path):
        """``uq_transcript_segment_content`` is DDL-only; the ORM cannot warn.

        The real corpus does not currently produce this collision (measured: 0
        over all 2,000 meetings), but a turn-per-segment corpus is exactly the
        shape that can, so the separation has to be exercised deliberately.
        """
        colliding = fixture_meeting("T000-S0-0000", n_turns=2)
        colliding["turns"][1] = fixture_turn(
            1, "Ada Vance", colliding["turns"][0]["content"], 0.0, 4.0
        )
        corpus = write_fixture_corpus(tmp_path / "synthetic", [colliding])

        doc = SyntheticAdapter(corpus, meetings=0).load("T000-S0-0000")
        resolve_timings(doc)
        segment_rows, turn_rows, nudged = rowbuild.build_segment_rows(doc, "", media_file_id=1)

        triples = [(r["start_time"], r["end_time"], r["text"]) for r in segment_rows]
        assert nudged == 1
        assert len(set(triples)) == len(triples)
        # The onset a citation points at is never moved; only the end walks.
        assert [r["start_time"] for r in segment_rows] == [0.0, 0.0]
        assert turn_rows[1]["end"] == segment_rows[1]["end_time"]


# --------------------------------------------------- gold spans -> chunk qrels


class TestGoldSpansReachChunkJudgements:
    """The whole chain, minus OpenSearch: corpus -> rows -> chunks -> qrels."""

    def _manifest(self, tmp_path: Path, adapter: SyntheticAdapter) -> Path:
        from app.scripts.corpus_injection import manifest as manifest_mod

        records: list[InjectionRecord] = []
        turns_by_file: dict[str, list[dict[str, Any]]] = {}
        for position, meeting_id in enumerate(adapter.meeting_ids(), start=1):
            doc = adapter.load(meeting_id)
            resolve_timings(doc)
            _, turn_rows, _ = rowbuild.build_segment_rows(doc, "", media_file_id=position)
            file_uuid = str(ids.file_uuid("synthetic", meeting_id))
            records.append(
                InjectionRecord(
                    corpus="synthetic",
                    meeting_id=meeting_id,
                    file_uuid=file_uuid,
                    media_file_id=position,
                    title=doc.title,
                    turn_count=len(doc.turns),
                    segment_count=len(turn_rows),
                    word_count=doc.word_count,
                    speaker_count=len(doc.speakers),
                    duration_seconds=doc.duration,
                    timing_source=doc.timing.source,
                    timing_reference=None,
                    timing_aligned_turns=0,
                    timing_alignment_rate=0.0,
                    synthetic_timing_params=doc.timing.params,
                    content_sha256=ids.content_sha256(doc.turns),
                    language="en",
                    action="created",
                    extra=dict(doc.extra),
                )
            )
            turns_by_file[file_uuid] = turn_rows

        out = tmp_path / "manifest"
        manifest_mod.write(
            out,
            adapter.describe(),
            records,
            turns_by_file,
            seed="",
            tool_version="pytest",
            target={"postgres": "none", "opensearch": "none"},
            dispatch_mode="none",
        )
        return out

    def _chunks(self, adapter: SyntheticAdapter):
        """Chunk every meeting with the PRODUCTION chunker, keyed by file uuid."""
        from app.services.search.chunking_service import chunk_transcript_by_speaker_turns
        from tests.eval.harness.qrels import ChunkDoc

        by_file: dict[str, list[ChunkDoc]] = {}
        for position, meeting_id in enumerate(adapter.meeting_ids(), start=1):
            doc = adapter.load(meeting_id)
            resolve_timings(doc)
            segment_rows, _, _ = rowbuild.build_segment_rows(doc, "", media_file_id=position)
            # build_segment_rows emits in time order and drops empty turns; the
            # speaker labels have to be taken in the same order or every chunk
            # would be attributed to the wrong voice.
            ordered = [
                turn
                for turn in sorted(
                    doc.turns, key=lambda t: (t.start or 0.0, t.end or 0.0, t.turn_index)
                )
                if turn.text
            ]
            file_uuid = str(ids.file_uuid("synthetic", meeting_id))
            documents = chunk_transcript_by_speaker_turns(
                segments=[
                    {
                        "start": row["start_time"],
                        "end": row["end_time"],
                        "text": row["text"],
                        "speaker": turn.speaker,
                    }
                    for row, turn in zip(segment_rows, ordered, strict=True)
                ],
                file_uuid=file_uuid,
                file_id=position,
                user_id=1,
                title=doc.title,
                speakers=doc.speakers,
                tags=[],
                upload_time="2026-08-12T00:00:00Z",
            )
            by_file[file_uuid] = [
                ChunkDoc(
                    file_uuid=file_uuid,
                    chunk_index=int(document["chunk_index"]),
                    speaker=str(document.get("speaker") or ""),
                    start_time=float(document["start_time"]),
                    end_time=float(document["end_time"]),
                )
                for document in documents
            ]
        return by_file

    def test_a_gold_turn_range_becomes_a_graded_chunk_judgement(self, corpus_root, tmp_path):
        from tests.eval.harness import corpora
        from tests.eval.harness.qrels import QrelsBuilder

        adapter = SyntheticAdapter(corpus_root, meetings=0)
        manifest_dir = self._manifest(tmp_path, adapter)
        injected = corpora.load_manifest(manifest_dir)
        queries = corpora.load_synthetic_queries(injected, corpus_root)
        builder = QrelsBuilder(corpora.load_turns(manifest_dir), self._chunks(adapter))

        by_id = {query.query_id: query for query in queries}
        assert sorted(by_id) == [
            "synthetic:ag-00000",
            "synthetic:ag-00001",
            "synthetic:lk-00000",
            "synthetic:mf-00000",
        ]

        multi_file = by_id["synthetic:mf-00000"]
        judged = builder.judgements(list(multi_file.spans))
        assert multi_file.query_class == "multi_file"
        assert judged, "a gold turn range produced no chunk judgement at all"
        assert max(judged.values()) == 2
        # Both gold files must be judged: a multi-file query judged on one file
        # scores a system that found half the answer as if it found all of it.
        assert len({doc_id.rsplit("_", 1)[0] for doc_id in judged}) == 2

    def test_gold_spans_land_on_the_uuids_injection_derived(self, corpus_root, tmp_path):
        """The remap is what a wrong ``meeting_id`` would break, silently."""
        from tests.eval.harness import corpora

        adapter = SyntheticAdapter(corpus_root, meetings=0)
        injected = corpora.load_manifest(self._manifest(tmp_path, adapter))
        queries = corpora.load_synthetic_queries(injected, corpus_root)

        span_uuids = sorted({span.file_uuid for query in queries for span in query.spans})
        # Only the RETRIEVAL-scored queries contribute ranked gold spans. An
        # aggregation query is scored on its answer, so `_answer_query` gives it
        # `gold_answer` and no spans — its ranked numbers are context, not its
        # score, and mixing them into a recall denominator would let a class the
        # harness cannot rank inflate one it can.
        expected = sorted(
            str(ids.file_uuid("synthetic", key))
            # mf-00000's two gold files, plus lk-00000's one.
            for key in ("T000-S0-0000", "T000-S0-0001", "T001-S0-0001")
        )
        assert span_uuids == expected
        assert not any(uuid_.startswith("corpusuuid-") for uuid_ in span_uuids)

        # Pin the other half of that split, so a regression that silently routed
        # aggregation back into the ranked set fails here rather than showing up
        # as a quietly larger recall denominator.
        by_id = {query.query_id: query for query in queries}
        for answer_scored in ("synthetic:ag-00000", "synthetic:ag-00001"):
            assert by_id[answer_scored].gold_answer is not None
            assert not by_id[answer_scored].spans

    def test_a_partly_injected_gold_set_drops_the_query_rather_than_shrinking_recall(
        self, corpus_root, tmp_path
    ):
        from tests.eval.harness import corpora

        # Budget 2 closes ag-00001 (two files) and cannot close ag-00000 (three).
        adapter = SyntheticAdapter(corpus_root, meetings=2, select_for=("aggregation",))
        injected = corpora.load_manifest(self._manifest(tmp_path, adapter))
        queries = corpora.load_synthetic_queries(injected, corpus_root)

        assert [query.query_id for query in queries] == ["synthetic:ag-00001"]


# ---------------------------------------------------------- the real corpus


@pytest.mark.skipif(
    not (REAL_CORPUS / "queries.jsonl").is_file(),
    reason=(
        f"{REAL_CORPUS} is absent. The fixtures above prove the logic; this class proves it "
        "survives 2,000 real meetings. Generate with: "
        "python3 -m tests.eval.synthetic generate --out <dir> --meetings 2000"
    ),
)
class TestAgainstTheRealCorpus:
    """A fixture cannot show that 233 MB of shards parse, or what a budget buys."""

    @pytest.fixture(scope="class")
    def adapter(self) -> SyntheticAdapter:
        return SyntheticAdapter(NAS_ROOT / "synthetic")

    def test_the_default_budget_selects_that_many_meetings_out_of_two_thousand(self, adapter):
        assert len(adapter.meeting_ids()) == DEFAULT_MEETING_BUDGET
        assert adapter.describe().version.endswith(f"/meetings={DEFAULT_MEETING_BUDGET}of2000")

    def test_the_registry_builds_it_with_the_default_budget(self):
        built = build_adapter("synthetic", NAS_ROOT)
        assert isinstance(built, SyntheticAdapter)
        assert built.root == REAL_CORPUS
        assert len(built.meeting_ids()) == DEFAULT_MEETING_BUDGET

    def test_every_gold_set_the_budget_bought_is_complete(self, adapter):
        selected = set(adapter.meeting_ids())
        by_id = {str(q["query_id"]): q for q in adapter.queries}
        drivers = sorted({query_id for ids_ in adapter.selection.values() for query_id in ids_})

        incomplete = [
            query_id
            for query_id in drivers
            if not {
                adapter.uuid_to_key[u]
                for u in by_id[query_id]["gold_files"]
                if u in adapter.uuid_to_key
            }
            <= selected
        ]
        assert len(drivers) >= 30, f"the budget closed only {len(drivers)} queries"
        assert incomplete == []

    def test_a_first_n_by_key_subset_would_close_almost_no_aggregation_query(self, adapter):
        """The negative control at real scale — the reason selection exists."""
        naive = set(sorted(adapter.shards)[:DEFAULT_MEETING_BUDGET])
        aggregation = [q for q in adapter.queries if q["query_class"] == "aggregation"]
        closed = [
            q
            for q in aggregation
            if {adapter.uuid_to_key[u] for u in q["gold_files"] if u in adapter.uuid_to_key}
            <= naive
        ]
        by_closure = {
            query_id
            for ids_ in adapter.selection.values()
            for query_id in ids_
            if query_id.startswith("ag-")
        }
        assert len(aggregation) == 166
        # The comparison is the finding: the same 200-meeting budget buys an
        # order of magnitude more aggregation queries when it is spent on gold
        # closure instead of on whichever meetings sort first.
        assert len(closed) < 10, f"naive subset closed {len(closed)} aggregation queries"
        assert len(by_closure) > 4 * max(1, len(closed))

    def test_a_real_meeting_parses_with_the_turn_count_the_generator_recorded(self, adapter):
        meeting_id = adapter.meeting_ids()[0]
        doc = adapter.load(meeting_id)
        raw = adapter.raw_record(meeting_id)

        assert len(doc.turns) == raw["turn_count"]
        assert doc.word_count == raw["word_count"]
        assert [turn.turn_index for turn in doc.turns] == list(range(len(doc.turns)))

    def test_real_meetings_produce_segment_spans_the_unique_index_accepts(self, adapter):
        """``UNIQUE(media_file_id, start_time, end_time, md5(text))``, in effect."""
        collisions = []
        checked = 0
        for meeting_id in adapter.meeting_ids()[:10]:
            doc = adapter.load(meeting_id)
            resolve_timings(doc)
            segment_rows, _, _ = rowbuild.build_segment_rows(doc, "", media_file_id=1)
            triples = [(r["start_time"], r["end_time"], r["text"]) for r in segment_rows]
            checked += len(triples)
            if len(set(triples)) != len(triples):
                collisions.append(meeting_id)
        assert checked > 1000, "sampled too few segments to say anything"
        assert collisions == []
