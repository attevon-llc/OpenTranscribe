"""The recorded diarization engine must be the one that RAN, or nothing at all.

``MediaFile.diarization_model`` is what the admin panel and the file detail page report as the
engine that produced a transcript's speakers. It was being fabricated.

The cloud-ASR path computes the real values: ``merge_cloud_diarization``
(``app/utils/diarization_merge.py``) reads ``provider_name``/``model_name`` off the
``DiarizeResult`` the provider itself returned and puts both into ``ASRResult.metadata``. But
``_run_cloud_asr_pipeline`` built its return dict from four fields and **dropped the metadata**,
so neither key reached ``finalize.py`` — which covered for the gap with::

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
branch, and the non-fatal diarization failure that returns the unmerged ``ASRResult``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLOUD_ASR = REPO_ROOT / "backend" / "app" / "tasks" / "transcription" / "cloud_asr.py"
FINALIZE = REPO_ROOT / "backend" / "app" / "tasks" / "transcription" / "finalize.py"

#: The weights both engines serve. Legitimate as a provider's own model_name; a defect as a
#: fallback for "the result dict did not tell us".
SHARED_WEIGHTS = "pyannote/speaker-diarization-community-1"


# ------------------------------------------------------------------ behavioural control


def test_the_merge_really_does_record_the_engine_that_ran():
    """Control: the values the pipeline was discarding genuinely exist upstream.

    Without this, the guards below would be asserting that we correctly propagate something
    that was never computed — they would pass over a pipeline recording nothing at all.
    """
    from app.services.asr.types import ASRResult
    from app.services.diarization.types import DiarizeResult
    from app.services.diarization.types import DiarizeSegment
    from app.utils.diarization_merge import merge_cloud_diarization

    asr = ASRResult(
        segments=[],
        language="en",
        provider_name="gladia",
        model_name="gladia-v2",
        metadata={},
    )
    diarized = DiarizeResult(
        segments=[DiarizeSegment(start=0.0, end=1.0, speaker="SPEAKER_00")],
        num_speakers=1,
        provider_name="some-other-engine",
        model_name="some-other/model-v9",
    )

    merged = merge_cloud_diarization(asr, diarized)

    assert merged.metadata["diarization_provider"] == "some-other-engine"
    assert merged.metadata["diarization_model"] == "some-other/model-v9", (
        "the merge no longer carries the diarizer's own model name, so there is nothing "
        "truthful for the pipeline to propagate and the fabricated constant becomes the only "
        "value available again"
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
        f"_run_cloud_asr_pipeline drops {sorted(missing)} from its result dict. The merge "
        "computed them; discarding them is what forced finalize.py to invent a model name."
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
