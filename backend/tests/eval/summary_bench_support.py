"""Import the ``scripts/summary_bench`` package (it lives outside ``backend``) for tests."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def load_bench() -> SimpleNamespace:
    """Put ``scripts/`` on ``sys.path`` (idempotent) and return the modules tests exercise."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    from summary_bench import arms
    from summary_bench import cli
    from summary_bench import config
    from summary_bench import inputs
    from summary_bench import planner
    from summary_bench import replay
    from summary_bench import report
    from summary_bench import runner
    from summary_bench import scoring
    from summary_bench import service

    return SimpleNamespace(
        arms=arms,
        cli=cli,
        config=config,
        inputs=inputs,
        planner=planner,
        replay=replay,
        report=report,
        runner=runner,
        scoring=scoring,
        service=service,
    )


def segment(speaker: str, start: float, text: str) -> SimpleNamespace:
    """A stand-in transcript segment satisfying the builder's ``MaskableSegment`` protocol."""
    return SimpleNamespace(
        text=text,
        start_time=start,
        end_time=start + 2.0,
        speaker=SimpleNamespace(name=speaker, display_name=None, suggested_name=None),
        redactions=None,
        words=None,
    )


def synthetic_transcript(turns: int = 420, offset_s: float = 0.0, width: int = 100) -> str:
    """A transcript rendered by the app's own builder, alternating two speakers."""
    from app.utils.transcript_builders import build_transcript_and_stats

    segments: list[Any] = [
        segment(f"SPEAKER_0{i % 2}", offset_s + i * 9.0, f"{'word ' * (width // 5)}n{i}")
        for i in range(turns)
    ]
    return build_transcript_and_stats(segments, None, voiceprint_names=True)[0]


SCOPE = " SCOPE-RULES-SENTINEL-A."
MERGE = " MERGE-RULES-SENTINEL-A."
MERGE_MARKER = "You are combining multiple section summaries"
SLICE_JSON = {
    "key_points": ["a point"],
    "speakers_in_section": ["SPEAKER_00"],
    "decisions": ["The remote will be yellow"],
    "action_items": [],
    "topics_discussed": ["colour"],
}
MERGED_JSON = {
    "bluf": "The team agreed on a yellow remote.",
    "brief_summary": "A design meeting.",
    "major_topics": [],
    "action_items": [],
    "key_decisions": ["The remote will be yellow"],
    "follow_up_items": [],
}


class FakeTransport:
    """Canned OpenAI-shaped replies; records every request's system message."""

    def __init__(self, *, fail_merge: bool = False, fail_slices: bool = False) -> None:
        self.systems: list[tuple[str, str]] = []  # (phase, system message)
        self.payloads: list[dict[str, Any]] = []
        self.fail_merge = fail_merge
        self.fail_slices = fail_slices
        self._lock = threading.Lock()

    def __call__(self, url: str, payload: dict, headers: dict, timeout: int) -> dict[str, Any]:
        system = next(m["content"] for m in payload["messages"] if m["role"] == "system")
        merge = MERGE_MARKER in system
        with self._lock:
            self.systems.append(("merge" if merge else "slice", system))
            self.payloads.append(payload)
        if merge and self.fail_merge:
            raise RuntimeError("merge endpoint down")
        if not merge and self.fail_slices:
            raise RuntimeError("slice endpoint down")
        body = MERGED_JSON if merge else SLICE_JSON
        return {
            "choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }


def lock_entry(bench: SimpleNamespace, transcript: str, *, failing: int | None, mode: str | None):
    chunks, response = bench.inputs.chunk_transcript(transcript, 12_000)
    assert len(chunks) >= 3, "the synthetic transcript must slice into at least 3 sections"
    return {
        "max_tokens": 12_000,
        "response_tokens": response,
        "chunks": [{"chars": len(c), "sha256": bench.inputs.sha256(c)} for c in chunks],
        "failing_slice": failing,
        "tail_index": None,
    }


def make_context(
    bench: SimpleNamespace, transport: FakeTransport, *, mode: str | None, failing: int | None = 2
):
    from app.core.enums import LLMProvider
    from app.services.llm_types import LLMConfig

    transcript = synthetic_transcript(turns=900)
    entry = lock_entry(bench, transcript, failing=failing if mode else None, mode=mode)
    spec = bench.inputs.InputSpec(
        "IX", ("Bxx",), bench.inputs.KIND_FAILURE if mode else "tail", mode
    )
    arm = bench.arms.Arm("x", SCOPE, MERGE)
    ctx = bench.runner.RunContext(
        specs={"IX": spec},
        lock={"inputs": {"IX": entry}},
        rendered={"IX": bench.inputs.RenderedInput(transcript, {"SPEAKER_00": {"words": 1}}, {})},
        arms={"x": arm, "y": arm.renamed("y")},
        llm_config_factory=lambda max_tokens: LLMConfig(
            provider=LLMProvider.VLLM,
            model="fake",
            base_url="http://127.0.0.1:9/v1",
            max_tokens=max_tokens,
        ),
        prompt="Summarize.\n{transcript}\n{speaker_data}",
        transport=transport,
    )
    return ctx, entry
