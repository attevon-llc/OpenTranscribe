"""R5: 50-scan.sh runs components as bounded background jobs and still scores every exit code."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSION = "v9.9.9"

pytestmark = pytest.mark.unit


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    tmp_path: Path, jobs: int, rc_for: dict[str, int]
) -> tuple[subprocess.CompletedProcess[str], float, dict]:
    repo = tmp_path / "repo"
    (repo / "scripts" / "release").mkdir(parents=True)
    for name in ("50-scan.sh", "criteria-lib.sh", "release-criteria.yaml"):
        shutil.copy(REPO_ROOT / "scripts" / "release" / name, repo / "scripts" / "release" / name)
    s = repo / "scripts"
    cases = "".join(f"{c}) rc={rc} ;; " for c, rc in rc_for.items())
    _exe(
        s / "security-scan.sh",
        'if [[ "$1" == list-repos ]]; then printf "a\\tt/a\\nb\\tt/b\\nc\\tt/c\\n"; exit 0; fi\n'
        'if [[ "$1" == pin-dbs ]]; then echo "pinned" >> "$PIN_LOG"; exit 0; fi\n'
        'sleep 1; rc=0; case "$1" in ' + cases + "esac\n"
        'mkdir -p "$OUTPUT_DIR"; printf \'{"ArtifactName":"t/%s:%s"}\' "$1" "$IMAGE_TAG" > "$OUTPUT_DIR/$1-amd64-trivy.json"\n'
        'echo "scan-output-$1"; exit $rc\n',
    )
    _exe(
        s / "docker-build-push.sh",
        "printf 'a\\tcuda\\tlinux/amd64\\nb\\tcuda\\tlinux/amd64\\nc\\tcuda\\tlinux/amd64\\n'\n",
    )
    _exe(s / "check-image-hygiene.sh", "exit 0\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "docker", "exit 0\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "OT_SCAN_DIR": str(tmp_path / "reports"),
        "PIN_LOG": str(tmp_path / "pin.log"),
        "JSON_OUT": "true",
        "SCAN_JOBS": str(jobs),
    }
    t0 = time.monotonic()
    proc = subprocess.run(
        ["bash", "scripts/release/50-scan.sh", VERSION],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    wall = time.monotonic() - t0
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith("{")][-1])
    return proc, wall, {c["id"]: c for c in payload["criteria"]}


def test_components_overlap_and_db_is_pinned_once(tmp_path: Path) -> None:
    proc, wall, crit = _run(tmp_path, 3, {})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert wall < 2.5, f"3 one-second scans took {wall:.1f}s: they ran serially"
    assert (tmp_path / "pin.log").read_text().count("pinned") == 1
    for name in ("a", "b", "c"):
        assert f"scan-output-{name}" in proc.stderr, "each component's log must be replayed"
    assert crit["every-published-repo-scanned"]["outcome"] == "pass"


def test_scan_jobs_one_is_serial(tmp_path: Path) -> None:
    _, wall, _ = _run(tmp_path, 1, {})
    assert wall >= 3.0, "SCAN_JOBS=1 must keep the serial behaviour"


def test_exit_codes_are_still_scored_per_component(tmp_path: Path) -> None:
    proc, _, crit = _run(tmp_path, 3, {"b": 2, "c": 1})
    assert proc.returncode == 1
    assert crit["could-not-scan-is-not-a-pass"]["outcome"] == "fail"
    assert "never scanned: b" in crit["could-not-scan-is-not-a-pass"]["detail"]
    assert crit["every-published-repo-scanned"]["outcome"] == "fail"
