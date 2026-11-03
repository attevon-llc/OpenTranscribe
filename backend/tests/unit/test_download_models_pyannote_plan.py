"""What the downloader's PyAnnote group fetches, checked without pyannote or whisperx installed.

These run in the GitHub ``backend-tests`` job, whose ``requirements-ci.txt`` has neither
package; ``test_download_models_pyannote_no_inference.py`` drives the group itself and only
runs where they are installed (the venv's unit phase, and the backend image).

Three properties:

* the repo list is DERIVED from the backend's own constants, so renaming a model there
  changes what is fetched (checked by renaming it and watching the plan follow);
* every in-process ``from_pretrained`` site in ``backend/app`` is one the plan was written
  for, so a new in-process model load fails here until the downloader covers it;
* the group never transcribes, diarizes or imports whisperx (the v0.6.0 IndexError).
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from app.services import embedding_mode_service
from app.services.embedding_mode_service import MODE_V3
from app.services.embedding_mode_service import MODE_V4
from app.services.embedding_mode_service import EmbeddingModeService
from app.transcription import diarizer

REPO_ROOT = Path(__file__).resolve().parents[3]
DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"
APP_DIR = REPO_ROOT / "backend" / "app"

#: The functions that make up the PyAnnote group.
PYANNOTE_GROUP = (
    "pyannote_repo_plan",
    "pipeline_dependency_repos",
    "_snapshot_with_dependencies",
    "fetch_diarization_pipeline",
    "download_pyannote_models",
)

#: Every module in backend/app that loads a PyAnnote model in-process, and the plan entry
#: that provisions it. A new site must be added here AND to ``pyannote_repo_plan``.
IN_PROCESS_LOAD_SITES = {
    "transcription/diarizer.py": "PYANNOTE_V4_MODEL and PYANNOTE_V3_FALLBACK",
    "services/speaker_embedding_service.py": "get_embedding_model_name(MODE_V4 / MODE_V3)",
}


@pytest.fixture
def downloader():
    """``download-models.py`` loaded as a module (torch.load restored if torch is present)."""
    try:
        import torch
    except ImportError:
        torch = None
    saved = (torch.load, torch.mps.empty_cache) if torch is not None else None
    spec = importlib.util.spec_from_file_location("download_models_plan", DOWNLOADER_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        if torch is not None and saved is not None:
            torch.load, torch.mps.empty_cache = saved


def test_plan_names_every_in_process_model(downloader):
    required, optional = downloader.pyannote_repo_plan()

    assert required == [
        diarizer.PYANNOTE_V4_MODEL,
        EmbeddingModeService.get_embedding_model_name(MODE_V4),
    ]
    assert optional == [
        diarizer.PYANNOTE_V3_FALLBACK,
        EmbeddingModeService.get_embedding_model_name(MODE_V3),
    ]


def test_plan_follows_a_renamed_model(downloader, monkeypatch):
    real_name = EmbeddingModeService.get_embedding_model_name

    def _renamed(mode=None):
        return "acme/renamed-wespeaker" if mode == MODE_V4 else real_name(mode)

    monkeypatch.setattr(diarizer, "PYANNOTE_V4_MODEL", "acme/renamed-diarization")
    monkeypatch.setattr(diarizer, "PYANNOTE_V3_FALLBACK", "acme/renamed-fallback")
    monkeypatch.setattr(
        embedding_mode_service.EmbeddingModeService,
        "get_embedding_model_name",
        staticmethod(_renamed),
    )

    required, optional = downloader.pyannote_repo_plan()

    assert required == ["acme/renamed-diarization", "acme/renamed-wespeaker"]
    assert optional[0] == "acme/renamed-fallback"


def _from_pretrained_sites() -> set[str]:
    sites = set()
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        imports_pyannote = any(
            isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pyannote")
            for node in ast.walk(tree)
        )
        loads = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_pretrained"
            for node in ast.walk(tree)
        )
        if imports_pyannote and loads:
            sites.add(path.relative_to(APP_DIR).as_posix())
    return sites


def test_every_in_process_pyannote_load_site_is_covered_by_the_plan():
    assert _from_pretrained_sites() == set(IN_PROCESS_LOAD_SITES)


def test_pipeline_config_dependencies_are_read_from_the_snapshot(downloader, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "pipeline:\n"
        "  name: pyannote.audio.pipelines.SpeakerDiarization\n"
        "  params:\n"
        "    clustering: AgglomerativeClustering\n"
        "    embedding: pyannote/wespeaker-voxceleb-resnet34-LM\n"
        "    embedding_batch_size: 32\n"
        "    segmentation: pyannote/segmentation-3.0\n"
        "    plda: $model/plda\n"
    )

    deps = downloader.pipeline_dependency_repos(tmp_path)

    assert sorted(deps) == ["pyannote/segmentation-3.0", "pyannote/wespeaker-voxceleb-resnet34-LM"]


def test_pyannote_group_runs_no_inference_and_never_imports_whisperx():
    tree = ast.parse(DOWNLOADER_PY.read_text())
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in PYANNOTE_GROUP
    }
    assert set(functions) == set(PYANNOTE_GROUP)

    offenders = []
    for name, func in functions.items():
        for node in ast.walk(func):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                if any(m.split(".")[0] == "whisperx" for m in modules):
                    offenders.append(f"{name}: imports whisperx")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"transcribe", "load_model", "apply", "crop", "itertracks"}:
                    offenders.append(f"{name}: calls .{node.func.attr}()")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"pipeline", "diarize_model", "model"}:
                    offenders.append(f"{name}: runs {node.func.id}(...)")
    assert offenders == []
