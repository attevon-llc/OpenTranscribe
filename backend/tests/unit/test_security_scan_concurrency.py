"""R5: scan tools start together, grype waits only for syft, the DBs are pinned and recorded.

Behavioural: the real `scan_component` / `pin_vuln_dbs` run under bash with the per-tool
functions replaced by timing fakes and `trivy`/`grype` replaced by fake binaries. No docker,
no scanners, no network.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_SH = REPO_ROOT / "scripts" / "security-scan.sh"

pytestmark = pytest.mark.unit

HARNESS = r"""
set -euo pipefail
export PATH="$FAKEBIN:$PATH"
source "$SCAN_SH"
set +e
OUTPUT_DIR="$OUT"; mkdir -p "$OUT"
now() { date +%s.%N; }
resolve_platform_image() { echo "repo:tag-scanleg-amd64"; }
scan_try_reuse_report() { return 1; }
lint_dockerfile() { return 0; }
check_dockle() { return 0; }
run_dockle() { return 0; }
generate_sbom() { sleep 1.5; echo "$OUT/sbom.json"; touch "$OUT/sbom.json"; echo "$(now)" > "$OUT/syft.end"; }
scan_trivy() { echo "$(now)" > "$OUT/trivy.start"; sleep 0.2; eval "$TRIVY_BODY"; }
scan_grype() { echo "$(now)" > "$OUT/grype.start"; [ -f "$OUT/syft.end" ] && echo done > "$OUT/grype.saw_sbom"; return 0; }
scan_component backend linux/amd64
echo "rc=$?"
"""


def _fake(bin_dir: Path, name: str, body: str) -> None:
    f = bin_dir / name
    f.write_text("#!/bin/bash\n" + body + "\n")
    f.chmod(f.stat().st_mode | stat.S_IXUSR)


def _run(tmp_path: Path, trivy_body: str = "return 0") -> tuple[int, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "trivy", 'echo \'{"VulnerabilityDB":{"UpdatedAt":"2026-11-12T12:59:19Z"}}\'')
    _fake(bin_dir, "grype", 'echo \'{"built":"2026-11-08T06:19:57Z"}\'')
    out = tmp_path / "out"
    proc = subprocess.run(
        ["bash", "-c", HARNESS],
        env={
            "PATH": "/usr/bin:/bin",
            "FAKEBIN": str(bin_dir),
            "SCAN_SH": str(SCAN_SH),
            "OUT": str(out),
            "TRIVY_BODY": trivy_body,
            "IMAGE_TAG": "tag",
            "HOME": str(tmp_path),
        },
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    rc_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("rc=")]
    assert rc_line, f"harness did not reach scan_component's return:\n{proc.stdout}\n{proc.stderr}"
    return int(rc_line[-1].split("=")[1]), out


def test_trivy_does_not_wait_for_syft_and_grype_does(tmp_path: Path) -> None:
    rc, out = _run(tmp_path)
    assert rc == 0
    trivy_start = float((out / "trivy.start").read_text())
    syft_end = float((out / "syft.end").read_text())
    grype_start = float((out / "grype.start").read_text())
    assert trivy_start < syft_end - 1.0, "trivy must start beside syft, not after it"
    assert grype_start >= syft_end, "grype consumes the SBOM, so it may only start after syft"
    assert (out / "grype.saw_sbom").exists()


def test_a_dead_trivy_job_is_could_not_scan(tmp_path: Path) -> None:
    rc, _ = _run(tmp_path, trivy_body="kill -9 $BASHPID")
    assert rc == 2, "a trivy job that died without a status file must be COULD NOT SCAN (2)"


def test_a_failing_trivy_is_findings_not_could_not_scan(tmp_path: Path) -> None:
    rc, _ = _run(tmp_path, trivy_body="return 1")
    assert rc == 1


def test_verdict_records_both_db_versions(tmp_path: Path) -> None:
    _, out = _run(tmp_path)
    verdict = json.loads((out / "backend-amd64-scan-verdict.json").read_text())
    assert verdict["trivy_db_updated_at"] == "2026-11-12T12:59:19Z"
    assert verdict["grype_db_built"] == "2026-11-08T06:19:57Z"
    assert verdict["exit_code"] == "0"


def test_every_tool_leaves_start_and_end_stamps(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "trivy", "exit 0")
    _fake(bin_dir, "grype", "exit 0")
    out = subprocess.run(
        ["bash", "-c", HARNESS],
        env={
            "PATH": "/usr/bin:/bin",
            "FAKEBIN": str(bin_dir),
            "SCAN_SH": str(SCAN_SH),
            "OUT": str(tmp_path / "o"),
            "TRIVY_BODY": "return 0",
            "IMAGE_TAG": "tag",
            "HOME": str(tmp_path),
        },
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    for tool in ("hadolint", "dockle", "syft", "trivy", "grype"):
        for edge in ("start", "end"):
            assert f"[scan-time] backend-amd64 {tool} {edge} " in out.stdout + out.stderr


def _pin(
    tmp_path: Path, trivy_rc: int, grype_rc: int, extra: str = ""
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _fake(bin_dir, "trivy", f'echo "trivy $*" >> {log}; exit {trivy_rc}')
    _fake(bin_dir, "grype", f'echo "grype $*" >> {log}; exit {grype_rc}')
    script = (
        'set -euo pipefail; export PATH="$FAKEBIN:$PATH"; source "$SCAN_SH"; set +e; '
        f"{extra} pin_vuln_dbs; echo rc=$? skip=${{TRIVY_SKIP_DB_UPDATE:-unset}} "
        "grype=${GRYPE_DB_AUTO_UPDATE:-unset}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": "/usr/bin:/bin",
            "FAKEBIN": str(bin_dir),
            "SCAN_SH": str(SCAN_SH),
            "HOME": str(tmp_path),
        },
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_pin_fetches_once_then_disables_updates(tmp_path: Path) -> None:
    proc = _pin(tmp_path, 0, 0)
    assert "rc=0 skip=true grype=false" in proc.stdout
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert "trivy image --download-db-only --quiet" in calls
    assert "grype db update" in calls


@pytest.mark.parametrize(("trivy_rc", "grype_rc"), [(1, 0), (0, 1)])
def test_failed_db_fetch_is_could_not_scan(tmp_path: Path, trivy_rc: int, grype_rc: int) -> None:
    proc = _pin(tmp_path, trivy_rc, grype_rc)
    assert "rc=2 " in proc.stdout
    assert "skip=unset" in proc.stdout


def test_already_pinned_caller_skips_the_fetch(tmp_path: Path) -> None:
    proc = _pin(tmp_path, 1, 1, extra="SCAN_DB_PINNED=1")
    assert "rc=0 " in proc.stdout
    assert not (tmp_path / "calls.log").exists()
