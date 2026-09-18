"""The publish-time security scan should verify the pushed digest, not re-scan blindly.

50-scan.sh scans every declared leg BEFORE publish and writes `<label>-trivy.json`.
docker-build-push.sh's post-push `run_security_scan` scans AGAIN, unconditionally,
against the tag it just pushed -- roughly 25 minutes of the v0.5.0 publish spent
re-measuring content that had already been measured 07:10-07:18 the same morning.

The fix (`scan_try_reuse_report` in scripts/security-scan.sh) is a digest-equality
check, which is a STRONGER claim than a second scan, not a weaker one: nothing before
this proved the image that got PUSHED is the image that got SCANNED -- both stages
merely said "v0.5.0". Comparing the local image's digest (`docker image inspect --format
'{{.Id}}'`) against Trivy's own recorded `Metadata.ImageID` closes that gap.

Reuse also needs the SAME scan policy (SEVERITY_THRESHOLD, FAIL_ON_CRITICAL): a
digest-identical image scanned under a different policy could legitimately produce a
different verdict, so a changed policy must re-scan too.

Must-fire / must-stay-clean pattern per backend/tests/CLAUDE.md's "four tools that keep
the suite honest": the reuse branch is the one the issue itself flags as needing the
hardest test, because a bug there is silent -- a broken comparison that always returns
"equal" would skip every scan forever and look exactly like a fast green run.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_SH = REPO_ROOT / "scripts" / "security-scan.sh"

pytestmark = pytest.mark.skipif(
    not SCAN_SH.exists(), reason="scripts/security-scan.sh not present in this checkout"
)

FAKE_DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def test_the_script_and_function_exist() -> None:
    """GUARD THE GUARD: a moved/renamed function would make every check below vacuous."""
    assert SCAN_SH.is_file(), f"missing {SCAN_SH}"
    assert "scan_try_reuse_report()" in SCAN_SH.read_text(encoding="utf-8")


def _extract_function(name: str) -> str:
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(SCAN_SH)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}() not found in {SCAN_SH.name}"
    return out


def _fake_docker(bin_dir: Path, *, inspect_digest: str) -> None:
    """A `docker` that answers `image inspect --format '{{.Id}}'` with a fixed digest."""
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then\n'
        f'  printf "%s" "{inspect_digest}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _run_reuse_check(
    tmp_path: Path,
    *,
    local_digest: str = FAKE_DIGEST,
    report_digest: str | None = FAKE_DIGEST,
    verdict_exit: str | None = "0",
    verdict_severity: str = "MEDIUM",
    verdict_fail_crit: str = "true",
    env_severity: str = "MEDIUM",
    env_fail_crit: str = "true",
    write_trivy: bool = True,
    write_verdict: bool = True,
) -> tuple[int, str, str]:
    """Run the REAL scan_try_reuse_report() against fixture reports.

    Returns (returncode, stdout, stderr). A successful reuse returns 0 and echoes the
    verdict's exit code on stdout; a "must scan" outcome returns 1 with empty stdout.
    """
    output_dir = tmp_path / "security-reports"
    output_dir.mkdir()
    label = "backend-amd64"

    if write_trivy:
        trivy_payload: dict = {"Metadata": {}}
        if report_digest is not None:
            trivy_payload["Metadata"]["ImageID"] = report_digest
        (output_dir / f"{label}-trivy.json").write_text(json.dumps(trivy_payload))

    if write_verdict:
        verdict_payload: dict = {
            "severity_threshold": verdict_severity,
            "fail_on_critical": verdict_fail_crit,
        }
        if verdict_exit is not None:
            verdict_payload["exit_code"] = verdict_exit
        (output_dir / f"{label}-scan-verdict.json").write_text(json.dumps(verdict_payload))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir, inspect_digest=local_digest)

    snippet = f"""
set -e
print_info() {{ echo "$1"; }}
print_warning() {{ echo "$1"; }}
print_success() {{ echo "$1"; }}
OUTPUT_DIR="{output_dir}"
SEVERITY_THRESHOLD="{env_severity}"
FAIL_ON_CRITICAL="{env_fail_crit}"
{_extract_function("scan_try_reuse_report")}
scan_try_reuse_report "{label}" "some/repo:v1.0.0"
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr


@pytest.mark.unit
def test_must_stay_clean_same_digest_same_policy_reuses(tmp_path: Path) -> None:
    """The must-stay-clean case: identical digest, identical policy -> reuse, echo exit code."""
    rc, stdout, stderr = _run_reuse_check(tmp_path=tmp_path, verdict_exit="0")
    assert rc == 0, f"expected reuse (rc=0), got rc={rc}, stderr={stderr}"
    assert stdout == "0", f"expected the verdict's exit code echoed cleanly, got {stdout!r}"


