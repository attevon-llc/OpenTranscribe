"""RELEASE_HOST_ARCH_ONLY: the build stage may skip non-host legs as a RECORDED exception,
and publish/promote must refuse the result.

Drives the REAL ``40-build.sh``, ``80-publish.sh`` and ``90-promote.sh`` (copied into a scratch
repo beside the real ``criteria-lib.sh``) against a fake ``docker`` and fake
``docker-build-push.sh`` / ``security-scan.sh`` producers, so the control flow, the ledger
marker and the criteria statuses are the real ones.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # runs the repo's own release scripts in a scratch tree  # nosec B404
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RELEASE = ROOT / "scripts" / "release"

pytestmark = pytest.mark.skipif(
    not (RELEASE / "40-build.sh").exists(), reason="scripts/release/40-build.sh missing"
)

_CRITERIA = """
version: 2
stages:
  build:
    criteria:
      - {id: platform-table-readable, severity: blocking}
      - {id: every-declared-leg-built, severity: blocking}
      - {id: bare-tag-is-host-arch, severity: blocking}
      - {id: leg-image-ids-recorded, severity: blocking}
      - {id: baked-version-host-leg, severity: blocking}
      - {id: host-arch-leg-present, severity: warn}
  publish:
    criteria:
      - {id: remote-builder-available, severity: blocking}
  promote:
    criteria:
      - {id: published-repo-list-derived, severity: blocking}
"""

_FAKE_DOCKER = r"""#!/bin/bash
case "$1" in
  version) echo linux/amd64 ;;
  tag) exit 0 ;;
  run) echo "docker run must not be used to probe the baked version" >> "$FAKE_LOG"; exit 1 ;;
  image)
    tag="$3"; arch="${tag##*-}"
    case "$arch" in amd64|arm64) ;; *) arch=amd64 ;; esac
    case "$*" in
      *Config.Env*) echo "PATH=/usr/bin"; echo "APP_VERSION=$FAKE_VERSION" ;;
      *Architecture*) echo "$arch" ;;
      *) echo "sha256:id-$tag" ;;
    esac ;;
  buildx) echo "buildx must not be reached" >> "$FAKE_LOG"; exit 1 ;;
esac
exit 0
"""

_FAKE_BUILD_PUSH = r"""#!/bin/bash
if [ "$1" = "list-platforms" ]; then
  printf 'backend\tcuda\tlinux/amd64,linux/arm64\nlite\tcpu\tlinux/amd64,linux/arm64\n'
  exit 0
fi
echo "BUILD $1 platforms=$PLATFORMS remote=$USE_REMOTE_BUILDER" >> "$FAKE_LOG"
exit 0
"""

_FAKE_SCAN = r"""#!/bin/bash
printf 'backend\tu/opentranscribe-backend\nlite\tu/opentranscribe-backend-lite\n'
"""

VERSION = "9.9.9"


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts" / "release").mkdir(parents=True)
    for name in ("40-build.sh", "80-publish.sh", "90-promote.sh", "criteria-lib.sh"):
        shutil.copy(RELEASE / name, repo / "scripts" / "release" / name)
    (repo / "scripts" / "release" / "release-criteria.yaml").write_text(_CRITERIA)
    for name, body in (
        ("docker-build-push.sh", _FAKE_BUILD_PUSH),
        ("security-scan.sh", _FAKE_SCAN),
    ):
        (repo / "scripts" / name).write_text(body)
        (repo / "scripts" / name).chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(_FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    return repo


def _run(repo: Path, stage: str, *, host_only: bool = False) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{repo.parent / 'bin'}:/usr/bin:/bin",
        "FAKE_LOG": str(repo.parent / "calls.log"),
        "FAKE_VERSION": VERSION,
        "JSON_OUT": "true",
        "HOME": str(repo.parent),
    }
    if host_only:
        env["RELEASE_HOST_ARCH_ONLY"] = "1"
    return subprocess.run(  # fixed argv in a scratch tree  # nosec B603 B607
        ["bash", str(repo / "scripts" / "release" / stage), VERSION],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
        cwd=repo,
    )


def _criteria(proc: subprocess.CompletedProcess) -> dict[str, dict]:
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return {c["id"]: c for c in payload["criteria"]}


def _calls(repo: Path) -> str:
    log = repo.parent / "calls.log"
    return log.read_text() if log.exists() else ""


@pytest.mark.unit
def test_host_only_skips_remote_legs_and_records_an_exception(scratch: Path) -> None:
    proc = _run(scratch, "40-build.sh", host_only=True)
    assert proc.returncode == 0, proc.stderr
    calls = _calls(scratch)
    assert "platforms=linux/amd64" in calls
    assert "arm64" not in calls

    legs = (scratch / ".release" / VERSION / "legs.tsv").read_text()
    assert "linux/amd64" in legs and "arm64" not in legs

    declared = _criteria(proc)["every-declared-leg-built"]
    assert declared["outcome"] != "pass"
    assert declared["status"] == "warn"
    assert "host-arch-only: 2 non-host legs not built" in declared["detail"]
    assert (scratch / ".release" / VERSION / "host-arch-only").exists()


@pytest.mark.unit
def test_without_the_flag_every_leg_is_built_and_recorded_as_pass(scratch: Path) -> None:
    marker = scratch / ".release" / VERSION / "host-arch-only"
    marker.parent.mkdir(parents=True)
    marker.write_text("stale marker from an earlier host-only build")

    proc = _run(scratch, "40-build.sh")
    assert proc.returncode == 0, proc.stderr
    assert "platforms=linux/arm64" in _calls(scratch)
    assert _criteria(proc)["every-declared-leg-built"]["outcome"] == "pass"
    legs = (scratch / ".release" / VERSION / "legs.tsv").read_text()
    assert "linux/arm64" in legs
    assert not marker.exists()


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["80-publish.sh", "90-promote.sh"])
def test_publish_and_promote_refuse_a_host_only_build(scratch: Path, stage: str) -> None:
    assert _run(scratch, "40-build.sh", host_only=True).returncode == 0
    proc = _run(scratch, stage)
    assert proc.returncode == 3
    assert "host-arch-only" in proc.stderr
    assert "buildx must not be reached" not in _calls(scratch)


@pytest.mark.unit
def test_publish_is_not_refused_after_a_full_build(scratch: Path) -> None:
    assert _run(scratch, "40-build.sh").returncode == 0
    proc = _run(scratch, "80-publish.sh")
    assert "host-arch-only" not in proc.stderr
