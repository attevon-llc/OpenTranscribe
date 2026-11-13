"""The demo's nightly reset (#628, decision D6): the public port is closed for the whole window.

``scripts/demo/demo-reset.sh`` runs against a stub install whose ``opentranscribe.sh`` prints a
compose chain and whose ``docker`` records its arguments. The order is the contract: nginx
stops first, the volumes go, the seed runs with ``DEMO_MODE=false`` while nginx is scaled to
zero, and nginx comes back only after the last step succeeded. A failing seed leaves it down.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.demo_docker_stub import PS_STUB

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "demo" / "demo-reset.sh"

pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock")

CHAIN = "-f docker-compose.yml -f docker-compose.lite.yml -f docker-compose.demo.yml"

FAKE_DOCKER = (
    "#!/usr/bin/env bash\n"
    + PS_STUB
    + r"""args="$*"
echo "docker ${args//-f docker-compose.yml -f docker-compose.lite.yml -f docker-compose.demo.yml/CHAIN} workers=${DEMO_WORKER_REPLICAS:-0}" >> "$STUB/calls.log"
case "$args" in *demo_seed*) [ -f "$STUB/seed-fails" ] && exit 1 ;; esac
case "$args" in *assert_inert*) [ -f "$STUB/not-inert" ] && exit 1 ;; esac
exit 0
"""
)


@pytest.fixture
def stub(tmp_path):
    install, bindir, bundle = tmp_path / "install", tmp_path / "bin", tmp_path / "bundle"
    for d in (install, bindir, bundle):
        d.mkdir()
    (bundle / "manifest.json").write_text("{}")
    manager = install / "opentranscribe.sh"
    manager.write_text(f'#!/usr/bin/env bash\necho "{CHAIN}"\n')
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    for path in (manager, docker):
        path.chmod(0o755)
    return install, bindir, bundle


def run(stub, *args, **env):
    install, bindir, bundle = stub
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=install,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STUB": str(install),
            "DEMO_INSTALL_DIR": str(install),
            "DEMO_SEED_BUNDLE": str(bundle),
            "DEMO_USER_PASSWORD": "test-only-value",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    log = install / "calls.log"
    return proc, (log.read_text().splitlines() if log.exists() else [])


def test_the_site_is_closed_for_the_whole_reset(stub):
    proc, calls = run(stub)
    bundle = stub[2]

    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "docker compose CHAIN stop nginx workers=0",
        "docker compose CHAIN down -v --remove-orphans workers=0",
        "docker compose CHAIN up -d --wait --wait-timeout 900 --scale nginx=0 workers=1",
        f"docker compose CHAIN run --rm -e DEMO_MODE=false -v {bundle}:/seed:ro backend "
        "python -m app.scripts.demo_seed import --bundle /seed workers=0",
        "docker compose CHAIN up -d --wait --wait-timeout 900 --scale nginx=0 workers=0",
        "docker compose CHAIN exec -T backend python -m app.scripts.assert_inert workers=0",
        "docker compose CHAIN up -d --wait --wait-timeout 900 nginx workers=0",
    ]


def test_a_demo_that_is_not_inert_never_opens_the_site(stub):
    (stub[0] / "not-inert").write_text("")

    proc, calls = run(stub)

    assert proc.returncode != 0
    assert calls[-1].endswith("python -m app.scripts.assert_inert workers=0")
    assert not any(c.endswith("nginx workers=0") and " up " in c for c in calls)


def test_a_failed_seed_leaves_the_site_closed(stub):
    (stub[0] / "seed-fails").write_text("")

    proc, calls = run(stub)

    assert proc.returncode != 0
    assert calls[-1].startswith("docker compose CHAIN run --rm -e DEMO_MODE=false")
    assert not any(c.endswith("nginx workers=0") and " up " in c for c in calls)


def test_without_a_demo_password_nothing_is_touched_and_the_fix_is_named(stub):
    proc, calls = run(stub, DEMO_USER_PASSWORD="")

    assert proc.returncode == 2
    assert calls == []
    assert "DEMO_USER_PASSWORD" in proc.stderr and "openssl rand" in proc.stderr


def test_the_password_may_come_from_the_install_dirs_env_file(stub):
    install, _, _ = stub
    (install / ".env").write_text("DEMO_USER_PASSWORD=from-the-env-file\n")

    proc, calls = run(stub, "--dry-run", DEMO_USER_PASSWORD="")

    assert proc.returncode == 0, proc.stderr


def test_without_a_bundle_nothing_is_touched(stub):
    proc, calls = run(stub, DEMO_SEED_BUNDLE=str(stub[0] / "no-such-bundle"))

    assert proc.returncode == 2
    assert calls == []


def test_without_the_demo_overlay_in_the_chain_nothing_is_touched(stub):
    (stub[0] / "opentranscribe.sh").write_text(
        '#!/usr/bin/env bash\necho "-f docker-compose.yml -f docker-compose.lite.yml"\n'
    )

    proc, calls = run(stub)

    assert proc.returncode == 2
    assert calls == []
    assert "demo overlay is not in the compose chain" in proc.stderr


def test_dry_run_runs_nothing(stub):
    proc, calls = run(stub, "--dry-run")

    assert proc.returncode == 0
    assert calls == []
    assert "+ docker compose" in proc.stdout


def test_the_upgrade_record_exists_before_the_stack_starts_and_is_never_overwritten(stub):
    install, _, _ = stub
    status = install / "demo-status.json"

    proc, _ = run(stub)
    assert proc.returncode == 0, proc.stderr
    assert status.read_text() == "{}"

    status.write_text('{"result": "upgraded"}')
    proc, _ = run(stub)
    assert proc.returncode == 0, proc.stderr
    assert status.read_text() == '{"result": "upgraded"}'


def test_a_dry_run_does_not_create_the_upgrade_record(stub):
    install, _, _ = stub

    run(stub, "--dry-run")

    assert not (install / "demo-status.json").exists()


DESTRUCTIVE = (" down ", " stop ", " up ", " run ", " exec ")


def _mutating(calls):
    return [c for c in calls if any(w in f" {c} " for w in DESTRUCTIVE)]


def test_a_project_running_from_another_directory_is_refused_before_any_docker_call(stub, tmp_path):
    """Must fire: `down -v` against a stack that is not this install deletes someone else's data."""
    proc, calls = run(stub, STUB_WORKDIR=str(tmp_path / "somewhere-else"))

    assert proc.returncode == 21, proc.stderr
    assert "refusing" in proc.stderr
    assert _mutating(calls) == []


def test_a_project_with_no_containers_is_refused_before_any_docker_call(stub):
    proc, calls = run(stub, STUB_NO_STACK="1")

    assert proc.returncode == 20, proc.stderr
    assert _mutating(calls) == []


def test_the_asserted_project_is_the_one_compose_acts_on(stub):
    """Must stay clean: the matching project proceeds, under the project that was asserted."""
    install = stub[0]
    (install / "opentranscribe.sh").write_text(f'#!/usr/bin/env bash\necho "{CHAIN}"\n')
    proc, calls = run(stub, DEMO_FRESH_NAME="Demo-X")

    assert proc.returncode == 0, proc.stderr
    assert "target project otfresh-demo-x" in proc.stdout
    assert _mutating(calls)


def test_the_reset_can_target_a_fresh_stack_and_never_the_live_project(stub):
    proc, _ = run(stub, DEMO_FRESH_NAME="t1", COMPOSE_PROJECT_NAME="opentranscribe")

    assert proc.returncode == 0, proc.stderr
    assert "otfresh-t1" in proc.stdout and "opentranscribe" not in proc.stdout
