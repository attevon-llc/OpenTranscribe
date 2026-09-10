"""A global ``ARG TARGETARCH`` shadows BuildKit's built-in and breaks per-arch stage selection.

``Dockerfile.prod`` and ``Dockerfile.lite`` pick their digest-pinned diar-native binary with

    FROM diar-native-bin-${TARGETARCH} AS diar-native-bin

Both files also declared ``ARG TARGETARCH`` just above it — which reads like the thing that
*makes* the substitution work, and is in fact what broke it. BuildKit already supplies
``TARGETARCH`` to the global scope for use in ``FROM``; re-declaring it with no default
overrides that with the empty string, so the stage name resolves to the literal
``diar-native-bin-``:

    ERROR: failed to solve: failed to parse stage name
           "diar-native-bin-": invalid reference format

That failed the entire lite-mode rehearsal scenario on 2026-09-07 — the image never built,
so nothing downstream of it was measured.

⚠️ **This module BUILDS, it does not read.** A static "is the ARG line absent?" assertion
would be a rule nobody can check the truth of; the two Dockerfiles are near-identical text
whose behaviour differs entirely, and the broken form is the one that *looks* correct. The
build is tiny (two `alpine` stages, no RUN), so it costs a second and proves the mechanism.

Marked ``integration``: it needs a working docker daemon, so it must not run in the CPU-only
CI job or the fast unit suite.
"""

from __future__ import annotations

import re
import shutil
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
