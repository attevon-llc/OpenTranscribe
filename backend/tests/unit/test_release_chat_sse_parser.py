"""Unit tests for `scripts/release-tests/lib/parse-chat-sse.py` (GH #611).

The bug this guards against: the release-rehearsal harness used to print the chat
answer, citation count and error code as three newline-separated "records" on stdout,
read back positionally with `sed -n '1p'/'2p'/'3p'`. The mock LLM's answer
(`scripts/mock-llm-server.py`'s `REPLY_TEMPLATE`) is multi-paragraph markdown, so the
first record is itself multi-line — a positional read of "line 3" landed inside the
answer text and was misreported as an `event: error` frame's code. Four independent
proofs that the backend was never at fault live in the issue; this suite drives the
harness's own parser, invoked exactly as bash invokes it (subprocess, not import), which
is the only vehicle that can actually exercise the defect.

`pytestmark = pytest.mark.unit` — no live stack, no containers; pure text in, JSON out.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PARSER = (
    Path(__file__).resolve().parents[3] / "scripts" / "release-tests" / "lib" / "parse-chat-sse.py"
)

# Copied verbatim from scripts/mock-llm-server.py's REPLY_TEMPLATE. This is the exact
# multi-paragraph markdown string that produced #611 — the third line of THIS string,
# not a made-up example, was what got reported as the error.
REPLY_TEMPLATE = (
    "Based on the transcript excerpts provided, here is what I found.\n\n"
    "The discussion covers several points relevant to your question about "
    "**{topic}**. The speakers return to it more than once [1], and a later "
    "passage adds detail that clarifies the earlier remark [2].\n\n"
    "Key points:\n\n"
    "- The first excerpt establishes the context [1]\n"
    "- A follow-up exchange expands on it [2]\n"
    "- No decision is recorded in the retrieved passages\n\n"
    "> This is mock output from `scripts/mock-llm-server.py` -- no real model "
    "was consulted. Connect a provider in Settings -> AI for real answers.\n\n"
    "```python\n"
    "# Code blocks render with syntax highlighting and a copy button.\n"
    "print('hello from the mock LLM')\n"
    "```\n"
)


def _delta_frames(text: str) -> list[str]:
    """Word-split `text` into `event: delta` frames, mirroring how the mock streams."""
    import re

    lines = []
    for token in re.findall(r"\S+\s*", text):
        lines.append("event: delta")
        lines.append(f"data: {json.dumps({'content': token})}")
        lines.append("")
    return lines


def _sources_frame(citation_ids: list[int]) -> list[str]:
    return [
        "event: sources",
        f"data: {json.dumps({'citations': [{'id': i} for i in citation_ids]})}",
        "",
    ]


def _error_frame(code: str, message: str) -> list[str]:
    return [
        "event: error",
        f"data: {json.dumps({'code': code, 'message': message})}",
        "",
    ]


def _done_frame() -> list[str]:
    return ["event: done", "data: {}", ""]


def run_parser(sse_lines: list[str]) -> tuple[str, str, int]:
    """Invoke the parser as bash does: a real subprocess, stdin -> stdout/stderr.

    Returns (stdout_text, stderr_text, returncode).
    """
    stream = "\n".join(sse_lines) + "\n"
    proc = subprocess.run(
        [sys.executable, str(_PARSER)],
        input=stream,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def parsed(sse_lines: list[str]) -> dict:
    stdout, _stderr, code = run_parser(sse_lines)
    assert code == 0, f"parser exited {code}, stderr: {run_parser(sse_lines)[1]}"
    lines = stdout.splitlines()
    assert len(lines) == 1, f"expected exactly one line of JSON, got {len(lines)}: {lines!r}"
    result: dict = json.loads(lines[0])
    return result


# ---------------------------------------------------------------------------
# Case 1 — the #611 shape itself
# ---------------------------------------------------------------------------


def test_mock_gpt_multiline_answer_is_not_misread_as_an_error():
    topic = "What was discussed?"
    text = REPLY_TEMPLATE.format(topic=topic)
    sse = _delta_frames(text) + _sources_frame([1, 2]) + _done_frame()

    stdout, _stderr, code = run_parser(sse)
    assert code == 0
    lines = stdout.splitlines()
    assert len(lines) == 1, f"parser must emit exactly one line, got {lines!r}"

    result = json.loads(lines[0])
    assert "\n" in result["answer"], "the answer must retain its embedded newlines"
    assert result["answer"] == text
    assert result["citations"] == 2
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# Case 2 — a genuine error frame must still be reported (GH #595 non-regression)
# ---------------------------------------------------------------------------


def test_mock_error_reports_the_real_error_frame():
    sse = _error_frame("provider_error", "Mock provider failure")
    result = parsed(sse)
    assert result["error"] == "provider_error: Mock provider failure"
    assert result["answer"] == ""


# ---------------------------------------------------------------------------
# Case 3 — empty answer is distinguishable from a blocked call
# ---------------------------------------------------------------------------


def test_mock_empty_answer_and_no_error_both_stay_empty():
    sse = _sources_frame([]) + _done_frame()
    result = parsed(sse)
    assert result["answer"] == ""
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# Case 4 — the timeout code keeps its prefix
# ---------------------------------------------------------------------------


def test_timeout_error_code_is_reported_with_its_prefix():
    sse = _error_frame("timeout", "The model did not start responding in time.")
    result = parsed(sse)
    assert result["error"] == "timeout: The model did not start responding in time."


# ---------------------------------------------------------------------------
# Case 5 — reasoning frames never leak into the answer
# ---------------------------------------------------------------------------


def test_reasoning_frames_do_not_pollute_the_answer():
    sse = []
    sse += [
        "event: reasoning",
        f"data: {json.dumps({'content': 'thinking about it'})}",
        "",
    ]
    sse += _delta_frames("The answer.")
    sse += _done_frame()

    result = parsed(sse)
    assert result["answer"] == "The answer."
    assert "thinking" not in result["answer"]
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# Case 6 — multiple error frames join with "; " (preserve existing behaviour)
# ---------------------------------------------------------------------------


def test_multiple_error_frames_are_joined_with_semicolon():
    sse = _error_frame("provider_error", "first failure") + _error_frame(
        "timeout", "second failure"
    )
    result = parsed(sse)
    assert result["error"] == "provider_error: first failure; timeout: second failure"


# ---------------------------------------------------------------------------
# Case 7 — an unknown future frame name is ignored
# ---------------------------------------------------------------------------


def test_unknown_frame_name_is_ignored():
    sse = [
        "event: plan_hint",
        f"data: {json.dumps({'hint': 'irrelevant'})}",
        "",
    ]
    sse += _delta_frames("Real answer.")
    sse += _done_frame()

    result = parsed(sse)
    assert result["answer"] == "Real answer."
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# Case 8 — malformed input does not crash the parser
# ---------------------------------------------------------------------------


def test_malformed_input_lines_are_ignored_without_crashing():
    sse = [
        "data: no preceding event line",  # ignored: no event set
        ": this is a keepalive comment",  # ignored: neither event: nor data:
        "event: delta",
        "data: ",  # empty data payload
        "",
    ]
    sse += _delta_frames("Still works.")
    sse += _done_frame()

    stdout, _stderr, code = run_parser(sse)
    assert code == 0
    result = json.loads(stdout.splitlines()[0])
    assert "Still works." in result["answer"]
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# Case 9 — the structural invariant the whole fix rests on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sse",
    [
        _delta_frames(REPLY_TEMPLATE.format(topic="x")) + _sources_frame([1, 2]) + _done_frame(),
        _error_frame("provider_error", "boom"),
        _sources_frame([]) + _done_frame(),
    ],
)
def test_output_is_always_exactly_one_line(sse):
    stdout, _stderr, code = run_parser(sse)
    assert code == 0
    assert len(stdout.rstrip("\n").splitlines()) == 1


# ---------------------------------------------------------------------------
# Case 10 — adversarial content cannot forge a record boundary
# ---------------------------------------------------------------------------


def test_adversarial_answer_content_cannot_forge_an_error_record():
    injected = "\nprovider_error: injected\n"
    sse = _delta_frames(injected) + _done_frame()

    result = parsed(sse)
    assert injected.strip() in result["answer"]
    assert result["error"] == "", (
        "an event: delta frame's own content must never be misread as an "
        "event: error frame — this is the general form of #611"
    )
