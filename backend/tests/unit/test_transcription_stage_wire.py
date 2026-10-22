"""The ``stage`` key on ``transcription_status`` progress frames.

The notification card used to guess a transcription's sub-stage from hard-coded percent
bands. The backend now sends the stage itself, from ONE vocabulary
(``app.core.enums.TranscriptionStage``) that the engine also emits. Pinned here:

  1. Every ``send_progress_notification`` call site in ``app/`` names its stage, and it
     is the one in the table below. A new call site without a row fails, so a stage can
     never again be left for the client to infer.
  2. Every engine ``emit`` names a canonical stage (the engine's former
     ``"preprocess"`` vocabulary is gone, so nothing has to translate it).
  3. The stage reaches the wire payload, and is omitted when there is none.
  4. ``adapt_legacy`` forwards the engine's stage to a stage-aware callback and still
     drives a plain ``(progress, message)`` one; the task-layer closures forward it.
  5. The backend's stage values equal the frontend's step keys, in order.
"""

from __future__ import annotations

import ast
import contextlib
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.enums import FileStatus
from app.core.enums import TranscriptionStage
from app.tasks.transcription import notifications as notif
from app.transcription.engine.progress import adapt_legacy
from app.transcription.engine.progress import emit
from tests.unit._plan_contexts import plan_context

_BACKEND = Path(__file__).resolve().parents[2]
_APP = _BACKEND / "app"
_FRONTEND_JOB = _BACKEND.parent / "frontend/src/lib/notifications/processingJob.ts"

_FORWARDED = "<forwarded>"

# (file, enclosing function, message literal or _FORWARDED) -> stage expression.
# Rationale for the non-obvious rows lives on TranscriptionStage's docstring.
_EXPECTED_CALL_SITES: dict[tuple[str, str, str], str] = {
    # preprocess (CPU): download, ffmpeg, temp upload
    ("tasks/transcription/preprocess.py", "_run_preprocess", "Preparing media file"): "PREPARE",
    (
        "tasks/transcription/preprocess.py",
        "_run_preprocess",
        "Staging audio for transcription",
    ): "PREPARE",
    (
        "tasks/transcription/preprocess.py",
        "_run_preprocess",
        "Audio ready for transcription",
    ): "PREPARE",
    (
        "tasks/transcription/preprocess.py",
        "_preprocess_video",
        "Extracting audio from video",
    ): "PREPARE",
    ("tasks/transcription/preprocess.py", "_preprocess_audio", "Processing audio file"): (
        "PREPARE"
    ),
    # GPU / CPU / cloud transcription
    ("tasks/transcription/core.py", "transcribe_gpu_task", "Starting AI transcription"): (
        "TRANSCRIBE"
    ),
    (
        "tasks/transcription/cpu_task.py",
        "_run_cpu_transcription",
        "Running fast CPU transcription",
    ): "TRANSCRIBE",
    ("tasks/transcription/cpu_task.py", "progress_callback", _FORWARDED): "stage",
    (
        "tasks/transcription/cpu_task.py",
        "transcribe_cpu_task",
        "Starting fast CPU transcription",
    ): "TRANSCRIBE",
    (
        "tasks/transcription/pipelines.py",
        "_run_transcription_pipeline",
        "Running AI transcription",
    ): "TRANSCRIBE",
    ("tasks/transcription/pipelines.py", "progress_callback", _FORWARDED): "stage",
    (
        "tasks/transcription/pipelines.py",
        "_run_engine_pipeline",
        "Running AI transcription",
    ): "TRANSCRIBE",
    ("tasks/transcription/pipelines.py", "_progress", _FORWARDED): "stage",
    (
        "tasks/transcription/pipelines.py",
        "_run_transcribe_only_stage",
        "Running AI transcription",
    ): "TRANSCRIBE",
    (
        "tasks/transcription/cloud_asr.py",
        "_run_cloud_asr_pipeline",
        "Running cloud ASR transcription",
    ): "TRANSCRIBE",
    # Cloud providers report (progress, message) only; all of it is transcription.
    ("tasks/transcription/cloud_asr.py", "cloud_progress_callback", _FORWARDED): "TRANSCRIBE",
    # gpu-split stage 2b
    (
        "tasks/transcription/diarize_task.py",
        "diarize_gpu_task",
        "Analyzing speaker patterns",
    ): "DIARIZE",
    ("tasks/transcription/diarize_task.py", "_progress", _FORWARDED): "stage",
    # finalize: segments + DB save
    (
        "tasks/transcription/finalize.py",
        "_process_and_save_critical",
        "Processing speaker segments",
    ): "FINALIZE",
    (
        "tasks/transcription/finalize.py",
        "_process_and_save_critical",
        "Saving transcript to database",
    ): "FINALIZE",
    # postprocess: the speakers tail
    (
        "tasks/transcription/postprocess.py",
        "_finalize",
        "Processing speaker identification",
    ): "SPEAKERS",
    (
        "tasks/transcription/postprocess.py",
        "_finalize",
        "Transcription complete, detecting speakers...",
    ): "SPEAKERS",
    ("tasks/transcription/postprocess.py", "_finalize", "Finalizing transcription"): "SPEAKERS",
    (
        "tasks/transcription/postprocess.py",
        "_dispatch_post_stage",
        "Queuing speaker detection",
    ): "SPEAKERS",
    (
        "tasks/transcription/postprocess.py",
        "_dispatch_embedding_stage",
        "Dispatching speaker embedding extraction",
    ): "SPEAKERS",
    # post stage / manual re-detect: diarize -> finalize -> speakers
    ("tasks/rediarize_task.py", "rediarize_task", "Starting speaker detection"): "DIARIZE",
    ("tasks/rediarize_task.py", "rediarize_task", "Preparing audio"): "DIARIZE",
    ("tasks/rediarize_task.py", "rediarize_task", "Running speaker detection"): "DIARIZE",
    ("tasks/rediarize_task.py", "rediarize_task", "Assigning speakers"): "FINALIZE",
    ("tasks/rediarize_task.py", "rediarize_task", "Saving updated transcript"): "FINALIZE",
    ("tasks/rediarize_task.py", "rediarize_task", "Processing speaker voiceprints"): "SPEAKERS",
    # a requeued run starts over from preprocess
    (
        "services/transcription_retry.py",
        "_retry_file",
        "Retrying after a temporary problem",
    ): "PREPARE",
}

