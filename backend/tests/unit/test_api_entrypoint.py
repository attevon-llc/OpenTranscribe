"""Every backend image starts the API through ``backend/scripts/api-start.sh``.

Scale foundations plan 3.5. The script is the one place that decides the process count (via
uvicorn's own ``UVICORN_*`` environment variables, https://www.uvicorn.org/settings/) and that
prepares ``PROMETHEUS_MULTIPROC_DIR`` (prometheus_client "Multiprocess Mode": set from a
startup script, empty at start). The dev override stays single-process because uvicorn's
``--reload`` and ``--workers`` are exclusive.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "backend" / "scripts" / "api-start.sh"
DOCKERFILES = ("Dockerfile.prod", "Dockerfile.lite", "Dockerfile.blackwell")

#: Variables a production chain refuses to render without; values are placeholders.
_RENDER_ENV = {"FLOWER_PASSWORD": "x", "POSTGRES_PASSWORD": "x"}


def _run_script(tmp_path: Path, **env: str) -> tuple[list[str], dict[str, str]]:
    """Run api-start.sh with a fake ``uvicorn`` first on PATH; return its argv and environment."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    record = tmp_path / "record.json"
    fake = fake_bin / "uvicorn"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(record)!r}, 'w'))\n"
    )
    fake.chmod(0o755)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("UVICORN_", "PROMETHEUS_"))}
    subprocess.run(
        ["sh", str(SCRIPT)],
        env={**base, "PATH": f"{fake_bin}:{base['PATH']}", **env},
        check=True,
        timeout=30,
    )
    data = json.loads(record.read_text())
    return data["argv"], data["env"]


@pytest.mark.parametrize("name", DOCKERFILES)
def test_every_image_starts_through_the_script(name: str) -> None:
    text = (REPO / "backend" / name).read_text()
    assert re.search(r'^CMD \["/app/scripts/api-start\.sh"\]$', text, re.M), name
    assert re.search(r"^ENV UVICORN_WORKERS=2\b", text, re.M), name
    assert "UVICORN_TIMEOUT_WORKER_HEALTHCHECK=30" in text, name
    assert "UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=30" in text, name


def test_the_script_is_executable_and_reaches_the_image() -> None:
    assert os.access(SCRIPT, os.X_OK)
    ignore = (REPO / "backend" / ".dockerignore").read_text().splitlines()
    assert "scripts/" in ignore and "!scripts/api-start.sh" in ignore, (
        "backend/.dockerignore drops scripts/; without the exception no image has the script"
    )


def test_uvicorn_leaves_the_client_and_scheme_to_the_app(tmp_path: Path) -> None:
    """uvicorn must not rewrite request.client: header_trust judges the real peer.

    The forwarded client and scheme are resolved in the app (test_header_trust_immediate_peer.py).
    """
    argv, _ = _run_script(tmp_path)
    assert argv[:1] == ["app.main:app"]
    assert "--no-proxy-headers" in argv
    assert "--proxy-headers" not in argv
    assert "--forwarded-allow-ips" not in argv
    assert "--workers" not in argv, "the count comes from UVICORN_WORKERS, which uvicorn reads"


def test_two_workers_enable_multiprocess_metrics_in_a_wiped_directory(tmp_path: Path) -> None:
    target = tmp_path / "prom"
    target.mkdir()
    (target / "counter_999.db").write_text("stale file from a previous run")
    _, env = _run_script(tmp_path, UVICORN_WORKERS="2", PROMETHEUS_MULTIPROC_DIR=str(target))
    assert env["PROMETHEUS_MULTIPROC_DIR"] == str(target)
    assert list(target.iterdir()) == []


def test_more_than_one_worker_defaults_the_directory(tmp_path: Path) -> None:
    _, env = _run_script(tmp_path, UVICORN_WORKERS="2")
    assert Path(env["PROMETHEUS_MULTIPROC_DIR"]).name == "prometheus-multiproc"


def test_one_worker_stays_on_the_plain_registry(tmp_path: Path) -> None:
    _, env = _run_script(tmp_path, UVICORN_WORKERS="1")
    assert "PROMETHEUS_MULTIPROC_DIR" not in env


def _render(*files: str) -> dict:
    docker = shutil.which("docker")
    assert docker, "docker is required to render the compose chains"
    cmd = [docker, "compose"]
    for name in files:
        cmd += ["-f", name]
    out = subprocess.run(
        [*cmd, "config", "--format", "json"],
        cwd=REPO,
        env={**os.environ, **_RENDER_ENV},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    backend: dict = json.loads(out.stdout)["services"]["backend"]
    return backend


@pytest.mark.parametrize(
    "chain",
    [
        ("docker-compose.yml", "docker-compose.prod.yml"),
        ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.lite.yml"),
        ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.offline.yml"),
        ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.nginx.yml"),
    ],
)
def test_production_chains_use_the_image_command_with_two_workers(chain: tuple[str, ...]) -> None:
    backend = _render(*chain)
    assert backend.get("command") in (None, ["/app/scripts/api-start.sh"])
    assert backend["environment"]["UVICORN_WORKERS"] == "2"
    assert backend["environment"]["DB_POOL_SIZE"] == "15"


def test_bench_overlay_starts_through_the_script() -> None:
    backend = _render("docker-compose.yml", "docker-compose.bench.yml")
    assert backend["command"] == ["/app/scripts/api-start.sh"]


def test_dev_override_pins_one_process_because_reload_is_single_process() -> None:
    backend = _render("docker-compose.yml", "docker-compose.override.yml")
    assert "--reload" in backend["command"]
    assert "--workers" not in backend["command"]
    assert "--no-proxy-headers" in backend["command"]
    assert backend["environment"]["UVICORN_WORKERS"] == "1"


def test_no_overlay_sets_a_bare_uvicorn_command_for_the_backend() -> None:
    """Only the dev override may name uvicorn directly; everything else goes through the script."""
    for path in sorted(REPO.glob("docker-compose*.yml")):
        if path.name == "docker-compose.override.yml":
            continue
        for line in path.read_text().splitlines():
            if line.strip().startswith("command:") and "uvicorn app.main" in line:
                pytest.fail(f"{path.name}: {line.strip()}")
