"""The /architecture diagrams must be rendered from the specs as committed (issue #1230).

``scripts/check-architecture-diagram-drift.py`` compares the ``spec-sha256`` stamp that
``scripts/generate-architecture-diagrams.sh`` writes into each rendered HTML with the spec's
current hash. These tests run it against the real tree, prove each failure shape fires on a
scratch copy, and pin the wiring (generator stamps, pre-commit runs the check).
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO_ROOT / "scripts" / "check-architecture-diagram-drift.py"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate-architecture-diagrams.sh"
SPEC_DIR = REPO_ROOT / "docs-site" / "architecture-diagrams" / "specs"
HTML_DIR = REPO_ROOT / "docs-site" / "static" / "architecture"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("architecture_drift", CHECKER_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {CHECKER_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


@pytest.fixture
def scratch(tmp_path: Path) -> tuple[Path, Path]:
    """A copy of the real specs and renders, safe to damage."""
    spec_dir, html_dir = tmp_path / "specs", tmp_path / "html"
    shutil.copytree(SPEC_DIR, spec_dir)
    shutil.copytree(HTML_DIR, html_dir)
    return spec_dir, html_dir


def test_committed_diagrams_match_their_specs() -> None:
    assert checker.find_drift() == []


def test_every_spec_has_a_render_and_the_tree_is_not_empty() -> None:
    specs = sorted(SPEC_DIR.glob("*.json"))
    assert len(specs) >= 10, "the spec directory lost diagrams"
    for spec in specs:
        html = HTML_DIR / f"{checker.spec_name(spec)}.html"
        assert checker.stamp_of(html) == hashlib.sha256(spec.read_bytes()).hexdigest()


def test_scratch_copy_stays_clean(scratch: tuple[Path, Path]) -> None:
    assert checker.find_drift(*scratch) == []


def test_an_edited_spec_fires(scratch: tuple[Path, Path]) -> None:
    spec_dir, html_dir = scratch
    spec = spec_dir / "transcription-pipeline.workflow.json"
    spec.write_text(spec.read_text(encoding="utf-8").replace("Postprocess", "Post-process"))
    problems = checker.find_drift(spec_dir, html_dir)
    assert len(problems) == 1
    assert problems[0].startswith("transcription-pipeline:")
    assert "changed since" in problems[0]


def test_a_render_without_a_stamp_fires(scratch: tuple[Path, Path]) -> None:
    spec_dir, html_dir = scratch
    html = html_dir / "cloud-asr.html"
    html.write_text(checker.STAMP.sub("", html.read_text(encoding="utf-8")), encoding="utf-8")
    problems = checker.find_drift(spec_dir, html_dir)
    assert problems == [
        "cloud-asr: cloud-asr.html carries no spec-sha256 stamp; "
        "regenerate with scripts/generate-architecture-diagrams.sh <name>"
    ]


def test_a_missing_render_and_an_orphan_render_both_fire(scratch: tuple[Path, Path]) -> None:
    spec_dir, html_dir = scratch
    (html_dir / "rag-chat.html").rename(html_dir / "rag-chat-old.html")
    problems = checker.find_drift(spec_dir, html_dir)
    assert len(problems) == 2
    assert problems[0].startswith("rag-chat: no rendered HTML")
    assert problems[1].startswith("rag-chat-old: rag-chat-old.html has no spec")


def test_selftest_cases_all_behave() -> None:
    assert checker.selftest() == []


def test_the_generator_stamps_what_the_checker_reads() -> None:
    script = GENERATOR_PATH.read_text(encoding="utf-8")
    assert "<!-- spec-sha256: %s -->" in script
    assert checker.STAMP.fullmatch("<!-- spec-sha256: " + "a" * 64 + " -->")


def test_pre_commit_runs_the_check_on_both_directories() -> None:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo.get("hooks", [])]
    hook = next(h for h in hooks if h["id"] == "architecture-diagram-drift")
    assert "scripts/check-architecture-diagram-drift.py" in hook["entry"]
    files = hook["files"]
    for path in (
        "docs-site/architecture-diagrams/specs/cloud-asr.workflow.json",
        "docs-site/static/architecture/cloud-asr.html",
        "scripts/check-architecture-diagram-drift.py",
        "scripts/generate-architecture-diagrams.sh",
    ):
        assert re.search(files, path), f"hook does not fire for {path}"