# Engine emits: message -> stage. "Loading audio" happens inside the GPU stage, after the
# task layer announced transcription, so it is TRANSCRIBE (a finished step must not reopen).
_EXPECTED_ENGINE_EMITS: dict[str, str] = {
    "Decoding audio": "PREPARE",
    "Loading audio": "TRANSCRIBE",
    "Running AI transcription": "TRANSCRIBE",
    "Preparing speaker analysis": "DIARIZE",
    "Analyzing speaker patterns": "DIARIZE",
    "Assigning speakers to transcript": "FINALIZE",
}


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else current
            )
            owner[child] = name
            visit(child, name)

    visit(tree, "<module>")
    return owner


def _stage_expr(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "stage":
            value = kw.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "TranscriptionStage"
            ):
                return value.attr
            return ast.unparse(value)
    return None


def _calls_named(name: str):
    for path in sorted(_APP.rglob("*.py")):
        source = path.read_text()
        if f"{name}(" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            ):
                yield path, owner.get(node, "<module>"), node


# Keys that legitimately occur more than once (one per branch); every other key occurs once.
_REPEATED_CALL_SITES = {
    ("tasks/transcription/core.py", "transcribe_gpu_task", "Starting AI transcription"): 2,
    ("tasks/transcription/pipelines.py", "_progress", _FORWARDED): 2,
    ("tasks/transcription/postprocess.py", "_finalize", "Processing speaker identification"): 2,
}


