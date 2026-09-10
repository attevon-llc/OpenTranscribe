"""The two REAL docker builds that prove the ``ARG TARGETARCH`` hazard.

Split out of ``tests/unit/test_dockerfile_targetarch_stage_selection.py`` (which keeps the
static checks). These carry ``@pytest.mark.integration`` because they need a working docker
daemon — and an ``integration``-marked module sitting under ``tests/unit/`` is collected by
NEITHER the fast suite (which deselects the marker) NOR the gate's integration phase (which
collects only ``tests/integration/``, ``tests/test_selective_reprocess.py`` and
``tests/eval/``). It ran nowhere.

``tests/unit/test_gate_phase_coverage.py`` caught that, which is exactly what it is for:
a test that cannot run looks identical to one that passes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILES = [
    REPO_ROOT / "backend" / "Dockerfile.prod",
    REPO_ROOT / "backend" / "Dockerfile.lite",
]

# A global `ARG TARGETARCH` — one with no default value, which is the shadowing form.
# `ARG TARGETARCH=something` would supply a value and is not this bug.
_SHADOWING_ARG = re.compile(r"^\s*ARG\s+TARGETARCH\s*$")


def _selects_by_targetarch(path: Path) -> bool:
    return any(
        "${TARGETARCH}" in line and line.lstrip().upper().startswith("FROM")
        for line in path.read_text(encoding="utf-8").splitlines()
    )


# --------------------------------------------------------------------- static (fast suite)


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_no_global_arg_targetarch_shadows_the_builtin(dockerfile: Path):
    if not dockerfile.is_file():
        pytest.skip(f"{dockerfile.name} is not in this checkout")
    if not _selects_by_targetarch(dockerfile):
        pytest.skip(f"{dockerfile.name} no longer selects a stage by TARGETARCH")

    offenders = [
        f"{dockerfile.name}:{n}: {line.strip()}"
        for n, line in enumerate(dockerfile.read_text(encoding="utf-8").splitlines(), 1)
        if _SHADOWING_ARG.match(line)
    ]
    assert not offenders, (
        "a bare `ARG TARGETARCH` shadows BuildKit's built-in with an empty string, so "
        "`FROM diar-native-bin-${TARGETARCH}` resolves to `diar-native-bin-` and the image "
        "cannot build at all:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_the_per_arch_stages_the_selection_needs_still_exist(dockerfile: Path):
    """Non-vacuity: removing the ARG is only right while the two named stages exist."""
    if not dockerfile.is_file():
        pytest.skip(f"{dockerfile.name} is not in this checkout")
    text = dockerfile.read_text(encoding="utf-8")
    if not _selects_by_targetarch(dockerfile):
        pytest.skip(f"{dockerfile.name} no longer selects a stage by TARGETARCH")
    for arch in ("amd64", "arm64"):
        assert f"AS diar-native-bin-{arch}" in text, (
            f"{dockerfile.name} selects by TARGETARCH but defines no "
            f"`diar-native-bin-{arch}` stage — the substitution would resolve to a stage "
            "that does not exist"
        )


# ------------------------------------------------------- behavioural: build both forms


pytestmark_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available on this host"
)

_BROKEN = """\
ARG TARGETARCH
FROM alpine:3.20 AS base-amd64
FROM alpine:3.20 AS base-arm64
FROM base-${TARGETARCH} AS chosen
"""

_FIXED = """\
FROM alpine:3.20 AS base-amd64
FROM alpine:3.20 AS base-arm64
FROM base-${TARGETARCH} AS chosen
"""


def _build(tmp_path: Path, dockerfile_text: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "Dockerfile").write_text(dockerfile_text, encoding="utf-8")
    return subprocess.run(
        # --platform pinned so the test asserts the same thing on an arm64 host; only the
        # amd64 stage is exercised, and both stages are the same tiny image anyway.
        ["docker", "build", "--platform", "linux/amd64", "-f", str(tmp_path / "Dockerfile"), "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytestmark_docker
@pytest.mark.integration
def test_the_shadowing_form_really_does_fail_to_build(tmp_path: Path):
    """The must-fire control. Without it, the fix below could be passing for another reason."""
    result = _build(tmp_path, _BROKEN)
    assert result.returncode != 0, (
        "a global `ARG TARGETARCH` no longer breaks stage selection — BuildKit's behaviour "
        "changed, and this whole module (plus the warnings in both Dockerfiles) should be "
        "re-checked rather than left asserting a hazard that no longer exists"
    )
    assert "base-" in result.stderr, (
        f"the build failed for some reason other than the empty stage name:\n{result.stderr}"
    )


@pytestmark_docker
@pytest.mark.integration
def test_the_form_the_dockerfiles_now_use_builds(tmp_path: Path):
    result = _build(tmp_path, _FIXED)
    assert result.returncode == 0, (
        "omitting the global ARG does not resolve TARGETARCH on this docker/BuildKit "
        f"version, so the Dockerfiles' current form would not build:\n{result.stderr}"
    )
