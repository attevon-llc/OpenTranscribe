"""The public demo's compose overlay and its selection by the installer (#628).

Topology is the demo's third layer: no worker, no beat and no Flower exist, so no task could
run even if one were published. The service list is read from ``docker-compose.yml`` itself,
so a worker added there later without a matching ``replicas: 0`` here fails this test rather
than quietly starting on the public instance.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.unit.test_compose_file_selection import DEMO
from tests.unit.test_compose_file_selection import LITE
from tests.unit.test_compose_file_selection import _files
from tests.unit.test_compose_file_selection import _resolve

REPO_ROOT = Path(__file__).resolve().parents[3]
#: Profile-gated services never start unless their profile is activated, which the demo
#: never does; every other worker-shaped service must be zeroed.
_WORKER = re.compile(r"^(celery-|flower$)")


def _compose(name: str) -> dict:
    data: dict = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
    return data


def _replicas(service: dict | None) -> str | None:
    value = ((service or {}).get("deploy") or {}).get("replicas")
    if value is None:
        return None
    # `${DEMO_WORKER_REPLICAS:-0}` resolves to its default when the reset script is not seeding.
    match = re.fullmatch(r"\$\{[A-Z_]+:-(\d+)\}", str(value))
    return match.group(1) if match else str(value)


def test_no_worker_beat_or_flower_runs_on_the_demo():
    base = _compose("docker-compose.yml")["services"]
    lite = _compose(LITE)["services"]
    demo = _compose(DEMO)["services"]
    workers = [n for n, s in base.items() if _WORKER.match(n) and not s.get("profiles")]
    assert len(workers) >= 8, workers

    running = [n for n in workers if "0" not in (_replicas(demo.get(n)), _replicas(lite.get(n)))]

    assert running == []


def test_the_api_runs_in_demo_mode_with_the_lite_image_semantics():
    env = _compose(DEMO)["services"]["backend"]["environment"]

    assert env["DEMO_MODE"] == "true"
    assert env["DEPLOYMENT_MODE"] == "lite"
    assert env["MAX_CONCURRENT_SESSIONS"] == "0"  # 0 = unlimited; one shared demo account
    assert env["OPENTRANSCRIBE_TELEMETRY"] == "false"
    assert env["API_MEDIATED_UPLOAD_ENABLED"] == "false"


def test_demo_mode_is_a_literal_not_read_from_the_project_env():
    """.env.example ships DEMO_MODE=false, and compose interpolates from .env."""
    values = {
        name: (service.get("environment") or {}).get("DEMO_MODE")
        for name, service in _compose(DEMO)["services"].items()
    }

    assert values["backend"] == "true"
    assert {name: v for name, v in values.items() if v not in (None, "true")} == {}


def test_trusted_proxies_never_default_to_everything():
    value = _compose(DEMO)["services"]["backend"]["environment"]["RATE_LIMIT_TRUSTED_PROXIES"]
    default = re.fullmatch(r"\$\{RATE_LIMIT_TRUSTED_PROXIES:-([^}]*)\}", value)

    assert default is not None, value
    assert "0.0.0.0/0" not in default.group(1) and "*" not in default.group(1)


def test_the_installer_adds_the_demo_overlay_last_when_demo_mode_is_on(tmp_path):
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=False,
        env_lines=("DEPLOYMENT_MODE=lite", "DEMO_MODE=true"),
    )

    assert _files(chain)[-1] == DEMO, chain
    assert LITE in _files(chain)


@pytest.mark.parametrize("value", ["false", ""])
def test_the_installer_leaves_the_demo_overlay_out_otherwise(tmp_path, value):
    chain, _ = _resolve(
        tmp_path,
        nvidia_runtime=False,
        env_lines=("DEPLOYMENT_MODE=lite", f"DEMO_MODE={value}"),
    )

    assert DEMO not in _files(chain)


def test_the_overlay_ships_with_a_release():
    manifest = (REPO_ROOT / "release-manifest.txt").read_text(encoding="utf-8")
    shipped = {line.split("\t")[0] for line in manifest.splitlines() if line and line[0] != "#"}

    assert DEMO in shipped


@pytest.mark.parametrize(
    ("lite", "demo", "expected"),
    [
        ("", "--demo", [LITE, DEMO]),
        ("--lite", "", [LITE]),
        ("", "", []),
    ],
)
def test_opentr_adds_lite_whenever_the_demo_is_requested(lite, demo, expected):
    script = (
        f"source {REPO_ROOT}/scripts/lib/lite-demo-overlay.sh; COMPOSE_FILES=''; "
        f"add_lite_and_demo_overlays '{lite}' '{demo}' >/dev/null; "
        'echo "$COMPOSE_FILES"'
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, timeout=30
    ).stdout

    assert _files(out) == expected
