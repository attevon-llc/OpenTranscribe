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

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "demo" / "demo-reset.sh"

pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock")

CHAIN = "-f docker-compose.yml -f docker-compose.lite.yml -f docker-compose.demo.yml"

FAKE_DOCKER = r"""#!/usr/bin/env bash
args="$*"
echo "docker ${args//-f docker-compose.yml -f docker-compose.lite.yml -f docker-compose.demo.yml/CHAIN} workers=${DEMO_WORKER_REPLICAS:-0}" >> "$STUB/calls.log"
case "$args" in *demo_seed*) [ -f "$STUB/seed-fails" ] && exit 1 ;; esac
exit 0
"""


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
        "docker compose CHAIN up -d --wait --wait-timeout 900 nginx workers=0",
    ]


def test_a_failed_seed_leaves_the_site_closed(stub):
    (stub[0] / "seed-fails").write_text("")

    proc, calls = run(stub)

    assert proc.returncode != 0
    assert calls[-1].startswith("docker compose CHAIN run --rm -e DEMO_MODE=false")
    assert not any(c.endswith("nginx workers=0") and " up " in c for c in calls)


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
