"""A local or dev-tag build must not overwrite the TRACKED release scan evidence.

`BUILD_MODE=local ./scripts/docker-build-push.sh backend` used to write its scan into
`./security-reports`, replacing the committed `backend-amd64-*` files with a throwaway
build's result. The function that now owns the decision is driven directly by sourcing the
script (it guards `main` behind a BASH_SOURCE check), so no docker or build is involved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PUSH_SH = REPO_ROOT / "scripts" / "docker-build-push.sh"

pytestmark = pytest.mark.skipif(not PUSH_SH.exists(), reason="docker-build-push.sh missing")


def _report_dir(build_mode: str, version: str, override: str = "") -> str:
    env_override = f'export SCAN_REPORT_DIR="{override}"; ' if override else ""
    script = (
        f"{env_override}source {PUSH_SH} >/dev/null 2>&1; "
        f'BUILD_MODE="{build_mode}"; VERSION_FULL="{version}"; scan_report_dir'
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    ).stdout.strip()


@pytest.mark.parametrize(
    ("mode", "version"),
    [
        ("local", "v0.7.0"),  # the incident: local build, release-shaped version
        ("local", "dev-0123abc"),
        ("push", "dev-0123abc"),
        ("push", "v0.0.0-rc1"),
    ],
)
def test_non_release_runs_never_target_tracked_evidence(mode, version):
    assert _report_dir(mode, version) == "./.security-reports-local"


def test_push_mode_release_semver_writes_tracked_evidence():
    assert _report_dir("push", "v0.7.0") == "./security-reports"


def test_explicit_override_wins():
    assert _report_dir("local", "v0.7.0", "custom/report-dir") == "custom/report-dir"


def test_local_dir_is_gitignored():
    r = subprocess.run(
        ["git", "check-ignore", "-q", ".security-reports-local/backend-amd64-trivy.txt"],
        cwd=REPO_ROOT,
    )
    assert r.returncode == 0


def test_run_security_scan_passes_the_resolved_dir():
    src = PUSH_SH.read_text(encoding="utf-8")
    assert 'OUTPUT_DIR="$(scan_report_dir)"' in src
    assert 'OUTPUT_DIR="./security-reports"' not in src
