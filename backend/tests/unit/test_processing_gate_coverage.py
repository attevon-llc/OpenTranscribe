"""Every call that starts processing is behind the legal processing gate.

Nothing here is a hand-maintained list of entry points. The test derives them:

1. **Processing tasks** come from the Celery routing table: every registered task routed to a
   queue that processes content (``nlp``, ``redaction``, ``download``), every task in
   ``GPU_PREFERRED_TASKS``, and every call-time-routed task (``INTENTIONALLY_UNROUTED_TASKS``,
   the transcription chain links). Each must call the gate in its own body, or sit in
   ``EXEMPT_TASKS`` with a written reason. A new task on one of those queues fails here until
   it is classified.
2. **Dispatchers** (``dispatch_transcription_pipeline`` and friends) must reach
   ``_run_pre_dispatch_gate``, which runs the gate first.
3. **Routes**: every route handler under ``app/api`` that can reach a call starting a
   processing task or a dispatcher (through a name-based call graph over ``app/``) must also
   reach a gate call, so the caller gets a 403 with the refusal code rather than a refused
   task. Exemptions need a reason in ``EXEMPT_ROUTES``.

The detectors are run on synthetic sources too (``test_selftest_*``): a detector that matches
nothing reports zero findings, which reads exactly like a clean codebase.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest
from celery import Task

APP = Path(__file__).resolve().parents[2] / "app"

GATE_CALLS = frozenset(
    {
        "require_processing_allowed",
        "check_processing_allowed",
        "require_processing_allowed_for_file",
        "check_processing_allowed_for_file",
        "refusal_for_file",
        "_run_pre_dispatch_gate",
    }
)
DISPATCHERS = frozenset(
    {
        "dispatch_transcription_pipeline",
        "dispatch_batch_transcription",
        "dispatch_upload_pipeline",
        "dispatch_upload_pipeline_or_mark_error",
    }
)
TASK_CALL_ATTRS = frozenset({"delay", "apply_async", "s", "si", "signature"})
PROCESSING_QUEUES = frozenset({"nlp", "redaction", "download"})
# GET is excluded on purpose: a read never answers 403 for a missing acceptance. A GET that
# lazily queues work (redaction detection on first open) is covered by that task's own gate.
ROUTE_METHODS = frozenset({"post", "put", "patch", "delete"})

EXEMPT_TASKS: dict[str, str] = {
    "ai.auto_label_batch": "applies stored topic suggestions as tags; no model runs on content",
    "ai.group_batch_files": "groups files by stored topic suggestions; no model runs",
    "ai.retroactive_auto_label": "coordinator of ai.auto_label_batch; no model runs",
    "ai.extract_topics_batch": "fans out to ai.extract_topics, which is gated per file",
    "artifacts.generate_file_facts": (
        "deterministic, local pipeline stage dispatched after a run that passed the gate"
    ),
    "asr.download_local_model": "downloads a model, touches no user content",
    "backup.mirror_run": "copies stored objects for backup; processes nothing",
    "download.prepare_media": "export of a stored file; processes nothing new",
    "download.prepare_bulk_subtitles": "export of stored transcripts",
    "download.prepare_transcript_document": "export of a stored transcript",
    "redaction.reindex_all": "search reindex maintenance over stored detections",
    "analyze_speakers_combined_batch": "maintenance migration over stored voiceprints",
    "detect_speaker_attributes_batch": "maintenance migration over stored voiceprints",
    "extract_v4_embeddings": "embedding-format migration (maintenance re-embed)",
    "extract_v4_embeddings_batch": "embedding-format migration (maintenance re-embed)",
    "speaker.recluster_all": "maintenance re-cluster of stored voiceprints",
    "speaker_embedding_consistency_repair_batch": "maintenance repair of stored voiceprints",
    "update_speaker_embedding_on_reassignment": (
        "recomputes a centroid after a human edit; no new content is processed"
    ),
    "transcription.cpu_transcribe": "chain link published only by a dispatcher (gated)",
    "transcription.gpu_transcribe": "chain link published only by a dispatcher (gated)",
    "transcription.diarize_gpu": "chain link published only by a dispatcher (gated)",
}

# Route handler name -> reason. Keyed by "<module path relative to app>::<function>".
EXEMPT_ROUTES: dict[str, str] = {
    "api/endpoints/files/__init__.py::update_transcript_segment": (
        "a human transcript edit is never refused; the redaction re-detection it queues is "
        "gated inside redaction.detect"
    ),
    "api/endpoints/files/transcript_document_export.py::request_document_export": (
        "export of a stored transcript; any detection it queues is gated inside redaction.detect"
    ),
    "api/endpoints/files/management.py::recover_stuck_files_bulk": (
        "admin bulk recovery: check_for_stuck_files never selects a refused file, and each "
        "re-dispatch passes _run_pre_dispatch_gate, which marks a refused file ERROR"
    ),
    "api/endpoints/tasks.py::recover_all_stuck_tasks": (
        "admin bulk recovery: each re-dispatch passes _run_pre_dispatch_gate, which marks a "
        "refused file ERROR with its code; the per-file failure is reported in the response"
    ),
    "api/endpoints/tasks.py::recover_task": (
        "admin recovery of one task row: the re-dispatch passes _run_pre_dispatch_gate, which "
        "marks a refused file ERROR; the failure is reported as dispatch_error"
    ),
}


# ---------------------------------------------------------------------------- AST helpers


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _task_call_target(node: ast.Call) -> str | None:
    """``X`` for a ``X.delay(...)`` / ``X.apply_async(...)`` / ``X.s(...)`` call."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in TASK_CALL_ATTRS:
        target = func.value
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute):
            return target.attr
    return None


