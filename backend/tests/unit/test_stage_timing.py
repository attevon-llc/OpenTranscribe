"""Structured per-stage pipeline timing (#1134): ``app/core/stage_timing.py`` and its call sites.

What is pinned:

* the recorder: exactly one ``TIMING: stage=...`` line and one histogram/counter observation
  per stage run, on success, on a raised exception and on an explicit ``run.fail()``;
* bounded cardinality: whatever names are recorded, the only label keys are ``stage`` and
  ``outcome`` and the only values are the fixed :data:`STAGES` (+ ``other``) and two outcomes;
* the metrics reach a worker's multiprocess ``/metrics`` exposition;
* every stage in :data:`STAGES` is recorded somewhere in the pipeline, every call site names a
  known stage, and the call sites that can fail without raising, or raise, still record once.
"""

from __future__ import annotations

import ast
import contextlib
import logging
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.core import stage_timing
from app.core.stage_timing import OUTCOMES
from app.core.stage_timing import STAGES

APP_DIR = Path(__file__).resolve().parents[2] / "app"
_LINE = re.compile(
    r"^TIMING: stage=(?P<stage>\S+) outcome=(?P<outcome>\S+) seconds=(?P<seconds>\d+\.\d{3}) "
    r"task_id=(?P<task_id>\S+) file_id=(?P<file_id>\S+)$"
)


def _count(stage: str, outcome: str) -> float:
    value = stage_timing._get_collectors().registry.get_sample_value(
        "pipeline_stage_total", {"stage": stage, "outcome": outcome}
    )
    return value or 0.0


def _observations(stage: str) -> float:
    value = stage_timing._get_collectors().registry.get_sample_value(
        "pipeline_stage_duration_seconds_count", {"stage": stage}
    )
    return value or 0.0


def _timing_lines(caplog) -> list[re.Match]:
    return [
        m
        for r in caplog.records
        if r.name == stage_timing.logger.name and (m := _LINE.match(r.getMessage()))
    ]


@pytest.fixture
def capture(caplog):
    caplog.set_level(logging.INFO, logger=stage_timing.logger.name)
    return caplog


class TestRecorder:
    def test_success_records_once(self, capture):
        before = (_count("asr", "success"), _observations("asr"))

        with stage_timing.stage("asr", task_id="t-1", file_id=7):
            pass

        lines = _timing_lines(capture)
        assert [(m["stage"], m["outcome"], m["task_id"], m["file_id"]) for m in lines] == [
            ("asr", "success", "t-1", "7")
        ]
        assert (_count("asr", "success"), _observations("asr")) == (before[0] + 1, before[1] + 1)

    def test_exception_records_failure_once_and_propagates(self, capture):
        before = _count("diarization", "failure")

        with pytest.raises(RuntimeError, match="boom"), stage_timing.stage("diarization"):
            raise RuntimeError("boom")

        lines = _timing_lines(capture)
        assert [(m["stage"], m["outcome"], m["task_id"]) for m in lines] == [
            ("diarization", "failure", "-")
        ]
        assert _count("diarization", "failure") == before + 1

    def test_explicit_fail_records_failure(self, capture):
        before = _count("postprocess", "failure")

        with stage_timing.stage("postprocess") as run:
            run.fail()

        assert [m["outcome"] for m in _timing_lines(capture)] == ["failure"]
        assert _count("postprocess", "failure") == before + 1

    def test_decorator_records_once_per_call(self, capture):
        @stage_timing.stage("finalize")
        def work(fail: bool) -> str:
            if fail:
                raise ValueError("no")
            return "ok"

        assert work(False) == "ok"
        with pytest.raises(ValueError):
            work(True)

        assert [m["outcome"] for m in _timing_lines(capture)] == ["success", "failure"]

    def test_nested_stage_inherits_bound_ids(self, capture):
        with stage_timing.bind(task_id="t-9", file_id=3), stage_timing.stage("vad"):
            pass

        (line,) = _timing_lines(capture)
        assert (line["task_id"], line["file_id"]) == ("t-9", "3")

    def test_unknown_stage_is_folded_into_other(self, capture):
        with stage_timing.stage("some-new-thing-with-an-id-1234"):
            pass

        assert [m["stage"] for m in _timing_lines(capture)] == ["other"]

    def test_a_broken_collector_never_breaks_the_stage(self, capture, monkeypatch):
        def _broken():
            raise RuntimeError("registry gone")

        monkeypatch.setattr(stage_timing, "_get_collectors", _broken)

        with stage_timing.stage("asr"):
            result = "still ran"

        assert result == "still ran"
        assert [m["outcome"] for m in _timing_lines(capture)] == ["success"]


def test_label_cardinality_is_bounded():
    for i in range(50):
        with stage_timing.stage(f"file-{i}"):
            pass
    for name in STAGES:
        with contextlib.suppress(RuntimeError), stage_timing.stage(name):
            raise RuntimeError

    label_keys: set[str] = set()
    stages: set[str] = set()
    outcomes: set[str] = set()
    for metric in stage_timing._get_collectors().registry.collect():
        for sample in metric.samples:
            labels = dict(sample.labels)
            labels.pop("le", None)  # histogram bucket bound, fixed by DURATION_BUCKETS
            label_keys |= set(labels)
            stages.add(labels["stage"])
            if "outcome" in labels:
                outcomes.add(labels["outcome"])

    assert label_keys == {"stage", "outcome"}
    assert stages <= {*STAGES, stage_timing.OTHER_STAGE}
    assert outcomes <= set(OUTCOMES)


