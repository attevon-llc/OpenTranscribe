"""Every production image must be able to receive OS security patches.

Two independent defects, found by the v0.5.0 release scan (2026-09-13), that
between them shipped 16-19 CRITICAL CVEs in the backend images while frontend and
docs carried **zero**:

* **`Dockerfile.lite` had no `apt-get upgrade` at all.** `Dockerfile.prod` had
  carried one since it was written; this file never did, so the lite image had no
  mechanism to receive Debian security patches — it shipped exactly what
  `python:3.13-slim-trixie` contained at the pinned tag, indefinitely. That matters
  most there: `opentranscribe.sh` defaults arm64 hosts to `DEPLOYMENT_MODE=lite`, so
  `lite-arm64` (19 CRITICAL, the worst leg measured) is the ONLY backend an arm64
  user can install.

* **`Dockerfile.prod`'s upgrade was frozen by the layer cache.** Its comment claimed
  the upgrade ran "on every image build". Docker caches a `RUN` by its command
  string, so once the layer existed it was reused verbatim and the upgrade never ran
  again — the image kept shipping whatever Debian published the day that layer was
  FIRST built. 15 of its 16 CRITICALs were fixable, and every one was an OS package
  (`perl`, `libxml2`, `libglib2.0`, `libmbedcrypto`) rather than anything from our
  own dependency tree.

The fix is a changing `ARG APT_SECURITY_REFRESH` ahead of the RUN, keyed to the build
date by `docker-build-push.sh`. This suite fails on either half regressing, because
each is individually silent: an image with no upgrade and an image whose upgrade is
cache-frozen both build fine, pass every functional test, and differ only in a CVE
report nobody reads until release day.

The Alpine images (`frontend`, `docs-site`) satisfy the same requirement with
`apk upgrade`, and measured 0 CRITICAL — so they are checked with the equivalent
rule rather than exempted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Production Dockerfiles that install OS packages into the shipped image.
#: `blackwell` is built only on request and never published, but is included so the
#: three backend Dockerfiles cannot drift — that drift is exactly how `.lite` became
#: the only one with no upgrade at all.
DEBIAN_IMAGES = [
    Path("backend/Dockerfile.prod"),
    Path("backend/Dockerfile.lite"),
    Path("backend/Dockerfile.blackwell"),
]
ALPINE_IMAGES = [
    Path("frontend/Dockerfile.prod"),
    Path("docs-site/Dockerfile"),
]

_APT_UPGRADE = re.compile(r"apt-get\s+upgrade\s+-y")
_APK_UPGRADE = re.compile(r"apk\s+upgrade")
_REFRESH_ARG = re.compile(r"^\s*ARG\s+APT_SECURITY_REFRESH", re.MULTILINE)


def _read(rel: Path) -> str:
    path = REPO_ROOT / rel
    assert path.is_file(), f"missing production Dockerfile {rel}"
    return path.read_text(encoding="utf-8")


def _executable_lines(source: str) -> str:
    """Drop `#` comments.

    Necessary, not tidiness: these Dockerfiles explain the upgrade in prose directly
    above it, so a raw scan finds `apt-get upgrade -y` in a COMMENT that sits before
    the ARG and reports the ordering as wrong. A detector that cannot tell a
    directive from an explanation would force the explanation to be deleted to stay
    green — precisely backwards.
    """
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


def test_the_dockerfiles_this_scans_exist():
    """GUARD THE GUARD: a missing path would make every check below vacuous."""
    scanned = DEBIAN_IMAGES + ALPINE_IMAGES
    # Asserted OUTSIDE the loop: an empty list would satisfy the per-file checks
    # below while proving nothing — the exact shape this guard exists to catch.
    assert len(scanned) == 5, f"expected 5 production Dockerfiles, got {len(scanned)}"

    missing = [str(rel) for rel in scanned if not (REPO_ROOT / rel).is_file()]
    assert not missing, f"production Dockerfiles missing from the tree: {missing}"


@pytest.mark.parametrize("rel", DEBIAN_IMAGES, ids=lambda p: p.name)
def test_every_debian_image_upgrades_its_os_packages(rel: Path):
    """THE property. Without this, the image can never receive a security patch."""
    source = _read(rel)

    assert _APT_UPGRADE.search(source), (
        f"{rel} installs OS packages but never runs `apt-get upgrade -y`, so it ships "
        "whatever the pinned base image contained and can never pick up a Debian "
        "security advisory. This is the defect that put 19 CRITICAL CVEs in lite-arm64 "
        "— the only backend an arm64 host can install."
    )


@pytest.mark.parametrize("rel", DEBIAN_IMAGES, ids=lambda p: p.name)
def test_the_upgrade_layer_cannot_be_frozen_by_the_build_cache(rel: Path):
    """An upgrade that runs once and never again is barely better than none at all.

    Docker keys a RUN layer on its command string. A changing ARG declared ahead of
    it is what makes a rebuild actually re-run apt.
    """
    source = _read(rel)

    assert _REFRESH_ARG.search(source), (
        f"{rel} has no `ARG APT_SECURITY_REFRESH`, so its `apt-get upgrade` layer is "
        "cached by command string and will never re-run. Dockerfile.prod's comment "
        "claimed the upgrade happened 'on every image build' while this exact caching "
        "held it frozen — 15 of its 16 CRITICALs were fixable OS packages."
    )

    code = _executable_lines(source)
    arg_at = code.index("ARG APT_SECURITY_REFRESH")
    upgrade_match = _APT_UPGRADE.search(code)
    assert upgrade_match is not None, f"{rel}: no executable apt-get upgrade found"
    upgrade_at = upgrade_match.start()
    assert arg_at < upgrade_at, (
        f"{rel} declares APT_SECURITY_REFRESH AFTER the upgrade it is supposed to "
        "invalidate — a build arg only busts layers that follow it"
    )


@pytest.mark.parametrize("rel", ALPINE_IMAGES, ids=lambda p: p.name)
def test_every_alpine_image_upgrades_its_os_packages(rel: Path):
    """Same requirement, different package manager. These measured 0 CRITICAL."""
    source = _read(rel)

    assert _APK_UPGRADE.search(source), (
        f"{rel} never runs `apk upgrade`, so it cannot receive Alpine security fixes"
    )


def test_the_build_script_supplies_a_changing_refresh_value():
    """The ARG is inert unless the builder actually passes a value that moves."""
    source = (REPO_ROOT / "scripts" / "docker-build-push.sh").read_text(encoding="utf-8")

    assert "APT_SECURITY_REFRESH=" in source, (
        "docker-build-push.sh never passes --build-arg APT_SECURITY_REFRESH, so the "
        "Dockerfiles' default is used on every build and the layer stays cached forever"
    )
    assert "date -u" in source, (
        "the refresh value must be derived from the date; a constant would cache "
        "exactly as hard as having no ARG at all"
    )


def test_the_detectors_can_actually_fire():
    """GUARD THE GUARD: prove each pattern distinguishes the broken form.

    Reproduces the pre-fix bodies. If these matched, the checks above could never
    have failed on the code they were written for.
    """
    unpatched = "RUN apt-get update && apt-get install -y --no-install-recommends curl\n"
    assert _APT_UPGRADE.search(unpatched) is None
    assert _REFRESH_ARG.search(unpatched) is None

    patched = (
        "ARG APT_SECURITY_REFRESH=unset\n"
        "RUN apt-get update && apt-get upgrade -y && apt-get install -y curl\n"
    )
    assert _APT_UPGRADE.search(patched)
    assert _REFRESH_ARG.search(patched)

    assert _APK_UPGRADE.search("RUN apk upgrade --no-cache && apk del curl\n")
    assert _APK_UPGRADE.search("RUN apk add --no-cache curl\n") is None
