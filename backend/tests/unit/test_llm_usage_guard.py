"""A module that calls the LLM service must be covered by a usage run.

Derived from the code (AST), not from a list of known callers: every module under
``app/`` that calls an LLM-entry method is found, and each must either open
``llm_usage_run`` itself or be listed in ``COVERED_ELSEWHERE`` / ``EXEMPT`` with a
written reason. A NEW LLM caller therefore fails here until it records usage.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

#: Attribute calls that spend tokens on the configured provider.
LLM_ENTRY_METHODS = frozenset(
    {
        "chat_completion",
        "chat_completion_raw",
        "chat_completion_stream",
        "generate_summary",
        "identify_speakers",
    }
)

#: Modules whose own code is the LLM layer.
LAYER = {"services/llm_service.py", "services/llm_bedrock.py", "services/llm_stream.py"}

#: module -> (module that opens the run, why).
COVERED_ELSEWHERE = {
    "services/topic_extraction_service.py": (
        "tasks/topic_extraction.py",
        "the service is only ever driven by ai.extract_topics_task, which opens the run",
    ),
    "services/redaction/detectors/llm.py": (
        "services/redaction/service.py",
        "detect_and_store opens the run around the detector (it has the file id)",
    ),
}

#: module prefix -> reason it records usage by another mechanism or is not a task run.
EXEMPT = {
    "services/chat/": "chat records chat.tokens per assistant message (services/chat/usage.py)",
    "services/llm_reasoning.py": "admin capability probe, not tied to a file or a task run",
    # chat_completion_raw / chat_completion_stream are NOT metered by LLMService: a new
    # non-chat caller of either needs its own metering path.
}


def _rel(path: Path) -> str:
    return path.relative_to(APP).as_posix()


def _calls_llm(tree: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in LLM_ENTRY_METHODS
        for n in ast.walk(tree)
    )


def _opens_run(tree: ast.AST) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.With):
            for item in n.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "llm_usage_run"
                ):
                    return True
    return False


def _scan() -> dict[str, tuple[bool, bool]]:
    found: dict[str, tuple[bool, bool]] = {}
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found[_rel(path)] = (_calls_llm(tree), _opens_run(tree))
    return found


def test_every_llm_calling_module_is_covered():
    scan = _scan()
    callers = {m for m, (calls, _) in scan.items() if calls}
    assert callers, "scan found no LLM caller at all - the detector is broken"

    uncovered = []
    for module in sorted(callers):
        if module in LAYER or any(module.startswith(p) for p in EXEMPT):
            continue
        opener = COVERED_ELSEWHERE.get(module, (module, ""))[0]
        if not scan.get(opener, (False, False))[1]:
            uncovered.append(f"{module} (needs llm_usage_run in {opener})")
    assert not uncovered, "LLM callers without a usage run: " + ", ".join(uncovered)


def test_the_expected_callers_are_actually_seen():
    """Must-fire control: the scan really finds the known callers."""
    scan = _scan()
    for module in (
        "tasks/summarization.py",
        "tasks/speaker_identification_task.py",
        "services/topic_extraction_service.py",
        "services/redaction/detectors/llm.py",
    ):
        assert scan[module][0], module
    assert scan["tasks/summarization.py"][1] is True
    assert scan["tasks/topic_extraction.py"][1] is True
    assert scan["services/redaction/service.py"][1] is True


def test_exemption_and_coverage_tables_are_not_stale():
    scan = _scan()
    for module in COVERED_ELSEWHERE:
        assert module in scan, f"{module} no longer exists - drop its entry"
    for prefix in EXEMPT:
        assert any(m.startswith(prefix) for m in scan), f"{prefix} matches nothing"


def test_guard_detects_an_uncovered_caller():
    """Must-fire: a synthetic module calling the service without a run is flagged."""
    tree = ast.parse("def f(llm):\n    return llm.chat_completion([])\n")
    assert _calls_llm(tree) and not _opens_run(tree)
    covered = ast.parse(
        "def f(llm):\n    with llm_usage_run('summary', file_id=1):\n"
        "        return llm.chat_completion([])\n"
    )
    assert _calls_llm(covered) and _opens_run(covered)


@pytest.mark.parametrize("stage", ["summary", "topics", "speaker_id", "redaction"])
def test_every_stage_has_an_event_type(stage):
    from app.services.llm_usage import STAGE_EVENT_TYPES

    assert STAGE_EVENT_TYPES[stage].endswith(".tokens")
