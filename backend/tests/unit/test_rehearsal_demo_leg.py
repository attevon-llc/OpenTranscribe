"""Scenario C must start the public-demo overlay on the lite stack's own images.

S6v2 found the demo frontend crash-looping (nginx could not resolve Flower, which
``docker-compose.demo.yml`` scales to 0). The static contract test reads configs; nothing
in the release pipeline started the overlay on built images. ``lib/demo-leg.sh`` does, and
``65-rehearse.sh`` records it as its own blocking criterion, ``lite-demo-overlay``.

These tests drive the REAL ``demo-leg.sh`` with ``docker`` and ``curl`` faked on ``PATH``
(no stack), and read the wiring statically. The must-fire / must-stay-clean pairs matter:
a leg that passed against a 500, or a verdict never recorded, would read as green.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
LEG = REPO_ROOT / "scripts" / "release-tests" / "lib" / "demo-leg.sh"
LITE = REPO_ROOT / "scripts" / "release-tests" / "test-lite-mode.sh"
REHEARSE = REPO_ROOT / "scripts" / "release" / "65-rehearse.sh"
CRITERIA = REPO_ROOT / "scripts" / "release" / "release-criteria.yaml"

# Only bash gates the module: a missing demo-leg.sh must be RED, not a skip.
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")


def _drive(
    tmp_path: Path,
    *,
    health: str,
    write_code: str,
    write_body: str,
    up_rc: int = 0,
    chain: str | None = None,
):
    """Run demo_leg_run with fakes; returns (stdout, verdict file text, docker argv log)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker_log = tmp_path / "docker.log"
    (bindir / "docker").write_text(f'#!/bin/bash\necho "$@" >> {docker_log}\nexit {up_rc}\n')
    (bindir / "curl").write_text(
        textwrap.dedent(f"""\
        #!/bin/bash
        case "$*" in
          *-X\\ POST*) printf '%s\\n%s' '{write_body}' '{write_code}' ;;
          *) printf '{health}' ;;
        esac
        """)
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    install = tmp_path / "install"
    install.mkdir()
    # The leg asks the install's own front end for the chain (lite + demo from .env, mocks via the
    # EXTRA seam) instead of hand-building it; stand in for that script.
    fe = install / "opentranscribe.sh"
    fe.write_text(
        "#!/bin/bash\n"
        # first match wins, like read_env_value
        '[ "$(grep -E "^DEMO_MODE=" .env | head -1 | cut -d= -f2-)" = true ]'
        ' || { echo "demo not selected" >&2; exit 1; }\n'
        '[ "${DEPLOYMENT_MODE:-}" = lite ] || { echo "lite not selected" >&2; exit 1; }\n'
        + (
            f'echo "{chain}"\n'
            if chain is not None
            else 'echo "-f docker-compose.yml -f docker-compose.lite.yml -f docker-compose.demo.yml'
            ' ${OPENTRANSCRIBE_EXTRA_COMPOSE_FILES:-}"\n'
        )
    )
    fe.chmod(0o755)
    # An install's .env comes from .env.example, which already carries both keys, FIRST.
    env_text = "A=1\nDEMO_MODE=false\nDEPLOYMENT_MODE=full\nB=2\n"
    (install / ".env").write_text(env_text)
    verdict = tmp_path / "verdict"
    script = f"""
    set -u
    as_fail=0
    as_record() {{ echo "REC $1 $2"; [[ $1 == FAIL ]] && as_fail=$((as_fail+1)); return 0; }}
    gr_log() {{ :; }}
    source {LEG}
    st_target() {{ return 0; }}
    st_require() {{ echo "REQ $*"; return 0; }}
    TEST_PROJECT_NAME=p TEST_FRONTEND_PORT=1 DEMO_LEG_VERDICT_FILE={verdict}
    demo_leg_run {install}
    """
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert (install / ".env").read_text() == env_text, "the install's .env must be restored"
    return out.stdout, verdict.read_text(), docker_log.read_text() if docker_log.exists() else ""


_REFUSED = '{"detail":{"code":"demo_read_only","message":"x"}}'


def test_a_healthy_refusing_demo_stack_passes_and_never_builds_or_pulls(tmp_path):
    out, verdict, docker = _drive(tmp_path, health="200", write_code="403", write_body=_REFUSED)
    assert "REC FAIL" not in out
    assert out.count("REC PASS") == 3
    assert "healthy:frontend,backend" in out
    assert verdict.startswith("pass\t")
    assert "docker-compose.demo.yml" in docker
    assert "--no-build" in docker and "--pull never" in docker


def test_the_chain_has_lite_demo_and_the_seam_overlays_despite_a_template_env(tmp_path):
    # .env.example's DEMO_MODE=false / DEPLOYMENT_MODE=full come first and read_env_value takes the
    # first match, so a key appended after them would be ignored and the demo overlay would drop out.
    _, _, docker = _drive(tmp_path, health="200", write_code="403", write_body=_REFUSED)
    for ov in ("lite", "demo", "diar-native", "mock-asr", "mock-llm"):
        assert f"docker-compose.{ov}.yml" in docker, ov


def test_a_chain_missing_the_demo_overlay_is_a_failure_that_prints_the_chain(tmp_path):
    out, verdict, docker = _drive(
        tmp_path, health="200", write_code="403", write_body=_REFUSED, chain="-f docker-compose.yml"
    )
    assert "REC FAIL" in out and verdict.startswith("fail\t") and "docker-compose.yml" in verdict
    assert docker == ""


def test_a_write_that_is_not_refused_with_the_demo_code_fails(tmp_path):
    # A 403 from some other layer, and a 200/500, must not count as "refused".
    for code, body in (("403", '{"detail":"Forbidden"}'), ("200", "{}"), ("500", _REFUSED)):
        (tmp_path / code).mkdir()
        out, verdict, _ = _drive(tmp_path / code, health="200", write_code=code, write_body=body)
        assert "REC FAIL demo" in out and verdict.startswith("fail\t"), code


def test_health_not_200_through_the_frontend_fails(tmp_path):
    out, verdict, _ = _drive(tmp_path, health="502", write_code="403", write_body=_REFUSED)
    assert "REC FAIL demo leg: /api/health" in out
    assert verdict.startswith("fail\t")


def test_an_overlay_that_does_not_come_up_fails_and_skips_the_probes(tmp_path):
    out, verdict, _ = _drive(tmp_path, health="200", write_code="403", write_body=_REFUSED, up_rc=1)
    assert "did not come up" in out and "REC PASS" not in out
    assert verdict.startswith("fail\t")


def test_the_overlay_is_wired_into_scenario_c_and_declared_blocking():
    lite = LITE.read_text()
    assert "docker-compose.demo.yml" in LEG.read_text() and "phase 07d demo_leg_phase" in lite
    assert lite.index("phase 07d") < lite.index("phase 07c phase_07c_summary")
    # The chain comes from ./opentranscribe.sh compose-files, not a hand-built -f list.
    assert "opentranscribe.sh compose-files" in LEG.read_text()
    ids = {
        c["id"]: c["severity"]
        for c in yaml.safe_load(CRITERIA.read_text())["stages"]["rehearse"]["criteria"]
    }
    assert ids.get("lite-demo-overlay") == "blocking"
    assert "record lite-demo-overlay" in REHEARSE.read_text()
