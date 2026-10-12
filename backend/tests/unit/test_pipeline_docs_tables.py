"""The Deployment variants page's tables are generated from the code that decides them (#1230).

``scripts/generate-pipeline-docs.py`` writes three Markdown partials (published images,
provider speakers/timing, and the transcription x speaker-detection validity matrix) from
``docker-build-push.sh list-platforms``, ``ASR_PROVIDER_CATALOG`` and
``resolve_pipeline_plan``. These tests fail when a committed partial no longer matches what the
code produces, and prove the comparison can fail.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.services.asr.factory import ASR_PROVIDER_CATALOG
from app.services.pipeline_plan import BLOCKING_CODES

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate-pipeline-docs.py"
PAGE = REPO_ROOT / "docs-site" / "docs" / "installation" / "deployment-variants.md"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_pipeline_docs", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


@pytest.fixture(scope="module")
def generated() -> dict[str, str]:
    return {name: render() for name, render in gen.GENERATORS.items()}


def test_committed_partials_match_the_code(generated: dict[str, str]) -> None:
    assert gen.drift(generated=generated) == []


def test_a_hand_edited_partial_fires(generated: dict[str, str], tmp_path: Path) -> None:
    shutil.copytree(gen.OUT_DIR, tmp_path, dirs_exist_ok=True)
    matrix = tmp_path / "validity-matrix.md"
    matrix.write_text(
        matrix.read_text(encoding="utf-8").replace(
            "Refused: `asr_no_timing_for_speakers`", "Speakers from the transcription vendor", 1
        ),
        encoding="utf-8",
    )
    problems = gen.drift(tmp_path, generated)
    assert len(problems) == 1
    assert problems[0].startswith("validity-matrix.md is stale")


def test_a_missing_partial_fires(generated: dict[str, str], tmp_path: Path) -> None:
    shutil.copytree(gen.OUT_DIR, tmp_path, dirs_exist_ok=True)
    (tmp_path / "images.md").unlink()
    problems = gen.drift(tmp_path, generated)
    assert problems == [f"images.md is missing from {tmp_path}"]


def test_an_unchanged_copy_stays_clean(generated: dict[str, str], tmp_path: Path) -> None:
    shutil.copytree(gen.OUT_DIR, tmp_path, dirs_exist_ok=True)
    assert gen.drift(tmp_path, generated) == []


def test_the_matrix_is_computed_not_constant(generated: dict[str, str]) -> None:
    """Both images, every column, and refusals that only the resolver can produce."""
    matrix = generated["validity-matrix.md"]
    assert "**Full image (GPU or CPU-only)**" in matrix
    assert "**Lite image**" in matrix
    for _source, title in gen.COLUMNS:
        assert title in matrix
    lite = matrix.split("**Lite image**", 1)[1]
    full = matrix.split("**Lite image**", 1)[0]
    assert "`local_asr_unavailable`" in lite and "`local_asr_unavailable`" not in full
    assert "`automatic_needs_speaker_provider`" in lite
    assert "`asr_no_timing_for_speakers`" in full
    for line in matrix.splitlines():
        for code in (c for c in BLOCKING_CODES if f"`{c}`" in line):
            assert "Refused:" in line, f"{code} rendered outside a refusal: {line}"


def test_every_cloud_model_in_the_catalog_has_a_row(generated: dict[str, str]) -> None:
    table = generated["provider-speakers.md"]
    models = [
        (entry["display_name"], model["id"])
        for provider, entry in ASR_PROVIDER_CATALOG.items()
        if provider != "local"
        for model in entry.get("models", [])
    ]
    assert models, "the catalog lists no cloud models"
    for display_name, model_id in models:
        assert f"| {display_name} |" in table and f"(`{model_id}`)" in table, model_id


def test_the_page_imports_every_partial() -> None:
    page = PAGE.read_text(encoding="utf-8")
    imported = {name for name in gen.GENERATORS if f"from './_generated/{name}'" in page}
    assert imported == {"images.md", "provider-speakers.md", "validity-matrix.md"}
