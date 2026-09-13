"""No compose service may pull MinIO from Docker Hub — the images are gone.

MinIO **removed** `minio/minio` and `minio/mc` from Docker Hub. This is not a rate
limit and not a transient outage: the Hub API returns `object not found` for the
repository and for our exact pinned tag, while an anonymous pull token is still
issued normally. `docker pull minio/minio` fails with `pull access denied ...
repository does not exist`.

The blast radius is every NEW user. An existing host with a warm image cache keeps
working — right up until someone prunes — so this is invisible in day-to-day
development and fatal to a fresh install. The v0.5.0 release rehearsal's
fresh-install scenario is what caught it; nothing else in the pipeline pulls.

The images are on quay.io, and it is the SAME artifact rather than a substitute:
`quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z` resolves to digest
`sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e`,
byte-identical to the docker.io copy cached on the build host. Verified by pulling
both and comparing, not by assuming two registries agree.

This test is deliberately OFFLINE. A test that pulls would be slow, would fail in
CI's sandbox for the wrong reason, and would turn a registry outage into a red gate.
What it pins is the thing under our control: nothing asks Docker Hub for MinIO.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Docker resolves an unqualified `minio/...` to docker.io, where it no longer exists.
_DOCKER_HUB_MINIO = re.compile(r"^\s*image:\s*[\"']?(minio/[^\s\"']+)")


def _compose_files() -> list[Path]:
    return sorted(REPO_ROOT.glob("docker-compose*.yml"))


def test_there_are_compose_files_to_scan():
    """GUARD THE GUARD: a glob that matches nothing would pass every test below."""
    assert _compose_files(), f"no docker-compose*.yml found under {REPO_ROOT}"


def test_no_compose_service_pulls_minio_from_docker_hub():
    """THE property. An unqualified `minio/...` image is an unpullable image."""
    offenders: list[str] = []
    for path in _compose_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # the explanatory comment names the dead path on purpose
            match = _DOCKER_HUB_MINIO.match(line)
            if match:
                offenders.append(f"{path.name}:{lineno} -> {match.group(1)}")

    assert not offenders, (
        "these resolve to docker.io, where MinIO deleted the repository — a fresh "
        f"install cannot pull them: {offenders}. Use quay.io/minio/... instead."
    )


def test_the_detector_actually_fires_on_the_shape_it_guards():
    """Without this, a typo in the regex makes the test above vacuously green."""
    assert _DOCKER_HUB_MINIO.match("    image: minio/minio:RELEASE.2025-09-07T16-13-09Z")
    assert _DOCKER_HUB_MINIO.match('    image: "minio/mc:latest"')
    # quay-qualified must NOT match, or the fix could never satisfy the test
    assert _DOCKER_HUB_MINIO.match("    image: quay.io/minio/minio:RELEASE.x") is None


def test_the_object_store_is_still_pinned_not_floating():
    """A registry move is not licence to drop the pin.

    Re-pointing a tag while loosening it would swap one supply-chain problem for
    another: `:latest` on a new registry is strictly worse than a dead pin, because
    it fails silently and differently on every host.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    minio_images = re.findall(r"^\s*image:\s*(\S*minio/minio\S*)", compose, re.MULTILINE)
    assert minio_images, "docker-compose.yml declares no minio image at all"
    for image in minio_images:
        assert ":" in image.rsplit("/", 1)[-1], f"{image} carries no tag"
        assert not image.endswith(":latest"), (
            f"{image} floats on :latest — pin the RELEASE tag, as every other image here is"
        )