@pytest.mark.unit
def test_must_stay_clean_reuses_a_findings_verdict_too(tmp_path: Path) -> None:
    """Reuse must work for a recorded FINDINGS verdict (1), not only a clean pass (0)."""
    rc, stdout, stderr = _run_reuse_check(tmp_path=tmp_path, verdict_exit="1")
    assert rc == 0, f"expected reuse (rc=0), got rc={rc}, stderr={stderr}"
    assert stdout == "1", stdout


@pytest.mark.unit
def test_must_fire_different_digest_forces_a_scan(tmp_path: Path) -> None:
    """THE core must-fire case: two genuinely different digests -> must scan, loudly."""
    rc, stdout, stderr = _run_reuse_check(
        tmp_path=tmp_path,
        local_digest=OTHER_DIGEST,
        report_digest=FAKE_DIGEST,
    )
    assert rc == 1, f"a digest mismatch must force a re-scan, got rc={rc} stdout={stdout!r}"
    assert stdout == "", f"a must-scan outcome must echo nothing, got {stdout!r}"
    assert "NOT reproducible" in stderr, (
        f"a digest mismatch must be logged loudly, not silently -- stderr was: {stderr!r}"
    )


@pytest.mark.unit
def test_must_fire_missing_trivy_report_forces_a_scan(tmp_path: Path) -> None:
    """Absence is never equality: no prior report at all -> must scan."""
    rc, stdout, _ = _run_reuse_check(tmp_path=tmp_path, write_trivy=False)
    assert rc == 1
    assert stdout == ""


@pytest.mark.unit
def test_must_fire_missing_verdict_forces_a_scan(tmp_path: Path) -> None:
    """A trivy report with no matching verdict sidecar -> must scan (nothing to reuse)."""
    rc, stdout, _ = _run_reuse_check(tmp_path=tmp_path, write_verdict=False)
    assert rc == 1
    assert stdout == ""


@pytest.mark.unit
def test_must_fire_unreadable_digest_field_forces_a_scan(tmp_path: Path) -> None:
    """A trivy report present but missing Metadata.ImageID -> must scan."""
    rc, stdout, _ = _run_reuse_check(tmp_path=tmp_path, report_digest=None)
    assert rc == 1
    assert stdout == ""


@pytest.mark.unit
def test_must_fire_missing_verdict_exit_code_forces_a_scan(tmp_path: Path) -> None:
    """A verdict sidecar present but missing exit_code -> must scan (nothing to echo)."""
    rc, stdout, _ = _run_reuse_check(tmp_path=tmp_path, verdict_exit=None)
    assert rc == 1
    assert stdout == ""


@pytest.mark.unit
def test_must_fire_changed_severity_threshold_forces_a_scan(tmp_path: Path) -> None:
    """Same digest, but the scan POLICY changed -> must scan; a digest match alone is not enough."""
    rc, stdout, stderr = _run_reuse_check(
        tmp_path=tmp_path,
        verdict_severity="MEDIUM",
        env_severity="HIGH",
    )
    assert rc == 1
    assert stdout == ""
    assert "policy changed" in stderr


@pytest.mark.unit
def test_must_fire_changed_fail_on_critical_forces_a_scan(tmp_path: Path) -> None:
    """Same digest, same severity, but FAIL_ON_CRITICAL changed -> must scan."""
    rc, stdout, stderr = _run_reuse_check(
        tmp_path=tmp_path,
        verdict_fail_crit="true",
        env_fail_crit="false",
    )
    assert rc == 1
    assert stdout == ""
    assert "policy changed" in stderr


@pytest.mark.unit
def test_scan_component_records_a_verdict_only_for_a_real_result() -> None:
    """COULD_NOT_SCAN must never be recorded as a reusable verdict.

    A future digest-identical call would otherwise reuse "could not scan" forever, even
    after whatever transient cause produced it is fixed. Checked structurally: the guard
    must gate the write on exit_code < EXIT_COULD_NOT_SCAN.
    """
    body = _extract_function("scan_component")
    assert 'if [ "${exit_code}" -lt "${EXIT_COULD_NOT_SCAN}" ]; then' in body, (
        "scan_component() must gate verdict-writing on a real result, "
        "never recording EXIT_COULD_NOT_SCAN as a reusable verdict"
    )
    assert "scan-verdict.json" in body
