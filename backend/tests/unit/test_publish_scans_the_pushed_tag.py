"""The publish-time security scan must examine the tag it just pushed.

Found during the real v0.5.0 publish (2026-09-14). `docker-build-push.sh`'s post-push
scan resolved its target as `:latest` unless `SCAN_SOURCE=local`. But
`scripts/release/80-publish.sh` runs it with **`PUSH_LATEST=false`** — `:latest` is
moved later, by the promote stage — so in push mode `:latest` is never the release
being published. It is one of two wrong things:

* the **PREVIOUS** release, for `backend`/`frontend`/`docs`, which already carry a
  `:latest` from v0.4.1 — the scan reads that image and files the report under the new
  version. Silent, and indistinguishable from a real result.
* **absent**, for `lite`, published for the first time in v0.5.0 — the registry returns
  `manifest for ...-lite:latest not found`, the scan reports COULD NOT SCAN, and the
  stage refuses.

Only the second is loud, and it is the only reason anyone looked. Had `lite` shipped in
an earlier release, v0.5.0 would have published four images whose "security scan" had
examined v0.4.1 — exactly issue #414 ("it scanned the wrong image"), which was fixed in
`scripts/release/50-scan.sh` and left live on this path.

This is a static test on purpose: reproducing it for real needs a registry push, and the
property — which tag the scan is pointed at — is decidable from the source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_PUSH = REPO_ROOT / "scripts" / "docker-build-push.sh"
PUBLISH_STAGE = REPO_ROOT / "scripts" / "release" / "80-publish.sh"

#: The guard that decides the scan target. Must admit push mode, not only local.
_SCAN_TAG_GUARD = re.compile(
    r'if\s+\[\s*"\$\{SCAN_SOURCE:-registry\}"\s*=\s*"local"\s*\]'
    r'.*?BUILD_MODE.*?=\s*"push"',
    re.DOTALL,
)


def test_the_scripts_exist():
    """GUARD THE GUARD: a moved file would make every check below vacuous."""
    assert BUILD_PUSH.is_file(), f"missing {BUILD_PUSH}"
    assert PUBLISH_STAGE.is_file(), f"missing {PUBLISH_STAGE}"


def test_publish_still_withholds_latest():
    """The precondition that makes this bug possible — pinned so the reasoning stays valid.

    If publish ever started pushing `:latest` itself, scanning `:latest` would no longer
    be wrong, and the guard below could be relaxed. Until then it is load-bearing.
    """
    source = PUBLISH_STAGE.read_text(encoding="utf-8")
    assert "PUSH_LATEST=false" in source, (
        "80-publish.sh no longer sets PUSH_LATEST=false. Re-derive whether the "
        "publish-time scan should still target the version tag — this test's premise "
        "is that `:latest` is NOT what publish pushes."
    )


def test_push_mode_scans_the_version_it_pushed_not_latest():
    """THE property. In push mode the scan target must be the version tag."""
    source = BUILD_PUSH.read_text(encoding="utf-8")

    assert _SCAN_TAG_GUARD.search(source), (
        "the publish-time scan target is chosen without considering BUILD_MODE=push, so "
        "it falls back to `:latest` — which publish never pushes. That scans the PREVIOUS "
        "release (or nothing at all) and reports it as this one's."
    )


def test_the_guard_detector_can_actually_fire():
    """GUARD THE GUARD: prove the pattern rejects the pre-fix form.

    The broken version is reproduced verbatim in shape. If it matched, the check above
    could never have failed on the code it was written for.
    """
    broken = """
    local scan_tag="latest"
    if [ "${SCAN_SOURCE:-registry}" = "local" ]; then
        scan_tag="${VERSION_FULL}"
    fi
    """
    fixed = """
    local scan_tag="latest"
    if [ "${SCAN_SOURCE:-registry}" = "local" ] || [ "${BUILD_MODE}" = "push" ]; then
        scan_tag="${VERSION_FULL}"
    fi
    """
    assert _SCAN_TAG_GUARD.search(broken) is None
    assert _SCAN_TAG_GUARD.search(fixed) is not None


def test_the_scan_stage_still_passes_an_explicit_tag():
    """The sibling path that was already correct must stay that way.

    `50-scan.sh` passing `IMAGE_TAG="$VERSION"` is why the dedicated scan stage was
    unaffected by #414 — and why this defect stayed hidden on the publish path.
    """
    source = (REPO_ROOT / "scripts" / "release" / "50-scan.sh").read_text(encoding="utf-8")
    assert 'IMAGE_TAG="$VERSION"' in source, (
        "50-scan.sh no longer pins IMAGE_TAG to the version under test, so the scan "
        "stage would inherit security-scan.sh's `latest` default (issue #414)"
    )
