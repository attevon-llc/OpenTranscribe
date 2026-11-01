"""The bundled object store must be a pullable, digest-pinned image, never upstream MinIO's.

History: MinIO **removed** `minio/minio` and `minio/mc` from Docker Hub (a removal, not a rate
limit), v0.5.0 repointed to quay.io, and on 2026-10-31 the v0.6.0 rehearsal found quay.io's
`minio/minio` refusing anonymous pulls too (`unauthorized`; `quay.io/prometheus/...` still pulled,
so it was MinIO specifically). The repository on GitHub is archived and the last community release
line carries published HIGH advisories that were fixed only in the commercial product. v0.6.0
therefore bundles **Silo** (PGSTY's AGPL fork of MinIO) pinned by digest; see
``docs/design/s3_object_store_alternatives_v060.md`` and ``docs/design/s3_object_store_adoption_survey.md``.

The blast radius of an unpullable image is every NEW user: a host with a warm cache keeps working
until someone prunes, so it is invisible in development and fatal to a fresh install. The
rehearsal's fresh-install scenario is what caught it twice; nothing else in the pipeline pulls.

This test is deliberately OFFLINE. A test that pulls would be slow, would fail in CI's sandbox for
the wrong reason, and would turn a registry outage into a red gate. What it pins is what is under
our control: nothing asks a dead MinIO registry for an image, and the image we do ask for is pinned
by digest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Upstream MinIO image references that no longer resolve for an anonymous pull.
_DEAD_MINIO = re.compile(r"^\s*image:\s*[\"']?((?:docker\.io/|quay\.io/)?minio/[^\s\"']+)")

_PINNED_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _compose_files() -> list[Path]:
    return sorted(REPO_ROOT.glob("docker-compose*.yml"))


def _minio_service_image() -> str:
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose.split("  minio:\n", 1)[1].split("\n  redis:", 1)[0]
    images = re.findall(r"^\s*image:\s*(\S+)", block, re.MULTILINE)
    assert len(images) == 1, f"expected exactly one image in the minio service, got {images}"
    return str(images[0])


def test_there_are_compose_files_to_scan():
    """GUARD THE GUARD: a glob that matches nothing would pass every test below."""
    assert _compose_files(), f"no docker-compose*.yml found under {REPO_ROOT}"


def test_no_compose_service_pulls_upstream_minio():
    """THE property. An upstream `minio/...` image is an unpullable image."""
    offenders: list[str] = []
    for path in _compose_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # the explanatory comment names the dead path on purpose
            match = _DEAD_MINIO.match(line)
            if match:
                offenders.append(f"{path.name}:{lineno} -> {match.group(1)}")

    assert not offenders, (
        "these resolve to registries where MinIO no longer serves anonymous pulls, so a fresh "
        f"install cannot pull them: {offenders}. Use the digest-pinned Silo image."
    )


def test_the_detector_actually_fires_on_the_shape_it_guards():
    """Without this, a typo in the regex makes the test above vacuously green."""
    assert _DEAD_MINIO.match("    image: minio/minio:RELEASE.2025-09-07T16-13-09Z")
    assert _DEAD_MINIO.match('    image: "minio/mc:latest"')
    assert _DEAD_MINIO.match("    image: quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z")
    assert _DEAD_MINIO.match("    image: docker.io/minio/minio:latest")
    assert _DEAD_MINIO.match("    image: docker.io/pgsty/silo:RELEASE.x@sha256:" + "a" * 64) is None


def test_the_object_store_is_pinned_by_digest_not_floating():
    """A tag alone can be re-pointed or deleted; the digest is the artifact.

    Both of this service's earlier images disappeared from under us, so a floating or tag-only
    reference swaps one supply-chain problem for another that fails silently and differently on
    every host.
    """
    image = _minio_service_image()

    assert _PINNED_DIGEST.search(image), f"{image} is not pinned by @sha256 digest"
    assert ":latest" not in image, f"{image} floats on :latest"
    assert "RELEASE." in image, f"{image} carries no release tag beside the digest"