class _FunctionFacts:
    def __init__(self, module: str, node: ast.FunctionDef | ast.AsyncFunctionDef):
        self.key = f"{module}::{node.name}"
        self.name = node.name
        self.calls: set[str] = set()
        self.task_targets: set[str] = set()
        self.is_route = any(_is_route_decorator(d) for d in node.decorator_list)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = _call_name(sub)
                if name:
                    self.calls.add(name)
                target = _task_call_target(sub)
                if target:
                    self.task_targets.add(target)


def _is_route_decorator(dec: ast.expr) -> bool:
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
        owner = dec.func.value
        owner_name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
        return dec.func.attr in ROUTE_METHODS and "router" in owner_name
    return False


def collect(sources: dict[str, str]) -> list[_FunctionFacts]:
    """Function facts for every function in *sources* (``{module_key: source}``)."""
    facts = []
    for module, text in sources.items():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                facts.append(_FunctionFacts(module, node))
    return facts


def ungated_routes(facts: list[_FunctionFacts], processing_task_funcs: set[str]) -> list[str]:
    """Route handlers that can reach a processing start but never reach a gate call.

    The call graph is by name: a call to ``f`` is an edge to every function named ``f``.
    Traversal stops at a dispatcher or processing task (their own gate does not count for the
    route: the route must refuse before its side effects).
    """
    by_name: dict[str, list[_FunctionFacts]] = defaultdict(list)
    for f in facts:
        by_name[f.name].append(f)
    starters = DISPATCHERS | processing_task_funcs

    def reach(root: _FunctionFacts) -> tuple[bool, bool]:
        starts = gated = False
        seen: set[str] = set()
        stack = [root]
        while stack:
            f = stack.pop()
            if f.key in seen:
                continue
            seen.add(f.key)
            if f.calls & GATE_CALLS:
                gated = True
            if f.calls & DISPATCHERS or f.task_targets & processing_task_funcs:
                starts = True
            for name in f.calls:
                if name in starters or name in GATE_CALLS:
                    continue
                stack.extend(by_name.get(name, ()))
        return starts, gated

    out = []
    for f in facts:
        if not f.is_route or f.key in EXEMPT_ROUTES:
            continue
        starts, gated = reach(f)
        if starts and not gated:
            out.append(f.key)
    return sorted(out)


def _app_sources() -> dict[str, str]:
    return {
        str(p.relative_to(APP)): p.read_text(encoding="utf-8")
        for p in APP.rglob("*.py")
        if "scripts" not in p.relative_to(APP).parts
    }


# ---------------------------------------------------------------------------- the registry


def _processing_tasks() -> dict[str, Task]:
    from app.core.celery import GPU_PREFERRED_TASKS
    from app.core.celery import INTENTIONALLY_UNROUTED_TASKS
    from app.core.celery import celery_app

    celery_app.loader.import_default_modules()
    routes = celery_app.conf.task_routes
    out = {}
    for name, task in celery_app.tasks.items():
        if name.startswith("celery."):
            continue
        queue = (routes.get(name) or {}).get("queue")
        if (
            str(queue) in PROCESSING_QUEUES
            or name in GPU_PREFERRED_TASKS
            or name in INTENTIONALLY_UNROUTED_TASKS
        ):
            out[name] = task
    return out