def test_metrics_are_served_by_a_multiprocess_worker(tmp_path):
    """The same exposition path ``WORKER_METRICS_PORT`` serves (worker_metrics.start_listener)."""
    script = textwrap.dedent(
        f"""
        import os
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = {str(tmp_path)!r}
        from prometheus_client import CollectorRegistry, generate_latest, multiprocess
        from app.core import stage_timing
        with stage_timing.stage("asr"):
            pass
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path={str(tmp_path)!r})
        print(generate_latest(registry).decode())
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=APP_DIR.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout

    assert 'pipeline_stage_total{outcome="success",stage="asr"} 1.0' in out
    assert 'pipeline_stage_duration_seconds_count{stage="asr"} 1.0' in out


# ── call sites ────────────────────────────────────────────────────────────────────────


def _stage_call_sites() -> list[tuple[str, str]]:
    """``(file, stage name)`` for every ``stage_timing.stage("<name>")`` under app/."""
    found = []
    for path in APP_DIR.rglob("*.py"):
        if path.name == "stage_timing.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stage"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "stage_timing"
            ):
                arg = node.args[0] if node.args else None
                name = (
                    arg.value
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    else "<dynamic>"
                )
                found.append((str(path.relative_to(APP_DIR)), name))
    return found


def test_every_stage_is_recorded_somewhere_and_every_call_site_names_a_known_stage():
    sites = _stage_call_sites()
    assert {name for _, name in sites} == set(STAGES), sites


class TestTranscriberStages:
    @pytest.fixture(autouse=True)
    def _no_vram_admission(self, monkeypatch):
        from app.transcription import transcriber as transcriber_mod

        @contextlib.contextmanager
        def _admit(stage, *, device, batch_size=None, **_kwargs):
            yield 0

        monkeypatch.setattr(transcriber_mod.vram_budget, "admit", _admit)

    @staticmethod
    def _transcriber(segments):
        from app.transcription.config import TranscriptionConfig
        from app.transcription.transcriber import Transcriber

        t = Transcriber(TranscriptionConfig(model_name="large-v3", device="cpu"))
        pipeline = MagicMock()
        pipeline.transcribe.return_value = (segments, MagicMock(language="en"))
        t._pipeline = pipeline
        return t

    def test_vad_and_asr_each_record_once(self, capture):
        seg = MagicMock()
        seg.start, seg.end, seg.words, seg.text = 0.0, 1.0, [], " hi"

        self._transcriber(iter([seg])).transcribe(np.zeros(16000, dtype=np.float32))

        assert [(m["stage"], m["outcome"]) for m in _timing_lines(capture)] == [
            ("vad", "success"),
            ("asr", "success"),
        ]

    def test_decode_failure_records_asr_failure(self, capture):
        def _exploding():
            raise RuntimeError("decoder died")
            yield  # pragma: no cover - makes this a generator

        with pytest.raises(RuntimeError, match="decoder died"):
            self._transcriber(_exploding()).transcribe(np.zeros(16000, dtype=np.float32))

        assert [(m["stage"], m["outcome"]) for m in _timing_lines(capture)] == [
            ("vad", "success"),
            ("asr", "failure"),
        ]


class TestTaskStages:
    def test_preprocess_failure_records_once(self, capture, monkeypatch):
        from app.tasks.transcription import preprocess

        monkeypatch.setattr(preprocess, "superseded_result", lambda *a, **k: None)
        monkeypatch.setattr(preprocess, "run_heartbeat", lambda *_: contextlib.nullcontext())

        def _fail(*_a, **_k):
            raise OSError("disk gone")

        monkeypatch.setattr(preprocess, "_run_preprocess", _fail)

        with pytest.raises(OSError):
            preprocess.preprocess_for_transcription.run("f-uuid", "t-pre")

        assert [(m["stage"], m["outcome"], m["task_id"]) for m in _timing_lines(capture)] == [
            ("preprocess", "failure", "t-pre")
        ]

    @pytest.mark.parametrize(("status", "outcome"), [("success", "success"), ("error", "failure")])
    def test_postprocess_records_its_reported_outcome(self, capture, monkeypatch, status, outcome):
        from app.tasks.transcription import postprocess

        monkeypatch.setattr(postprocess, "run_heartbeat", lambda *_: contextlib.nullcontext())
        monkeypatch.setattr(postprocess, "_finalize", lambda _r: {"status": status})
        monkeypatch.setattr(postprocess, "_run_completed", lambda _t: False)

        postprocess.finalize_transcription.run({"task_id": "t-post", "status": "success"})

        assert [(m["stage"], m["outcome"]) for m in _timing_lines(capture)] == [
            ("postprocess", outcome)
        ]

    def test_postprocess_cleanup_only_payload_is_not_a_stage_run(self, capture, monkeypatch):
        from app.tasks.transcription import postprocess

        monkeypatch.setattr(postprocess, "_finalize", lambda _r: {"status": "error"})

        postprocess.finalize_transcription.run({"task_id": "t-x", "status": "error"})

        assert _timing_lines(capture) == []
