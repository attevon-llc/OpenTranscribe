"""Every prod image must carry GIT_SHA, and every build path must pass it (S6v3).

The prod LITE backend reported GIT_SHA 'unknown' because ``opentr.sh start prod --build --lite``
built only the full backend; the lite image the stack runs was never built with the arg.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILES = {
    "backend-full": ROOT / "backend/Dockerfile.prod",
    "backend-lite": ROOT / "backend/Dockerfile.lite",
    "frontend": ROOT / "frontend/Dockerfile.prod",
    "docs": ROOT / "docs-site/Dockerfile",
}
LOCAL = (ROOT / "scripts/lib/prod-images.sh").read_text()
PUSH = (ROOT / "scripts/docker-build-push.sh").read_text()


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_dockerfile_declares_git_sha(name):
    assert re.search(r"^ARG GIT_SHA\b", DOCKERFILES[name].read_text(), re.M), name


@pytest.mark.parametrize(
    "dockerfile",
    [
        "backend/Dockerfile.prod",
        "backend/Dockerfile.lite",
        "frontend/Dockerfile.prod",
        "docs-site/Dockerfile",
    ],
)
def test_local_build_passes_git_sha_for_every_dockerfile(dockerfile):
    """Each `docker build` in build_prod_images that names a Dockerfile passes GIT_SHA."""
    base = dockerfile.split("/")[0]
    blocks = [b for b in re.split(r"\n\s*\n", LOCAL) if "docker build" in b]
    hit = [b for b in blocks if dockerfile in b or (base == "docs-site" and "docs-site" in b)]
    assert hit, f"no local build for {dockerfile}"
    assert all("GIT_SHA=" in b for b in hit), dockerfile


def test_local_lite_build_is_conditional_on_lite_flag():
    assert "-f backend/Dockerfile.lite" in LOCAL
    assert 'build_prod_images "$LITE_FLAG"' in (ROOT / "opentr.sh").read_text()


def test_release_docs_build_passes_git_sha():
    match = re.search(r"^build_docs\(\) \{\n.*?^\}\n", PUSH, re.S | re.M)
    assert match, "build_docs not found"
    assert "GIT_SHA=" in match.group(0)
