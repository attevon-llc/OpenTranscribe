"""`cc_manifest_compose_overlays` must honor the manifest's `optional` flag.

`release-manifest.txt` marks `docker-compose.blackwell.yml` and `docker-compose.backup.yml`
`optional` — a release build is allowed not to carry them. `test-upgrade.sh`'s
`_replay_release_manifest` (the function that stages a deployment's after-tree from the
manifest, mirroring `./opentranscribe.sh update-full`) already honors that: it skips an
optional entry when the source tree does not have the file, and only `gr_die`s on a
NON-optional entry that is missing.

`scripts/release-tests/lib/compose-chain.sh`'s `cc_manifest_compose_overlays` reads the
exact same file to build the list `cc_assert_chain` checks was "downloaded" into a rehearsed
install. It ignored `optional` entirely — every `docker-compose*.yml` line was reported
regardless of the flag. Currently harmless (both optional entries exist in this checkout),
but the day either one is genuinely absent from a release's source tree, this reader would
still demand it, while `_replay_release_manifest` would have silently skipped staging it —
producing a false FAIL for behavior the manifest format itself says is allowed.

Fixed by making `cc_manifest_compose_overlays` skip an `optional` entry when
`$repo_root/$path` does not exist, exactly like `_replay_release_manifest` does for staging.

These tests source the real, unmodified `compose-chain.sh` (a pure function-definition file,
safe to source directly — no top-level side effects) and drive `cc_manifest_compose_overlays`
against a throwaway `repo_root` with a synthetic `release-manifest.txt`, so no real overlay
file or the real manifest is required to be in any particular state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_CHAIN = REPO_ROOT / "scripts" / "release-tests" / "lib" / "compose-chain.sh"

pytestmark = pytest.mark.skipif(
    not COMPOSE_CHAIN.exists(), reason="compose-chain.sh not present in this checkout"
)


def _run(fake_repo: Path) -> list[str]:
    snippet = f"""
set -euo pipefail
source "{COMPOSE_CHAIN}"
cc_manifest_compose_overlays "{fake_repo}"
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def _write_manifest(fake_repo: Path, body: str) -> None:
    fake_repo.mkdir(parents=True, exist_ok=True)
    (fake_repo / "release-manifest.txt").write_text(body)


@pytest.mark.unit
def test_optional_overlay_absent_from_the_source_tree_is_skipped(tmp_path: Path) -> None:
    """The bug: an optional entry the source tree genuinely does not carry must not be
    reported as an overlay the install should have downloaded."""
    fake_repo = tmp_path / "repo"
    _write_manifest(
        fake_repo,
        "docker-compose.yml\ndocker-compose.prod.yml\ndocker-compose.blackwell.yml\toptional\n",
    )
    # Only create the non-optional files — blackwell is declared optional and absent,
    # exactly the "release build does not carry it" case.
    (fake_repo / "docker-compose.yml").touch()
    (fake_repo / "docker-compose.prod.yml").touch()

    overlays = _run(fake_repo)

    assert overlays == ["docker-compose.yml", "docker-compose.prod.yml"], overlays
    assert "docker-compose.blackwell.yml" not in overlays, (
        "an optional overlay absent from the source tree must not be demanded — "
        "this is the exact false-FAIL the fix prevents"
    )


@pytest.mark.unit
def test_optional_overlay_present_is_still_reported(tmp_path: Path) -> None:
    """Control: when an optional overlay DOES exist, it must still be listed — `optional`
    means "may be absent", not "never check it"."""
    fake_repo = tmp_path / "repo"
    _write_manifest(
        fake_repo,
        "docker-compose.yml\ndocker-compose.blackwell.yml\toptional\n",
    )
    (fake_repo / "docker-compose.yml").touch()
    (fake_repo / "docker-compose.blackwell.yml").touch()

    overlays = _run(fake_repo)

    assert overlays == ["docker-compose.yml", "docker-compose.blackwell.yml"], overlays


@pytest.mark.unit
def test_non_optional_overlay_is_always_reported_even_if_absent(tmp_path: Path) -> None:
    """Control: a NON-optional entry must still be reported even when absent — that is a
    real "release-manifest.txt lists it but the tree doesn't have it" bug, not something
    to silently swallow. (cc_assert_chain's own "was downloaded" assertion is what turns
    this into a FAIL; this test only proves the overlay reader still surfaces it.)"""
    fake_repo = tmp_path / "repo"
    _write_manifest(fake_repo, "docker-compose.yml\ndocker-compose.nginx.yml\n")
    (fake_repo / "docker-compose.yml").touch()
    # docker-compose.nginx.yml is deliberately NOT created, and NOT marked optional.

    overlays = _run(fake_repo)

    assert overlays == ["docker-compose.yml", "docker-compose.nginx.yml"], overlays


@pytest.mark.unit
def test_against_the_real_manifest_both_optional_entries_currently_exist(tmp_path: Path) -> None:
    """Sanity check against the actual repo: today's release-manifest.txt has both
    optional entries present on disk, so the fix must not change today's real output."""
    overlays = _run(REPO_ROOT)

    assert "docker-compose.blackwell.yml" in overlays, (
        "docker-compose.blackwell.yml exists in this checkout — it must still be reported"
    )
    assert "docker-compose.backup.yml" in overlays, (
        "docker-compose.backup.yml exists in this checkout — it must still be reported"
    )
