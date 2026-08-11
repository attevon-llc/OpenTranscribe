"""release-manifest.txt must stay truthful.

The manifest is the single list of files a self-hosted deployment downloads. It
replaced two hardcoded lists (one in setup-opentranscribe.sh, one in
opentranscribe.sh's update-full) that had silently drifted apart in both
directions — see the manifest's own header for the two production bugs that
caused.

Consolidating only helps if the manifest itself stays correct, so:

* every listed path must exist in the repo (a typo means a 404 at install time,
  for every user, on the pinned tag)
* every compose overlay opentranscribe.sh can select must be listed (that is the
  exact bug that shipped: get_compose_files() picks docker-compose.blackwell.yml
  on SM_12x hardware, the installer never downloaded it, and the `[ -f ]` guard
  turned it into a silent fallback to the wrong image)
* both consumers must actually read the manifest rather than growing a new
  hardcoded list beside it
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "release-manifest.txt"
MANAGER = REPO_ROOT / "opentranscribe.sh"

pytestmark = pytest.mark.skipif(
    not MANIFEST.exists(), reason="release-manifest.txt not present in this checkout"
)


def _entries() -> list[tuple[str, set[str]]]:
    """Parse the manifest the same way the shell consumers do."""
    entries: list[tuple[str, set[str]]] = []
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        path = parts[0].strip()
        flags = set()
        if len(parts) > 1 and parts[1].strip():
            flags = {f.strip() for f in parts[1].split(",") if f.strip()}
        entries.append((path, flags))
    return entries


def test_manifest_is_not_empty():
    assert _entries(), "release-manifest.txt parsed to zero entries"


def test_every_listed_path_exists():
    """A path that does not exist 404s for every user installing that tag."""
    missing = [path for path, _ in _entries() if not (REPO_ROOT / path).exists()]
    assert not missing, f"release-manifest.txt lists paths that do not exist: {missing}"


def test_flags_are_known():
    known = {"optional", "exec", "preserve"}
    bad = {path: sorted(flags - known) for path, flags in _entries() if not flags <= known}
    assert not bad, f"unknown manifest flags (consumers ignore these silently): {bad}"


def test_base_compose_is_listed_and_required():
    """docker-compose.yml carries the service definitions every overlay merges onto.

    Omitting it from the upgrade path is what made celery-redaction start with the
    image's default CMD instead of the redaction worker.
    """
    entries = dict(_entries())
    assert "docker-compose.yml" in entries, "base compose file missing from the manifest"
    assert "optional" not in entries["docker-compose.yml"], (
        "docker-compose.yml must never be optional — overlays merge onto it"
    )


def test_every_selectable_compose_overlay_is_listed():
    """Any overlay get_compose_files() can choose must be downloadable.

    The `[ -f overlay ]` guards mean a missing overlay degrades silently rather
    than erroring, so this test is the only thing that catches the omission.
    """
    listed = {path for path, _ in _entries()}

    # Comments are prose, not selection logic. opentranscribe.sh explains in a
    # comment that docker-compose.offline.yml sets HF_HUB_OFFLINE=1 — that file is
    # shipped inside the offline package and is never downloaded, so matching it
    # made this test fail on a documentation change.
    code = "\n".join(
        line for line in MANAGER.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    referenced = set(re.findall(r"docker-compose[a-z0-9.-]*\.yml", code))

    unlisted = sorted(referenced - listed)
    assert not unlisted, (
        f"opentranscribe.sh can select {unlisted} but the manifest does not list "
        "them, so a deployment may never download them"
    )


def test_update_full_reads_the_manifest():
    """Guard against someone reintroducing a hardcoded download list."""
    text = MANAGER.read_text()
    assert "release-manifest.txt" in text, (
        "opentranscribe.sh no longer references release-manifest.txt — "
        "did update-full grow its own artifact list again?"
    )


def test_env_example_is_listed_for_new_key_reporting():
    """update-full diffs .env.example against the user's .env to report new keys.

    Settings uses extra="ignore", so a newly required var is silently defaulted
    rather than erroring — reporting it at upgrade time is the only signal.
    """
    assert ".env.example" in {path for path, _ in _entries()}
