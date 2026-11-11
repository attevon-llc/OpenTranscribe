"""Real docker builds of the ``FROM <stage>-${TARGETARCH}`` selection the Dockerfiles use.

``Dockerfile.prod`` and ``Dockerfile.lite`` take their digest-pinned diar-native binary from

    FROM diar-native-bin-${TARGETARCH} AS diar-native-bin

The property that matters is that each target platform gets ITS OWN stage: an arm64 image
must carry the arm64 binary. v0.5.0's rehearsal broke on the opposite failure: a bare global
``ARG TARGETARCH`` shadowed BuildKit's built-in with an empty string on the BuildKit of the
day, the stage name resolved to ``diar-native-bin-``, and the image never built (bf32bdb69).

The first version of this module asserted that the shadowing form FAILS to build. That is a
fact about one BuildKit release, not about our images: on this host (Docker 29.9, BuildKit
0.34, buildx 0.38) the same Dockerfile builds and selects the correct stage for both
platforms, so the assertion went red with nothing wrong. Docker's reference documents the
automatic platform ARGs as "defined in the global scope" ("Automatic platform ARGs in the
global scope", Dockerfile reference); what an argument-less global redeclaration does to them
has varied by release, which is why ``unit/test_dockerfile_targetarch_stage_selection.py``
keeps forbidding the bare ARG statically: a user's or a remote builder's BuildKit may still be
one that breaks.

So these builds check the property itself, per platform, on whatever Docker runs them:

* the form the Dockerfiles use selects the matching stage for amd64 AND arm64;
* the shadowing form never selects the WRONG stage: it either fails on the empty stage name
  (older BuildKit) or selects correctly (this one). A silent wrong pick would be the worst
  case, a published arm64 image carrying an amd64 binary;
* a must-fire control: a Dockerfile that ignores TARGETARCH is caught by the same check, so
  the check cannot pass by reading the wrong thing.

Each stage is ``FROM scratch`` plus a ``LABEL`` naming it, so nothing is pulled or executed
(no QEMU for the arm64 build) and the chosen stage is read back from the image config.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

requires_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available on this host"
)

PLATFORMS = ("amd64", "arm64")

_STAGES = """\
FROM scratch AS base-amd64
LABEL ot.stage=amd64
FROM scratch AS base-arm64
LABEL ot.stage=arm64
"""

#: The form both Dockerfiles use: no global ARG, BuildKit's built-in TARGETARCH.
_DOCKERFILES_FORM = _STAGES + "FROM base-${TARGETARCH} AS chosen\n"

#: The v0.5.0 form: a bare global ARG above the stages.
_SHADOWING_FORM = "ARG TARGETARCH\n" + _STAGES + "FROM base-${TARGETARCH} AS chosen\n"

#: Control: a selection that ignores the target platform entirely.
_IGNORES_TARGETARCH = _STAGES + "FROM base-amd64 AS chosen\n"


@pytest.fixture
def build_run_label():
    """A per-test label value; every image the test builds carries it, and only those go.

    A successful ``docker build`` leaves an untagged image behind, which nothing else
    removes and which is not ours to find by name. Labelling the build and removing by that
    label (plus the sweep's ``ot-test-artifact`` marker) scopes the cleanup to exactly this
    test's output: never ``docker image prune``, never a bare image name.
    """
    run_label = uuid.uuid4().hex[:12]
    try:
        yield run_label
    finally:
        listed = subprocess.run(
            ["docker", "images", "--all", "-q", "--filter", f"label=ot-test-build={run_label}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        image_ids = sorted(set(listed.stdout.split()))
        if image_ids:
            subprocess.run(
                ["docker", "rmi", "--force", *image_ids],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )


def _build(
    tmp_path: Path, dockerfile_text: str, platform: str, run_label: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    dockerfile = tmp_path / f"Dockerfile.{platform}"
    dockerfile.write_text(dockerfile_text, encoding="utf-8")
    iidfile = tmp_path / f"iid.{platform}"
    result = subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            f"linux/{platform}",
            "--label",
            "ot-test-artifact=1",
            "--label",
            f"ot-test-build={run_label}",
            "--iidfile",
            str(iidfile),
            "-f",
            str(dockerfile),
            ".",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return result, iidfile


def _chosen_stage(iidfile: Path) -> tuple[str, str]:
    """``(stage label, image architecture)`` of the image a build wrote to ``iidfile``."""
    image_id = iidfile.read_text(encoding="utf-8").strip()
    inspected = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            image_id,
            "--format",
            '{{index .Config.Labels "ot.stage"}} {{.Architecture}}',
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    stage, arch = inspected.stdout.split()
    return stage, arch


@requires_docker
@pytest.mark.integration
@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_dockerfiles_form_selects_the_stage_of_each_target_platform(
    tmp_path: Path, build_run_label: str, platform: str
):
    result, iidfile = _build(tmp_path, _DOCKERFILES_FORM, platform, build_run_label)
    assert result.returncode == 0, (
        "the form Dockerfile.prod/.lite use does not build on this docker/BuildKit:\n"
        f"{result.stderr}"
    )
    assert _chosen_stage(iidfile) == (platform, platform), (
        f"a linux/{platform} build took another platform's stage: the image would ship "
        "the wrong diar-native binary"
    )


@requires_docker
@pytest.mark.integration
@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_shadowing_form_never_silently_selects_the_wrong_stage(
    tmp_path: Path, build_run_label: str, platform: str
):
    """Fails loudly (older BuildKit) or selects correctly (newer); never a wrong image."""
    result, iidfile = _build(tmp_path, _SHADOWING_FORM, platform, build_run_label)
    if result.returncode != 0:
        assert "base-" in result.stderr, (
            f"the shadowing form failed for a reason other than the empty stage name:\n"
            f"{result.stderr}"
        )
        return
    assert _chosen_stage(iidfile) == (platform, platform), (
        f"a global `ARG TARGETARCH` built a linux/{platform} image from another platform's stage"
    )


@requires_docker
@pytest.mark.integration
def test_the_stage_check_catches_a_selection_that_ignores_the_platform(
    tmp_path: Path, build_run_label: str
):
    """Must-fire control: an arm64 build of a hardcoded amd64 stage is reported as such."""
    result, iidfile = _build(tmp_path, _IGNORES_TARGETARCH, "arm64", build_run_label)
    assert result.returncode == 0, result.stderr
    assert _chosen_stage(iidfile) == ("amd64", "arm64")
    # The cleanup in `build_run_label` removes by this label; if the build stopped carrying
    # it, the finally would silently clean nothing and the images would dangle.
    labelled = subprocess.run(
        ["docker", "images", "--all", "-q", "--filter", f"label=ot-test-build={build_run_label}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert labelled.stdout.split(), "the built image does not carry the cleanup label"
