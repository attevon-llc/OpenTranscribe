"""Guards for the release/build script efficiency changes (v0.7.0 lane dkc).

Each test fails against the previous behaviour: a global ``docker buildx use``, serial prod image
builds that stop at the first failure, a stop path that leaks anonymous volumes (or could delete
named ones), a fixed ``sleep 5`` in the auth e2e runner, ``cache-to mode=max`` in the PR scan.
"""

from __future__ import annotations

import re
import subprocess  # runs the repo's own shell functions against a fake docker  # nosec B404
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
OPENTR = ROOT / "opentr.sh"
PROD_IMAGES = ROOT / "scripts" / "lib" / "prod-images.sh"
BUILDER_LIB = ROOT / "scripts" / "lib" / "builder-select.sh"
BUILD_PUSH = ROOT / "scripts" / "docker-build-push.sh"
BUILD_STAGE = ROOT / "scripts" / "release" / "40-build.sh"


def _fn(path: Path, name: str) -> str:
    """Source text of one top-level shell function."""
    text = path.read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, f"{name} not found in {path}"
    return match.group(0)


def _bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # fixed argv  # nosec B603 B607
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=ROOT,
    )


def _code_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]


@pytest.mark.unit
@pytest.mark.parametrize("path", [BUILD_PUSH, BUILD_STAGE, BUILDER_LIB])
def test_no_global_builder_switch_in_build_scripts(path):
    offenders = [ln for ln in _code_lines(path) if re.search(r"buildx (use|create .*--use)\b", ln)]
    assert offenders == []


@pytest.mark.unit
def test_builder_is_scoped_to_the_process():
    code = "\n".join(_code_lines(BUILDER_LIB))
    assert "export BUILDX_BUILDER=" in code
    assert 'export BUILDX_BUILDER="${LOCAL_LOAD_BUILDER:-default}"' in code


@pytest.mark.unit
def test_host_arch_only_detection():
    script = (
        _fn(BUILDER_LIB, "targets_host_arch_only")
        + "\ndocker() { echo linux/amd64; }\n"
        + 'effective_platforms_for_target() { echo "$FAKE_PLATFORMS"; }\n'
        + "BUILD_TARGET=all\nif targets_host_arch_only; then echo HOST; else echo OTHER; fi\n"
    )
    assert "HOST" in _bash("FAKE_PLATFORMS=',linux/amd64,linux/amd64'\n" + script).stdout
    assert "OTHER" in _bash("FAKE_PLATFORMS=',linux/amd64,linux/arm64'\n" + script).stdout


def _prod_build_driver(failing: str) -> str:
    return (
        _fn(PROD_IMAGES, "build_prod_images")
        + f"""
APP_VERSION=1 GIT_SHA=x
docker() {{
  echo "BUILD $*" >> "$CALLS"
  case "$*" in *{failing}*) return 1 ;; esac
  return 0
}}
( build_prod_images )
echo "RC=$?"
"""
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "failing", ["frontend/Dockerfile.prod", "docs-site", "backend/Dockerfile.prod"]
)
def test_prod_builds_run_all_three_and_any_failure_fails_the_function(failing, tmp_path):
    calls = tmp_path / "calls"
    proc = _bash(f'CALLS="{calls}"\n' + _prod_build_driver(failing))
    assert "RC=1" in proc.stdout, proc.stdout + proc.stderr
    # one failure must not stop the other two builds
    assert len(calls.read_text().splitlines()) == 3


@pytest.mark.unit
def test_prod_builds_all_succeed(tmp_path):
    calls = tmp_path / "calls"
    proc = _bash(f'CALLS="{calls}"\n' + _prod_build_driver("NOPE_NEVER"))
    assert "RC=0" in proc.stdout, proc.stdout + proc.stderr
    assert len(calls.read_text().splitlines()) == 3


@pytest.mark.unit
def test_prod_builds_are_concurrent():
    body = _fn(PROD_IMAGES, "build_prod_images")
    assert body.count('&\n  pids+=("$!")') == 3
    assert 'wait "${pids[$i]}"' in body


@pytest.mark.unit
@pytest.mark.parametrize(("sub", "expect_call"), [("down", True), ("logs", False), ("up", False)])
def test_anonymous_volume_removal_only_on_down(sub, expect_call, tmp_path):
    calls = tmp_path / "calls"
    proc = _bash(
        _fn(OPENTR, "ot_rm_anonymous_volumes")
        + f'\ndocker() {{ echo "$*" >> "{calls}"; }}\n'
        + f"ot_rm_anonymous_volumes {sub} -f a.yml -f b.yml\n"
    )
    assert proc.returncode == 0, proc.stderr
    if expect_call:
        assert calls.read_text().strip() == "compose -f a.yml -f b.yml rm -fsv"
    else:
        assert not calls.exists()


@pytest.mark.unit
def test_stop_paths_never_delete_named_volumes():
    for name in ("stop_all_containers", "fresh_stop"):
        body = _fn(OPENTR, name)
        assert "ot_rm_anonymous_volumes" in body, name
        assert not re.search(r"docker compose[^\n]*\bdown\b[^\n]*(\s-v\b|--volumes)", body), name
    helper = _fn(OPENTR, "ot_rm_anonymous_volumes")
    assert "rm -fsv" in helper
    assert "down" not in helper.replace('"$sub" = "down"', "")
    # the plain `stop` command passes `down` alone, never `down -v`
    stop_branch = OPENTR.read_text().split("\n  stop)\n", 1)[1].split(";;", 1)[0]
    assert "stop_all_containers down\n" in stop_branch
    assert "down -v" not in stop_branch


@pytest.mark.unit
def test_dev_only_attestation_opt_out():
    assert "export BUILDX_NO_DEFAULT_ATTESTATIONS=1" in OPENTR.read_text()
    block = re.search(
        r'if \[ "\$ENVIRONMENT" = "dev" \]; then\n\s+export BUILDX_NO_DEFAULT_ATTESTATIONS=1',
        OPENTR.read_text(),
    )
    assert block, "must be guarded by ENVIRONMENT=dev"
    for path in (BUILD_PUSH, BUILD_STAGE, BUILDER_LIB):
        assert "BUILDX_NO_DEFAULT_ATTESTATIONS" not in path.read_text()


@pytest.mark.unit
def test_auth_e2e_polls_instead_of_sleeping():
    text = (ROOT / "scripts" / "run-auth-e2e.sh").read_text()
    assert "sleep 5" not in text
    assert re.search(r"wait_for_http \"[^\"]*openid-configuration\" 60 ", text)


@pytest.mark.unit
def test_release_build_logs_per_leg_timestamps_and_keeps_failure_closed():
    text = BUILD_STAGE.read_text()
    assert "build leg ${component} ${platform} start" in text
    assert "duration_s=" in text
    assert "record every-declared-leg-built fail" in text
    # the concurrent remote chain is opt-in, default off
    assert 'OT_BUILD_REMOTE_CONCURRENT:-false}" == "true"' in text


@pytest.mark.unit
def test_ci_cache_changes():
    scan = (ROOT / ".github/workflows/security-scan.yml").read_text()
    assert "mode=max" not in scan
    assert scan.count("cache-to: type=gha,mode=min") == 2
    pre = (ROOT / ".github/workflows/pre-commit.yml").read_text()
    assert "path: frontend/static/ffmpeg" in pre
    assert "hashFiles('frontend/ffmpeg-wasm-build/**'" in pre
