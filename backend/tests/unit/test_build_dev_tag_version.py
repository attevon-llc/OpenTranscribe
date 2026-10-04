"""`docker-build-push.sh` accepts a throwaway `dev-<git-sha>` version, and it never moves `:latest`.

WHY

Testing an unreleased commit against a downstream consumer needs an image tagged with that
commit, without cutting a release. The script only accepted `vX.Y.Z`, so a `dev-<sha>` build
needed an uncommitted local edit. And `PUSH_LATEST` defaults to `true`: a dev push that
forgot `PUSH_LATEST=false` would repoint `:latest` — what every fresh install pulls — at an
unreleased build.

APPROACH

The `# --- BEGIN/END version-validate ---` block is extracted from the REAL shipped script and
run in bash with stub `print_*` helpers, so the regexes and control flow under test are the
shipped ones.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PUSH_SH = REPO_ROOT / "scripts" / "docker-build-push.sh"

pytestmark = pytest.mark.skipif(
    not PUSH_SH.exists(), reason="scripts/docker-build-push.sh not in this checkout"
)


def _block() -> str:
    text = PUSH_SH.read_text(encoding="utf-8")
    start = text.index("# --- BEGIN version-validate ---")
    end = text.index("# --- END version-validate ---")
    return text[start:end]


def _run(semver: str, push_latest: str = "true") -> subprocess.CompletedProcess[str]:
    script = (
        'print_error() { echo "ERROR: $*" >&2; }\n'
        'print_warning() { echo "WARN: $*" >&2; }\n'
        f"SEMVER='{semver}'\nPUSH_LATEST='{push_latest}'\n"
        f"{_block()}\n"
        'echo "SEMVER=${SEMVER} PUSH_LATEST=${PUSH_LATEST}"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("tag", ["dev-0ea15b4b", "dev-abc1234", "dev-" + "a" * 40])
def test_dev_tag_is_accepted_verbatim_and_never_moves_latest(tag: str) -> None:
    result = _run(tag)
    assert result.returncode == 0, result.stderr
    assert f"SEMVER={tag} PUSH_LATEST=false" in result.stdout
    assert ":latest is release-only" in result.stderr


def test_dev_tag_with_latest_already_off_stays_quiet() -> None:
    result = _run("dev-0ea15b4b", push_latest="false")
    assert result.returncode == 0
    assert "PUSH_LATEST=false" in result.stdout
    assert "WARN" not in result.stderr


@pytest.mark.parametrize(("given", "expected"), [("v1.2.3", "v1.2.3"), ("1.2.3", "v1.2.3")])
def test_release_versions_are_unchanged(given: str, expected: str) -> None:
    result = _run(given)
    assert result.returncode == 0, result.stderr
    assert f"SEMVER={expected} PUSH_LATEST=true" in result.stdout


@pytest.mark.parametrize(
    "bad", ["dev-XYZ1234", "dev-abc12", "dev-", "latest", "v1.2", "dev-0ea15b4b-dirty"]
)
def test_invalid_versions_are_rejected(bad: str) -> None:
    result = _run(bad)
    assert result.returncode == 1
    assert "Invalid semantic version format" in result.stderr
