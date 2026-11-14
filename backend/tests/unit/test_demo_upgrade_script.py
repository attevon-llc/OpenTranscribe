"""The demo's pull-based upgrade (#628, E3): upgrade, no-op, rollback, refusal, lock.

``scripts/demo/demo-upgrade.sh`` runs against a stub install: a fake ``opentranscribe.sh``
that records its arguments and pins ``OT_IMAGE_TAG`` the way the real ``update --version`` /
``--rollback`` do, a fake reset command, and a fake ``curl`` on ``PATH`` that answers the
GitHub release record and ``/health`` from files. Every outcome the timer depends on is
driven through the real script: the order of commands, the exit code and the status file
the release ``finish`` stage reads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.demo_docker_stub import PS_STUB

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "demo" / "demo-upgrade.sh"

pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock")

FAKE_OPENTRANSCRIBE = r"""#!/usr/bin/env bash
echo "opentranscribe.sh $*" >> calls.log
echo "${COMPOSE_PROJECT_NAME:-}|${OPENTRANSCRIBE_EXTRA_COMPOSE_FILES:-}" >> env.log
tag() { grep '^OT_IMAGE_TAG=' .env | cut -d= -f2; }
case "$1 ${2:-}" in
  "update --version")
    # A target with no images: the pull is refused before anything is pinned or recorded.
    [ -f "missing-$3" ] && exit 1
    echo "# OT_PREVIOUS_IMAGE_TAG=$(tag)" > prev
    sed -i "s|^OT_IMAGE_TAG=.*|OT_IMAGE_TAG=$3|" .env ;;
  "update --rollback")
    sed -i "s|^OT_IMAGE_TAG=.*|OT_IMAGE_TAG=$(cut -d= -f2 prev)|" .env ;;
esac
[ -f "fail-$1" ] && exit 1
# The served version follows the pin, unless this tag is marked broken.
t=$(tag); [ -f "broken-$t" ] && t=broken; echo "$t" > served
exit 0
"""

FAKE_CURL = r"""#!/usr/bin/env bash
url="${@: -1}"
case "$url" in
  *releases/latest) printf '{"tag_name": "%s"}' "$(cat "$STUB/latest")" ;;
  */health) printf '{"status": "healthy", "version": "%s"}' "$(cat "$STUB/served")" ;;
  *) exit 22 ;;