def _calls_in(func) -> set[str]:
    """Every call name *func* reaches through functions of its own module (by name)."""
    import inspect

    module = inspect.getmodule(func)
    assert module is not None, f"cannot locate the module of {func!r}"
    module_facts = collect({"m": inspect.getsource(module)})
    by_name: dict[str, list[_FunctionFacts]] = defaultdict(list)
    for f in module_facts:
        by_name[f.name].append(f)
    out: set[str] = set()
    seen: set[int] = set()
    stack = list(by_name[func.__name__])
    while stack:
        f = stack.pop()
        if id(f) in seen:
            continue
        seen.add(id(f))
        out |= f.calls
        for name in f.calls:
            stack.extend(by_name.get(name, ()))
    return out


def test_processing_task_set_is_derived_and_not_vacuous():
    tasks = _processing_tasks()
    # The tasks the gate exists for must be in the derived set, or the derivation is broken.
    for name in ("ai.generate_summary", "redaction.detect", "rediarize", "download.media_url"):
        assert name in tasks
    assert set(EXEMPT_TASKS) <= set(tasks), (
        f"exemptions naming no processing task: {sorted(set(EXEMPT_TASKS) - set(tasks))}"
    )


def test_every_processing_task_calls_the_gate_or_is_exempt():
    missing = []
    for name, task in sorted(_processing_tasks().items()):
        if name in EXEMPT_TASKS:
            continue
        if not _calls_in(task.run) & GATE_CALLS:
            missing.append(name)
    assert missing == [], (
        "these tasks start processing of a user's file without the legal processing gate: "
        "call app.legal.processing_gate in the task body, or add an EXEMPT_TASKS reason"
    )


def test_every_dispatcher_reaches_the_pre_dispatch_gate():
    from app.api.endpoints.files import upload
    from app.tasks.transcription import dispatch

    assert "_run_pre_dispatch_gate" in _calls_in(dispatch.dispatch_transcription_pipeline)
    assert "_run_pre_dispatch_gate" in _calls_in(dispatch.dispatch_batch_transcription)
    assert "check_processing_allowed_for_file" in _calls_in(dispatch._run_pre_dispatch_gate)
    # The upload tails reach the gated dispatcher.
    assert "dispatch_transcription_pipeline" in _calls_in(upload.dispatch_upload_pipeline)
    assert "dispatch_upload_pipeline" in _calls_in(upload.dispatch_upload_pipeline_or_mark_error)


def test_every_route_that_starts_processing_checks_the_gate():
    task_funcs = {t.run.__name__ for t in _processing_tasks().values()}
    task_funcs -= {t.run.__name__ for n, t in _processing_tasks().items() if n in EXEMPT_TASKS}
    facts = collect(_app_sources())
    found_routes = [f for f in facts if f.is_route]
    assert len(found_routes) > 150, "route detection matched almost nothing"
    assert ungated_routes(facts, task_funcs) == [], (
        "these routes can start processing without checking the legal processing gate first: "
        "call app.legal.processing_gate.require_processing_allowed[_for_file] before any side "
        "effect, or add an EXEMPT_ROUTES reason"
    )


# ---------------------------------------------------------------------------- self-tests

_FIRE = """
@router.post("/x")
def handler(db):
    _helper(db)

def _helper(db):
    summarize_transcript_task.delay("u")
"""

_CLEAN = """
@router.post("/x")
def handler(db):
    require_processing_allowed(db, owner_user_id=1, organization_id=None)
    _helper(db)

def _helper(db):
    summarize_transcript_task.delay("u")

@router.get("/y")
def reader(db):
    return 1
"""

_FIRE_DISPATCH = """
@user_router.post("/z")
async def handler(db):
    dispatch_transcription_pipeline(file_uuid="u")
"""


@pytest.mark.parametrize(
    "source, expected",
    [
        (_FIRE, ["m.py::handler"]),
        (_FIRE_DISPATCH, ["m.py::handler"]),
        (_CLEAN, []),
    ],
    ids=["helper-task-call", "direct-dispatch", "gated-and-unrelated"],
)
def test_selftest_route_detector(source, expected):
    facts = collect({"m.py": source})
    assert ungated_routes(facts, {"summarize_transcript_task"}) == expected
