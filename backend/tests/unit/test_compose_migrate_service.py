"""The one-shot ``migrate`` service and the API's migration gate, per deployment shape.

docs/design/migrate_job_plan.md: Alembic runs once in ``migrate`` (``python -m
app.startup.migrate``). ``backend`` and the Celery services wait for it with
``service_completed_successfully``, and where it runs the API is started with
``RUN_MIGRATIONS_ON_STARTUP=false``, so N replicas never race Alembic.

``migrate`` is a SECOND service running the backend image, and compose cannot share a
definition across files (``extends`` resolves per file, before overlays merge). So the risk
this module guards is drift: an overlay that moves ``backend`` to a new image, build or
``DEPLOYMENT_MODE`` without moving ``migrate`` would migrate with different code than the API
serves. Two layers:

* every ``docker-compose*.yml`` that sets one of those keys on ``backend`` must set the same
  value on ``migrate`` (static, so a NEW overlay is covered the day it is added);
* the REAL ``docker compose config`` for each chain ``opentr.sh`` / ``opentranscribe.sh``
  assemble must resolve both services identically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
yaml = pytest.importorskip("yaml")

#: Keys that decide WHICH code migrates and against WHAT. ``migrate`` must agree with
#: ``backend`` on each, in every file and every chain. The owner credentials
#: (``POSTGRES_PASSWORD``, ``MIGRATION_DATABASE_URL``) are deliberately absent: under the role
#: split only ``migrate`` holds them (test_runtime_role_least_privilege.py).
IMAGE_KEYS = ("image", "build", "pull_policy")
MIGRATION_ENV_KEYS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_APP_USER",
    "POSTGRES_APP_PASSWORD",
    "POSTGRES_SSLMODE",
    "DATABASE_URL",
    "DEPLOYMENT_MODE",
    "ENVIRONMENT",
    "TENANCY_MODE",
    "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
)

BASE = "docker-compose.yml"
DEV = (BASE, "docker-compose.override.yml")
PROD = (BASE, "docker-compose.prod.yml")

#: Chains where the API keeps migrating itself, with the reason. Everything else that runs
#: ``migrate`` must start the API with RUN_MIGRATIONS_ON_STARTUP=false.
SELF_MIGRATING_API: dict[str, str] = {
    "dev": "hot reload and restart-backend must apply a new revision; single replica",
    "dev+gpu+blackwell": "a dev chain (override loaded)",
    "dev+gpu-scale": "a dev chain (override loaded)",
    "dev+pki-dev": "a dev chain (override loaded)",
}

CHAINS: dict[str, tuple[str, ...]] = {
    "dev": DEV,
    "dev+gpu+blackwell": (*DEV, "docker-compose.gpu.yml", "docker-compose.blackwell.yml"),
    "dev+gpu-scale": (*DEV, "docker-compose.gpu.yml", "docker-compose.gpu-scale.yml"),
    "dev+pki-dev": (*DEV, "docker-compose.pki-dev.yml"),
    "prod": PROD,
    "prod+local": (*PROD, "docker-compose.local.yml"),
    "prod+nginx": (*PROD, "docker-compose.nginx.yml"),
    "prod+nginx+pki": (*PROD, "docker-compose.nginx.yml", "docker-compose.pki.yml"),
    "prod+db-roles": (*PROD, "docker-compose.db-roles.yml"),
    "prod+gpu+blackwell": (*PROD, "docker-compose.gpu.yml", "docker-compose.blackwell.yml"),
    "prod+lite": (*PROD, "docker-compose.lite.yml"),
    "prod+lite+nginx": (*PROD, "docker-compose.lite.yml", "docker-compose.nginx.yml"),
    "prod+lite+demo": (*PROD, "docker-compose.lite.yml", "docker-compose.demo.yml"),
    "offline": (BASE, "docker-compose.offline.yml"),
    "bench": (BASE, "docker-compose.gpu.yml", "docker-compose.bench.yml"),
}


def _env(service: dict) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, list):
        pairs = (item.split("=", 1) for item in raw)
        return {p[0]: (p[1] if len(p) > 1 else "") for p in pairs}
    return {k: ("" if v is None else str(v)) for k, v in raw.items()}


# ── static: every file ──────────────────────────────────────────────────────────


def _compose_files() -> list[Path]:
    files = sorted(REPO_ROOT.glob("docker-compose*.yml"))
    assert len(files) > 10, "expected the repo's compose overlays"
    return files


@pytest.mark.parametrize("path", _compose_files(), ids=lambda p: p.name)
def test_every_file_moves_migrate_with_backend(path: Path) -> None:
    services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
    backend = services.get("backend") or {}
    migrate = services.get("migrate") or {}

    drift = [
        f"{key}: backend={backend[key]!r} migrate={migrate.get(key)!r}"
        for key in IMAGE_KEYS
        if key in backend and migrate.get(key) != backend[key]
    ]
    backend_env, migrate_env = _env(backend), _env(migrate)
    drift += [
        f"environment {key}: backend={backend_env[key]!r} migrate={migrate_env.get(key)!r}"
        for key in MIGRATION_ENV_KEYS
        if key in backend_env and migrate_env.get(key) != backend_env[key]
    ]
    assert not drift, (
        f"{path.name} changes `backend` without making the same change to `migrate`, so the "
        f"migrate step would run different code or settings than the API: {drift}"
    )


def test_the_base_file_defines_a_one_shot_migrate_step() -> None:
    services = yaml.safe_load((REPO_ROOT / BASE).read_text())["services"]
    migrate = services["migrate"]
    assert migrate["restart"] == "no"
    assert migrate["command"] == "python -m app.startup.migrate"
    assert migrate["healthcheck"] == {"disable": True}
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_the_api_default_still_migrates_for_old_compose_files() -> None:
    """Public config: an install whose compose files predate `migrate` must still migrate."""
    services = yaml.safe_load((REPO_ROOT / BASE).read_text())["services"]
    assert "RUN_MIGRATIONS_ON_STARTUP" not in _env(services["backend"])


# ── resolved: every chain the scripts assemble ──────────────────────────────────

_needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not present in this checkout"
)


def _resolve(files: tuple[str, ...]) -> dict:
    missing = [f for f in files if not (REPO_ROOT / f).is_file()]
    assert not missing, f"compose overlay(s) not found in this checkout: {missing}"
    args = ["docker", "compose", "-p", "ot-migrate-wiring-test"]
    for f in files:
        args += ["-f", f]
    if "docker-compose.gpu-scale.yml" in files:
        # The scaled worker is profile-gated; without the profile it is not resolved at all.
        args += ["--profile", "gpu-scale"]
    args.append("config")
    env = {**os.environ, "FLOWER_PASSWORD": "rendered-test-value"}
    result = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False, env=env
    )
    assert result.returncode == 0, f"docker compose config failed for {files}:\n{result.stderr}"
    services: dict = yaml.safe_load(result.stdout)["services"]
    return services


@_needs_docker
@pytest.mark.parametrize("label", sorted(CHAINS))
def test_chain_runs_migrate_first_with_the_backends_image_and_settings(label: str) -> None:
    services = _resolve(CHAINS[label])
    backend, migrate = services["backend"], services["migrate"]

    for key in ("image", "build", "pull_policy"):
        assert migrate.get(key) == backend.get(key), (
            f"{label}: migrate {key}={migrate.get(key)!r}, backend {key}={backend.get(key)!r}"
        )
    backend_env, migrate_env = _env(backend), _env(migrate)
    for key in MIGRATION_ENV_KEYS:
        assert migrate_env.get(key) == backend_env.get(key), f"{label}: {key} differs"

    waiting = [
        name
        for name, svc in services.items()
        if (name == "backend" or name.startswith("celery-"))
        and (svc.get("depends_on") or {}).get("migrate", {}).get("condition")
        != "service_completed_successfully"
    ]
    assert not waiting, f"{label}: these start without waiting for migrate: {waiting}"


@_needs_docker
@pytest.mark.parametrize("label", sorted(CHAINS))
def test_the_api_does_not_migrate_where_the_migrate_step_owns_it(label: str) -> None:
    flag = _env(_resolve(CHAINS[label])["backend"]).get("RUN_MIGRATIONS_ON_STARTUP")
    if label in SELF_MIGRATING_API:
        assert flag in (None, "true"), f"{label} is documented as self-migrating"
    else:
        assert flag == "false", (
            f"{label}: `migrate` runs, so the API must be started with "
            f"RUN_MIGRATIONS_ON_STARTUP=false (got {flag!r}); every replica would race Alembic"
        )


@_needs_docker
def test_dev_migrates_the_working_tree_not_the_image() -> None:
    """Dev bind-mounts ./backend into the API; the migrate step must see the same revisions."""
    services = _resolve(DEV)

    def app_mount(service: dict) -> str | None:
        for volume in service.get("volumes") or []:
            if isinstance(volume, dict) and volume.get("target") == "/app":
                return str(volume.get("source"))
        return None

    assert app_mount(services["backend"]) is not None
    assert app_mount(services["migrate"]) == app_mount(services["backend"])