esac
"""

FAKE_DOCKER = "#!/usr/bin/env bash\n" + PS_STUB + "exit 0\n"

FAKE_RESET = """#!/usr/bin/env bash
echo "reset lock_held=${DEMO_LOCK_HELD:-}" >> calls.log
"""


@pytest.fixture
def stub(tmp_path):
    install = tmp_path / "install"
    bindir = tmp_path / "bin"
    install.mkdir()
    bindir.mkdir()
    (install / ".env").write_text("OT_IMAGE_TAG=v0.6.1\nBACKEND_PORT=5174\n")
    (install / "served").write_text("v0.6.1\n")
    (install / "latest").write_text("v0.7.0\n")
    for path, body in (
        (install / "opentranscribe.sh", FAKE_OPENTRANSCRIBE),
        (install / "reset.sh", FAKE_RESET),
        (bindir / "curl", FAKE_CURL),
        (bindir / "docker", FAKE_DOCKER),
    ):
        path.write_text(body)
        path.chmod(0o755)
    return install, bindir


def run(stub, *args, **env):
    install, bindir = stub
    full_env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB": str(install),
        "DEMO_INSTALL_DIR": str(install),
        "DEMO_RESET_CMD": str(install / "reset.sh"),
        "DEMO_HEALTH_TIMEOUT": "2",
        "DEMO_HEALTH_INTERVAL": "1",
        **env,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=install,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    log = install / "calls.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def status(stub):
    return json.loads((stub[0] / "demo-status.json").read_text())


def test_a_new_release_is_pinned_fetched_reseeded_and_proven(stub):
    proc, calls = run(stub)

    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "opentranscribe.sh update --version v0.7.0",
        "opentranscribe.sh update-full",
        "reset lock_held=1",
    ]
    assert status(stub)["tag"] == "v0.7.0"
    assert status(stub)["previous_tag"] == "v0.6.1"
    assert status(stub)["result"] == "upgraded"


def test_already_current_does_nothing(stub):
    (stub[0] / "latest").write_text("v0.6.1\n")

    proc, calls = run(stub)

    assert proc.returncode == 0
    assert calls == []
    assert "already on v0.6.1" in proc.stdout


def test_a_pinned_target_wins_over_the_latest_release(stub):
    proc, calls = run(stub, DEMO_TARGET_TAG="v0.7.0-rc.1")

    assert proc.returncode == 0, proc.stderr
    assert calls[0] == "opentranscribe.sh update --version v0.7.0-rc.1"


def test_an_unhealthy_upgrade_rolls_back_and_exits_1(stub):
    (stub[0] / "broken-v0.7.0").write_text("")

    proc, calls = run(stub)

    assert proc.returncode == 1
    assert calls == [
        "opentranscribe.sh update --version v0.7.0",
        "opentranscribe.sh update-full",
        "reset lock_held=1",
        "opentranscribe.sh update --rollback",
        "opentranscribe.sh update-full",
        "reset lock_held=1",
    ]
    assert status(stub)["tag"] == "v0.6.1"
    assert status(stub)["result"] == "rolled-back"


def test_a_target_refused_before_the_pin_is_restarted_on_the_current_tag(stub):
    """Must fire: `update --rollback` here moved the demo to the tag an EARLIER upgrade recorded
    (or, with none recorded, failed and exited 4) although the demo never left v0.6.1."""
    (stub[0] / "prev").write_text("# OT_PREVIOUS_IMAGE_TAG=v0.5.0\n")
    (stub[0] / "missing-v0.7.0").write_text("")

    proc, calls = run(stub)

    assert proc.returncode == 1, proc.stderr
    assert calls == [
        "opentranscribe.sh update --version v0.7.0",
        "opentranscribe.sh update",
        "opentranscribe.sh update-full",
        "reset lock_held=1",
    ]
    assert "OT_IMAGE_TAG=v0.6.1" in (stub[0] / ".env").read_text()
    assert status(stub)["tag"] == "v0.6.1"
    assert status(stub)["result"] == "rolled-back"


def test_a_failed_rollback_exits_4(stub):
    (stub[0] / "broken-v0.7.0").write_text("")
    (stub[0] / "broken-v0.6.1").write_text("")

    proc, _ = run(stub)

    assert proc.returncode == 4
    assert status(stub)["result"] == "rollback-failed"


@pytest.mark.parametrize("tag", ["latest", "v0.7", "v0.7.0;rm -rf /", ""])
def test_a_malformed_target_is_refused(stub, tag):
    (stub[0] / "latest").write_text(f"{tag}\n")

    proc, calls = run(stub)

    assert proc.returncode == 2
    assert calls == []


def test_dry_run_prints_the_plan_and_runs_nothing(stub):
    proc, calls = run(stub, "--dry-run")

    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert "+ ./opentranscribe.sh update --version v0.7.0" in proc.stdout
    assert not (stub[0] / "demo-status.json").exists()


def test_a_held_lock_exits_3(stub):
    with open(stub[0] / ".demo.lock", "w") as fh:
        holder = subprocess.Popen(["flock", "-x", str(fh.name), "sleep", "30"])
        try:
            for _ in range(50):
                probe = subprocess.run(["flock", "-n", fh.name, "true"], check=False)
                if probe.returncode != 0:
                    break
            proc, calls = run(stub)
        finally:
            holder.kill()
            holder.wait()

    assert proc.returncode == 3
    assert calls == []


def test_a_project_from_another_directory_is_refused_before_any_restart(stub, tmp_path):
    """Must fire: `update` restarts the stack and the reset deletes its volumes."""
    proc, calls = run(stub, STUB_WORKDIR=str(tmp_path / "elsewhere"))

    assert proc.returncode == 21, proc.stderr
    assert calls == []
    assert not (stub[0] / "demo-status.json").exists()


def _fresh_overlay(stub, slug: str) -> None:
    """The container-name overlay `opentr.sh --fresh` generates for the stack."""
    (stub[0] / ".fresh").mkdir(exist_ok=True)
    (stub[0] / ".fresh" / f"{slug}.yml").write_text("services: {}\n")


def test_a_fresh_stack_is_upgraded_through_its_container_name_overlay(stub):
    """Must fire: a real upgrade of a fresh stack was refused, so its real and forced-failure legs
    could only run on the demo host. `update` now gets the stack's project and overlay."""
    _fresh_overlay(stub, "pd7")

    proc, calls = run(stub, DEMO_FRESH_NAME="pd7", DEMO_TARGET_TAG="v0.7.0")

    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "opentranscribe.sh update --version v0.7.0",
        "opentranscribe.sh update-full",
        "reset lock_held=1",
    ]
    env_seen = set((stub[0] / "env.log").read_text().splitlines())
    assert env_seen == {"otfresh-pd7|-f .fresh/pd7.yml"}
    assert status(stub)["result"] == "upgraded"


def test_a_fresh_stack_without_its_overlay_is_refused_before_anything_runs(stub):
    """Must fire: without .fresh/<slug>.yml `update` would collide with the live stack's names."""
    proc, calls = run(stub, DEMO_FRESH_NAME="pd7", DEMO_TARGET_TAG="v0.7.0")

    assert proc.returncode == 2, proc.stderr
    assert ".fresh/pd7.yml" in proc.stderr
    assert calls == []
    assert not (stub[0] / "demo-status.json").exists()


def test_a_dry_run_of_a_fresh_stack_prints_the_sequence(stub):
    _fresh_overlay(stub, "pd7")
    proc, calls = run(stub, "--dry-run", DEMO_FRESH_NAME="pd7", DEMO_TARGET_TAG="v0.7.0")

    assert proc.returncode == 0, proc.stderr
    assert "+ ./opentranscribe.sh update --version v0.7.0" in proc.stdout
    assert calls == []
