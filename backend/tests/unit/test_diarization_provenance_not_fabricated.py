"""The recorded diarization engine must be the one that RAN, or nothing at all.

``MediaFile.diarization_model`` is what the admin panel and the file detail page report as the
engine that produced a transcript's speakers. It was being fabricated.

The cloud-ASR path computes the real values: ``_run_cloud_asr_pipeline`` reads
``provider_name``/``model_name`` off the ``DiarizeResult`` the speaker detector itself returned
(it used to arrive via ``merge_cloud_diarization``'s ``ASRResult.metadata``, deleted in #1226
when the merge became ``assign_speakers``). It once built its return dict from four fields and
**dropped them**, so neither key reached ``finalize.py`` — which covered for the gap with::

    result.get("diarization_model", "pyannote/speaker-diarization-community-1")

That constant was correct only by luck. ``services/diarization/local_provider.py`` happens to
serve exactly those weights, so on today's default deployment the fabricated string matches
reality. Any other diarization provider on that path was recorded under **PyAnnote's** name —
and recorded next to a ``diarization_provider`` of ``NULL``, because that field was read with no
default and was therefore honest about not knowing. One row asserting a specific model while
admitting it cannot name the engine is the tell.

⚠️ **The fix is to propagate, not to widen the default.** A better-guessed constant is the same
defect. ``storage.py`` skips a ``None`` (``if diarization_model:``), so an unknown engine leaves
the column NULL — which is a reportable "we don't know" rather than a confident wrong answer.

Two legitimate cases still produce ``None`` here, and both mean nothing diarized: the ASR-only
branch, and the non-fatal diarization failure (or an empty result) that leaves the transcript
unmerged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.unit._plan_contexts import make_plan
from tests.unit.test_cloud_asr import FakeASRProvider
from tests.unit.test_cloud_asr import FakeDiarizeProvider
from tests.unit.test_cloud_asr import _asr_result
from tests.unit.test_cloud_asr import _make_ctx
from tests.unit.test_cloud_asr import _yield_session

REPO_ROOT = Path(__file__).resolve().parents[3]
CLOUD_ASR = REPO_ROOT / "backend" / "app" / "tasks" / "transcription" / "cloud_asr.py"
FINALIZE = REPO_ROOT / "backend" / "app" / "tasks" / "transcription" / "finalize.py"

#: The weights both engines serve. Legitimate as a provider's own model_name; a defect as a
#: fallback for "the result dict did not tell us".
SHARED_WEIGHTS = "pyannote/speaker-diarization-community-1"


# ------------------------------------------------------------------ behavioural control


def test_the_pipeline_really_does_record_the_engine_that_ran(db_session, normal_user):
    """Control: the values the pipeline was discarding genuinely exist upstream.

    Without this, the guards below would be asserting that we correctly propagate something
    that was never computed — they would pass over a pipeline recording nothing at all.
    """
    from unittest.mock import patch

    from app.models.media import Task
    from app.services.diarization.types import DiarizeResult
    from app.services.diarization.types import DiarizeSegment
    from app.tasks.transcription.cloud_asr import _run_cloud_asr_pipeline

    db_session.add(
        Task(
            id="task-provenance",
            user_id=normal_user.id,
            media_file_id=None,
            task_type="transcription",
            status="in_progress",
            progress=0.1,
        )
    )
    db_session.commit()
    diarized = DiarizeResult(
        segments=[DiarizeSegment(start=0.0, end=1.0, speaker="SPEAKER_00")],
        num_speakers=1,
        provider_name="some-other-engine",
        model_name="some-other/model-v9",
    )
    cloud_asr = "app.tasks.transcription.cloud_asr"
    with (
        patch(f"{cloud_asr}.session_scope", lambda: _yield_session(db_session)),
        patch(f"{cloud_asr}.send_progress_notification"),
        patch(
            "app.services.diarization.factory.DiarizationProviderFactory.create_for_engine",
            return_value=FakeDiarizeProvider(result=diarized),
        ),
    ):
        result = _run_cloud_asr_pipeline(
            _make_ctx(normal_user, "task-provenance"),
            "/fake/audio.wav",
            min_speakers=None,
            max_speakers=None,
            num_speakers=None,
            provider=FakeASRProvider(result=_asr_result()),
            plan=make_plan("cloud_text_word", "pyannote"),
        )

    assert result["diarization_provider"] == "some-other-engine"
    assert result["diarization_model"] == "some-other/model-v9", (
        "the pipeline no longer carries the diarizer's own model name, so there is nothing "
        "truthful to persist and the fabricated constant becomes the only value available again"
    )


# ------------------------------------------------------------------ the two fixed sites


def _return_dict_keys(path: Path, func_name: str) -> set[str]:
    """Literal string keys of every dict returned by *func_name*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
                keys |= {
                    k.value
                    for k in inner.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    return keys


def test_the_cloud_asr_pipeline_propagates_both_fields():
    keys = _return_dict_keys(CLOUD_ASR, "_run_cloud_asr_pipeline")
    assert keys, "could not find a returned dict literal — the function was restructured"
    missing = {"diarization_provider", "diarization_model"} - keys
    assert not missing, (
        f"_run_cloud_asr_pipeline drops {sorted(missing)} from its result dict. The "
        "detector reported them; discarding them is what forced finalize.py to invent a model "
        "name."
    )


def test_neither_finalize_site_substitutes_a_model_name():
    """`.get(key, <literal>)` on either field is the defect, whatever the literal says."""
    tree = ast.parse(FINALIZE.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if len(node.args) != 2 or not isinstance(node.args[0], ast.Constant):
            continue
        key = node.args[0].value
        if key in {"diarization_model", "diarization_provider"} and not (
            isinstance(node.args[1], ast.Constant) and node.args[1].value is None
        ):
            offenders.append(f"line {node.lineno}: .get({key!r}, <default>)")

    assert not offenders, (
        "finalize.py substitutes a value for a diarization field it was not told:\n  "
        + "\n  ".join(offenders)
        + "\nUnknown must stay unknown — storage.py leaves the column NULL for None, which "
        "reports honestly instead of naming an engine nobody verified."
    )


def test_the_fabricated_constant_is_gone_from_the_task_layer():
    """Narrowly scoped: the weights are legitimate in the provider and the status panels.

    Only the *task* layer, which had no business naming a model it did not observe, is checked.
    """
    text = FINALIZE.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert SHARED_WEIGHTS not in code, (
        f"finalize.py names {SHARED_WEIGHTS!r} in executable code again. It must record what "
        "the pipeline reported, never a constant."
    )


# ------------------------------------------------------------------ guard the guards


def test_the_get_default_detector_actually_fires(tmp_path: Path):
    """A scanner that matches nothing reads exactly like a clean tree."""
    victim = tmp_path / "regressed.py"
    victim.write_text(
        f'def f(result):\n    return result.get("diarization_model", "{SHARED_WEIGHTS}")\n',
        encoding="utf-8",
    )
    tree = ast.parse(victim.read_text(encoding="utf-8"))
    found = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and len(n.args) == 2
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "diarization_model"
        and not (isinstance(n.args[1], ast.Constant) and n.args[1].value is None)
    ]
    assert len(found) == 1, "the detector no longer matches the exact shape that was fixed"


def test_the_return_key_reader_actually_fires(tmp_path: Path):
    victim = tmp_path / "shrunk.py"
    victim.write_text(
        "def _run_cloud_asr_pipeline():\n    return {'segments': [], 'language': 'en'}\n",
        encoding="utf-8",
    )
    keys = _return_dict_keys(victim, "_run_cloud_asr_pipeline")
    assert keys == {"segments", "language"}
    assert "diarization_model" not in keys, "the reader would not notice a dropped field"


def test_an_explicit_none_default_is_accepted():
    """Must-stay-clean: `.get(k, None)` is honest and must not be flagged."""
    tree = ast.parse('r.get("diarization_model", None)\n')
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    flagged = not (isinstance(call.args[1], ast.Constant) and call.args[1].value is None)
    assert not flagged, "an explicit None default is the correct shape and must pass"


@pytest.mark.parametrize("path", [CLOUD_ASR, FINALIZE])
def test_the_files_are_where_this_module_thinks(path: Path):
    assert path.is_file(), f"{path} moved; re-point this module rather than letting it vacuum"