def test_every_progress_call_site_names_its_expected_stage():
    found: dict[tuple[str, str, str], list[str | None]] = {}
    for path, func, call in _calls_named("send_progress_notification"):
        message = call.args[3] if len(call.args) > 3 else None
        literal = (
            message.value
            if isinstance(message, ast.Constant) and isinstance(message.value, str)
            else _FORWARDED
        )
        key = (path.relative_to(_APP).as_posix(), func, literal)
        found.setdefault(key, []).append(_stage_expr(call))

    missing = sorted(set(_EXPECTED_CALL_SITES) - set(found))
    unexpected = sorted(set(found) - set(_EXPECTED_CALL_SITES))
    assert not missing, f"expected call sites not found (renamed?): {missing}"
    assert not unexpected, (
        f"new send_progress_notification call sites need a stage and a row here: {unexpected}"
    )
    counts = {k: len(v) for k, v in found.items() if len(v) != _REPEATED_CALL_SITES.get(k, 1)}
    assert not counts, f"call-site multiplicity changed; add a row or update the count: {counts}"
    wrong = {k: v for k, v in found.items() if any(s != _EXPECTED_CALL_SITES[k] for s in v)}
    assert not wrong, f"call sites with the wrong stage (found): {wrong}"


def test_forwarding_callbacks_default_to_their_own_stage():
    """A closure the engine drives defaults to its task's stage for a stage-less caller."""
    expected = {
        ("tasks/transcription/pipelines.py", "progress_callback"): "TRANSCRIBE",
        ("tasks/transcription/pipelines.py", "_progress"): "TRANSCRIBE",
        ("tasks/transcription/cpu_task.py", "progress_callback"): "TRANSCRIBE",
        ("tasks/transcription/diarize_task.py", "_progress"): "DIARIZE",
    }
    seen: dict[tuple[str, str], set[str]] = {}
    for rel, func in expected:
        tree = ast.parse((_APP / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func:
                params = node.args.args
                defaults = dict(
                    zip(
                        [a.arg for a in params[-len(node.args.defaults) :]],
                        node.args.defaults,
                        strict=True,
                    )
                )
                assert "stage" in defaults, f"{rel}:{func} does not take a stage"
                seen.setdefault((rel, func), set()).add(ast.unparse(defaults["stage"]))
    assert seen == {k: {f"TranscriptionStage.{v}"} for k, v in expected.items()}


def test_every_engine_emit_names_a_canonical_stage():
    stages_py = _APP / "transcription/engine/stages.py"
    tree = ast.parse(stages_py.read_text())
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "emit"
        ):
            message, stage = node.args[2], node.args[3]
            assert isinstance(message, ast.Constant) and isinstance(message.value, str)
            assert isinstance(stage, ast.Attribute) and isinstance(stage.value, ast.Name)
            assert stage.value.id == "TranscriptionStage", ast.unparse(stage)
            found.append((message.value, stage.attr))
    assert found, "no emit() calls found: the scan is looking in the wrong place"
    assert {m for m, _ in found} == set(_EXPECTED_ENGINE_EMITS)
    wrong = [(m, s) for m, s in found if _EXPECTED_ENGINE_EMITS[m] != s]
    assert not wrong, wrong


def test_oom_retry_frame_carries_prepare():
    calls = [
        (func, call)
        for path, func, call in _calls_named("send_notification_via_redis")
        if path.name == "task_recovery_service.py"
    ]
    assert len(calls) == 1
    assert _stage_expr(calls[0][1]) == "PREPARE"


# ------------------------------------------------------------------- wire payload


@pytest.fixture
def captured(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        "app.services.takedown_service.is_notification_suppressed", lambda *a, **k: False
    )
    monkeypatch.setattr(
        notif,
        "get_file_metadata",
        lambda fid: {
            "file_uuid": "u-1",
            "filename": "a.wav",
            "content_type": "audio/wav",
            "file_size": 1,
        },
    )
    monkeypatch.setattr(notif, "_invalidate_user_files", lambda uid: None)

    def _send(user_id, event_type, data, **kwargs):
        sent.append({"event_type": event_type, "data": data})
        return True

    monkeypatch.setattr(notif, "send_ws_event_for_file", _send)
    return sent


def test_progress_frame_carries_the_stage_value(captured):
    notif.send_progress_notification(7, 3, 0.52, "Analyzing", stage=TranscriptionStage.DIARIZE)

    assert len(captured) == 1
    assert captured[0]["event_type"] == "transcription_status"
    data = captured[0]["data"]
    assert data["stage"] == "diarize"
    assert type(data["stage"]) is str
    assert data["progress"] == 52
    assert data["status"] == FileStatus.PROCESSING.value


def test_frame_without_a_stage_omits_the_key(captured):
    notif.send_notification_via_redis(7, 3, FileStatus.COMPLETED, "done", progress=100)
    notif.send_progress_notification(7, 3, 0.10, "legacy caller")

    assert [("stage" in c["data"]) for c in captured] == [False, False]


def test_retry_wrapper_forwards_the_stage(captured):
    notif.send_notification_with_retry(
        7, 3, FileStatus.PROCESSING, "x", progress=5, stage=TranscriptionStage.PREPARE
    )
    assert captured[0]["data"]["stage"] == "prepare"


# ------------------------------------------------------------------ engine bridge


def test_adapt_legacy_forwards_the_stage_to_a_stage_aware_callback():
    got: list[tuple] = []

    def cb(progress: float, message: str, stage: TranscriptionStage | None = None) -> None:
        got.append((progress, message, stage))

    emit(adapt_legacy(cb), 0.65, "Assigning", TranscriptionStage.FINALIZE)
    assert got == [(0.65, "Assigning", TranscriptionStage.FINALIZE)]


def test_adapt_legacy_still_drives_a_two_argument_callback():
    got: list[tuple] = []
    emit(adapt_legacy(lambda p, m: got.append((p, m))), 0.4, "x", TranscriptionStage.TRANSCRIBE)
    assert got == [(0.4, "x")]


def test_adapt_legacy_none_stays_none():
    assert adapt_legacy(None) is None


def test_transcribe_only_stage_forwards_the_engine_stage(monkeypatch, db_session):
    """The real task-layer closure carries the engine's stage onto the wire call.

    Settings and task-status reads run against the real session; only the engine (which
    would load Whisper), the config factory and the WebSocket send are replaced.
    """
    from app.tasks.transcription import pipelines
    from app.transcription import TranscriptionConfig

    sent: list[tuple] = []

    @contextlib.contextmanager
    def _scope():
        yield db_session

    class _Engine:
        def __init__(self, config):
            pass

        def run_transcribe_only(self, pre, progress_callback=None):
            # DIARIZE is not the closure's default, so only a real forward can produce it.
            emit(adapt_legacy(progress_callback), 0.55, "probe", TranscriptionStage.DIARIZE)
            return MagicMock(serialize=dict)

    monkeypatch.setattr(pipelines, "session_scope", _scope)
    monkeypatch.setattr(
        pipelines,
        "send_progress_notification",
        lambda uid, fid, p, m, stage=None: sent.append((p, m, stage)),
    )
    monkeypatch.setattr(
        TranscriptionConfig,
        "from_environment",
        classmethod(lambda cls, **o: TranscriptionConfig(device="cpu")),
    )
    monkeypatch.setattr("app.transcription.Engine", _Engine)

    pipelines._run_transcribe_only_stage(
        MagicMock(task_id="t", file_id=1, user_id=1),
        "/scratch/a.wav",
        {"pipeline_plan": plan_context()},
    )

    assert sent == [
        (0.40, "Running AI transcription", TranscriptionStage.TRANSCRIBE),
        (0.55, "probe", TranscriptionStage.DIARIZE),
    ]


# ---------------------------------------------------------------- frontend parity


def test_stage_values_equal_the_frontend_step_keys_in_order():
    source = _FRONTEND_JOB.read_text()
    match = re.search(r"export const TRANSCRIPTION_STAGES = \[([^\]]*)\] as const", source)
    assert match, f"TRANSCRIPTION_STAGES not found in {_FRONTEND_JOB}"
    frontend_keys = re.findall(r"'([a-z_]+)'", match.group(1))
    assert frontend_keys == [s.value for s in TranscriptionStage]
