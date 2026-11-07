"""`./opentr.sh fresh-list --names` prints bare deployment names, one per line (issue #1370).

It must come from the same overlay loop as the human listing and carry nothing else, because
`scripts/release-tidy.sh` feeds each line to `fresh-destroy`. The real function body is
extracted from opentr.sh and driven against a fake `.fresh` directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

OPENTR = Path(__file__).resolve().parents[3] / "opentr.sh"

pytestmark = pytest.mark.skipif(not OPENTR.exists(), reason="opentr.sh not present")


def _function_body(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _fresh_list(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    text = OPENTR.read_text(encoding="utf-8")
    parts = [
        # `docker` must never be reached by --names; make any call fail loudly.
        "docker() { echo DOCKER-WAS-CALLED; return 9; }",
        _function_body(text, "fresh_sanitize_name"),
        _function_body(text, "fresh_project_name"),
        _function_body(text, "fresh_read_offset"),
        _function_body(text, "fresh_list"),
        f'FRESH_OVERLAY_DIR="{tmp_path}"',
        f"fresh_list {' '.join(args)}",
    ]
    return subprocess.run(
        ["bash", "-c", "\n".join(parts)], cwd=tmp_path, capture_output=True, text=True
    )


@pytest.mark.unit
def test_names_lists_each_overlay_and_nothing_else(tmp_path: Path) -> None:
    for name in ("rel-0-7-0", "mine"):
        (tmp_path / f"{name}.yml").write_text("services: {}\n")
    # Stale / sibling overlays that are not deployments.
    (tmp_path / "mine-ports.yml").write_text("x: 1\n")
    (tmp_path / "mine-baked.yml").write_text("x: 1\n")

    proc = _fresh_list(tmp_path, "--names")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["mine", "rel-0-7-0"]


@pytest.mark.unit
def test_names_is_empty_when_there_are_no_deployments(tmp_path: Path) -> None:
    proc = _fresh_list(tmp_path, "--names")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


@pytest.mark.unit
def test_the_human_listing_is_unchanged_without_the_flag(tmp_path: Path) -> None:
    (tmp_path / "mine.yml").write_text("services: {}\n")

    proc = _fresh_list(tmp_path)

    assert "Fresh deployments:" in proc.stdout
    assert "• mine" in proc.stdout
