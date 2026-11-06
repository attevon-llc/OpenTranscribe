"""Rendering, slice planning and the lock: the layout is the app's own chunker's, and it is locked."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.eval.summary_bench_support import load_bench
from tests.eval.summary_bench_support import synthetic_transcript as _transcript

NAS = Path("/mnt/nas/opentranscribe-benchmarks")
TURN_RE = re.compile(r"\n[A-Z_][A-Z0-9_]*:\s*\[\d+:\d+\]")  # the chunker's boundary pattern


@pytest.fixture(scope="module")
def bench() -> SimpleNamespace:
    return load_bench()


class TestRenderedFormat:
    def test_the_builders_turns_match_the_chunkers_boundary_pattern_past_99_minutes(self) -> None:
        text = _transcript(turns=8, offset_s=6100.0)  # [101:40] and on
        assert "[101:4" in text
        assert len(TURN_RE.findall(text)) == 8

    def test_a_three_digit_minute_stamp_is_read_as_seconds(self, bench: SimpleNamespace) -> None:
        assert bench.inputs.chunk_start_seconds("SPEAKER_00: [101:40] hi") == 6100
        assert bench.inputs.chunk_start_seconds("no stamp here") is None

    def test_named_labels_do_not_match_the_boundary_pattern(self) -> None:
        # The reason relabelling to SPEAKER_NN is load-bearing: these would send every boundary
        # to the sentence splitter, so the run would measure a path real summaries rarely take.
        assert not TURN_RE.search("\n\nProject Manager: [00:01] hello")
        assert not TURN_RE.search("\n\nGrad A: [00:01] hello")


class TestChunking:
    def test_slice_index_follows_the_stamps(self, bench: SimpleNamespace) -> None:
        chunks = ["SPEAKER_00: [00:10] a", "SPEAKER_00: [05:00] b", "SPEAKER_00: [101:40] c"]
        index = bench.inputs.slice_index_for
        assert [index(t, chunks) for t in (0.0, 10.0, 299.9, 300.0, 6099.0, 6100.0, 9999.0)] == [
            1,
            1,
            1,
            2,
            2,
            3,
            3,
        ]

    def test_located_per_slice_counts_only_located_items(self, bench: SimpleNamespace) -> None:
        chunks = ["SPEAKER_00: [00:10] a", "SPEAKER_00: [05:00] b"]
        counts = bench.inputs.located_per_slice([20.0, 301.0, None, 400.0], chunks)
        assert counts == {1: 1, 2: 2}

    def test_failing_slice_is_the_one_with_most_gold_ties_earlier_never_last(
        self, bench: SimpleNamespace
    ) -> None:
        pick = bench.inputs.failing_slice
        assert pick({1: 3, 2: 9, 3: 9, 4: 0}, 4, None) == 2  # tie goes to the earlier slice
        assert pick({1: 1, 2: 1, 3: 99}, 3, None) == 1  # the last slice is never eligible
        assert pick({1: 5, 2: 9, 3: 0}, 3, 2) == 1  # nor the crosstalk tail


class TestPlanSlices:
    def _render(self, bench: SimpleNamespace) -> Any:
        return bench.inputs.RenderedInput(_transcript(), {}, {})

    def test_picks_the_max_tokens_whose_tail_is_closest_to_386_chars(
        self, bench: SimpleNamespace
    ) -> None:
        render = self._render(bench)
        spec = bench.inputs.GATE_INPUTS[0]
        plan = bench.inputs.plan_slices(spec, render, [])
        chunks, _ = bench.inputs.chunk_transcript(render.transcript, plan.max_tokens)
        assert bench.inputs.TAIL_MIN_CHARS <= len(chunks[-1]) <= bench.inputs.TAIL_MAX_CHARS
        assert plan.tail_index == len(chunks) >= 3
        assert plan.chunk_sha256 == [bench.inputs.sha256(c) for c in chunks]
        # Oracle: no other qualifying max_tokens has a tail closer to the target.
        distances = []
        for max_tokens in bench.inputs.TAIL_SEARCH:
            other, _ = bench.inputs.chunk_transcript(render.transcript, max_tokens)
            if len(other) >= 3 and 200 <= len(other[-1]) <= 600:
                distances.append(abs(len(other[-1]) - bench.inputs.TAIL_TARGET_CHARS))
        assert distances
        assert abs(len(chunks[-1]) - bench.inputs.TAIL_TARGET_CHARS) == min(distances)

    def test_the_closest_tail_wins_even_when_a_smaller_max_tokens_also_qualifies(
        self, bench: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tails = {12_000: 590, 12_050: 390, 12_100: 250}  # chars; 12_050 is nearest to 386

        def fake_chunks(transcript: str, max_tokens: int) -> tuple[list[str], int]:
            size = tails.get(max_tokens)
            # Distinct bodies per max_tokens, or the planner's dedupe of repeated layouts hides them.
            body = [f"{max_tokens}-a" * 50, f"{max_tokens}-b" * 50]
            return ([*body, "t" * size] if size else [transcript]), 4000

        monkeypatch.setattr(bench.inputs, "chunk_transcript", fake_chunks)
        render = bench.inputs.RenderedInput("whole", {}, {})
        plan = bench.inputs.plan_slices(bench.inputs.GATE_INPUTS[0], render, [])
        assert plan.max_tokens == 12_050
        assert plan.chunk_chars[-1] == 390

    def test_refuses_when_every_candidate_tail_holds_a_gold_item(
        self, bench: SimpleNamespace
    ) -> None:
        render = self._render(bench)
        every_second = [float(t) for t in range(4000)]
        with pytest.raises(bench.config.HarnessError) as refusal:
            bench.inputs.plan_slices(bench.inputs.GATE_INPUTS[0], render, every_second)
        assert refusal.value.code == bench.config.ExitCode.PRECONDITION
        assert "nearest misses" in str(refusal.value)

    def test_refuses_a_transcript_too_short_to_ever_have_two_slices_and_a_tail(
        self, bench: SimpleNamespace
    ) -> None:
        short = bench.inputs.RenderedInput(_transcript(turns=20), {}, {})
        with pytest.raises(bench.config.HarnessError):
            bench.inputs.plan_slices(bench.inputs.GATE_INPUTS[0], short, [])

    def test_a_long_input_skips_a_three_slice_layout_for_the_first_four_slice_one(
        self, bench: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        counts = {16_000: 3, 16_050: 3, 16_100: 4, 16_150: 5}

        def fake_chunks(transcript: str, max_tokens: int) -> tuple[list[str], int]:
            return [f"{max_tokens}-{i}" for i in range(counts.get(max_tokens, 9))], 4000

        monkeypatch.setattr(bench.inputs, "chunk_transcript", fake_chunks)
        render = bench.inputs.RenderedInput("whole", {}, {})
        plan = bench.inputs.plan_slices(bench.inputs.GATE_INPUTS[2], render, [])
        assert (plan.max_tokens, len(plan.chunk_chars)) == (16_100, 4)

    def test_a_long_input_gets_four_or_five_slices_at_the_smallest_window_that_does(
        self, bench: SimpleNamespace
    ) -> None:
        render = bench.inputs.RenderedInput(_transcript(turns=2400), {}, {})
        plan = bench.inputs.plan_slices(bench.inputs.GATE_INPUTS[2], render, [])
        assert len(plan.chunk_chars) in (4, 5)
        assert plan.tail_index is None
        smaller = plan.max_tokens - 50
        if smaller >= bench.inputs.LONG_FLOOR_TOKENS:
            earlier, _ = bench.inputs.chunk_transcript(render.transcript, smaller)
            assert len(earlier) not in (4, 5)

    def test_chunking_leaves_the_app_loggers_levels_alone(self, bench: SimpleNamespace) -> None:
        # This module runs inside the backend test session: a level set here outlives the test
        # and hid llm_service's DEBUG records from a later caplog test on the same worker.
        names = ("app.services.llm_service", "app.services.llm_chunking")
        before = {name: logging.getLogger(name).level for name in names}
        chunks, _ = bench.inputs.chunk_transcript(_transcript(turns=50), 12_000)
        assert chunks
        assert {name: logging.getLogger(name).level for name in names} == before


class TestLock:
    def _entry(self, bench: SimpleNamespace, transcript: str) -> dict:
        chunks, response = bench.inputs.chunk_transcript(transcript, 12_000)
        return {
            "max_tokens": 12_000,
            "response_tokens": response,
            "transcript_sha256": bench.inputs.sha256(transcript),
            "chunks": [{"chars": len(c), "sha256": bench.inputs.sha256(c)} for c in chunks],
        }

    def test_an_unchanged_chunker_reproduces_the_lock(self, bench: SimpleNamespace) -> None:
        text = _transcript(turns=600)
        assert bench.inputs.verify_against_lock(self._entry(bench, text), text) == []

    def test_a_changed_chunk_hash_is_reported(self, bench: SimpleNamespace) -> None:
        text = _transcript(turns=600)
        entry = self._entry(bench, text)
        entry["chunks"][1]["sha256"] = "0" * 64
        assert bench.inputs.verify_against_lock(entry, text) == ["chunk 2 hash differs"]

    def test_a_changed_slice_count_is_reported(self, bench: SimpleNamespace) -> None:
        text = _transcript(turns=600)
        entry = self._entry(bench, text)
        entry["chunks"].pop()
        assert any("chunks" in p for p in bench.inputs.verify_against_lock(entry, text))

    def test_the_runner_refuses_to_run_against_a_stale_lock(
        self, bench: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = _transcript(turns=600)
        entry = self._entry(bench, text)
        entry["chunks"][0]["sha256"] = "f" * 64
        monkeypatch.setattr(
            bench.planner,
            "render_input",
            lambda spec, paths: bench.inputs.RenderedInput(text, {}, {}),
        )
        spec = bench.inputs.GATE_INPUTS[0]
        with pytest.raises(bench.config.HarnessError) as refusal:
            bench.planner.verify_lock({"inputs": {"I1": entry}}, (spec,), None)
        assert refusal.value.code == bench.config.ExitCode.PRECONDITION
        assert "no longer reproduces" in str(refusal.value)

    def test_lock_round_trips_and_a_missing_lock_is_a_precondition_failure(
        self, bench: SimpleNamespace, tmp_path: Path
    ) -> None:
        path = bench.planner.write_lock({"version": 1, "inputs": {}}, tmp_path)
        assert bench.planner.read_lock(tmp_path) == {"version": 1, "inputs": {}}
        assert path.name == "inputs.lock.json"
        with pytest.raises(bench.config.HarnessError) as refusal:
            bench.planner.read_lock(tmp_path / "missing")
        assert refusal.value.code == bench.config.ExitCode.PRECONDITION


@pytest.mark.skipif(not NAS.is_dir(), reason=f"benchmark corpora not mounted at {NAS}")
class TestRealCorpus:
    def test_the_rendered_tail_meeting_matches_the_chunker_and_ends_in_crosstalk(
        self, bench: SimpleNamespace
    ) -> None:
        paths = bench.inputs.CorpusPaths.under(NAS)
        render = bench.inputs.render_input(bench.inputs.GATE_INPUTS[0], paths)
        assert "{" not in render.transcript  # QMSum inline markers are stripped
        assert len(TURN_RE.findall(render.transcript)) > 400
        assert render.transcript.rstrip().endswith("Switching o")

    def test_a_participant_keeps_one_label_across_the_four_sessions_of_a_series(
        self, bench: SimpleNamespace
    ) -> None:
        paths = bench.inputs.CorpusPaths.under(NAS)
        render = bench.inputs.render_input(bench.inputs.GATE_INPUTS[2], paths)
        assert sorted(set(re.findall(r"SPEAKER_\d+", render.transcript))) == [
            "SPEAKER_00",
            "SPEAKER_01",
            "SPEAKER_02",
            "SPEAKER_03",
        ]
        assert render.offsets["ES2008a"] == 0.0
        assert render.offsets["ES2008b"] > 900  # session a ends, then the 30 s gap

    def test_the_gate_lock_reproduces_the_plans_shape(self, bench: SimpleNamespace) -> None:
        lock = bench.planner.build_lock(
            bench.inputs.GATE_INPUTS, bench.inputs.CorpusPaths.under(NAS)
        )
        for tail in ("I1", "I2"):
            entry = lock["inputs"][tail]
            assert 200 <= entry["chunks"][-1]["chars"] <= 600
            assert len(entry["chunks"]) >= 3
            assert entry["gold"]["located_per_slice"][str(len(entry["chunks"]))] == 0
        assert len(lock["inputs"]["I3"]["chunks"]) in (4, 5)
        assert lock["inputs"]["I4"]["failing_slice"] == lock["inputs"]["I4p"]["failing_slice"]
        assert lock["inputs"]["I3"]["failing_slice"] is None
        assert lock["inputs"]["I4"]["chunks"] == lock["inputs"]["I3"]["chunks"]
