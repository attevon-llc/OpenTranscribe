"""With the database role split, only the migrate step holds owner (DDL) credentials.

docs/design/migrate_job_plan.md: the one-shot ``migrate`` service owns migrations, so under
``docker-compose.db-roles.yml`` the API and every worker connect with the runtime role only
(the owner password and ``MIGRATION_DATABASE_URL`` are blanked, ``RUN_MIGRATIONS_ON_STARTUP``
is false), and ``migrate`` keeps them.

That only works if nothing in the API still logs in to Postgres as ``POSTGRES_USER``. The
password login path (``direct_auth.get_db_connection``) did exactly that: it built its own
psycopg2 connection from the OWNER credentials, so every local login ran as the table owner and
the role split did not cover it. It must use the runtime engine.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from app.auth import direct_auth
from app.core.config import settings
from app.db.base import engine

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_login_connection_uses_the_runtime_role_not_the_owner_credentials(monkeypatch):
    """With the owner password withheld (as the db-roles overlay now does), login still connects."""
    monkeypatch.setattr(settings, "POSTGRES_USER", "owner_credentials_not_given_to_the_api")
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", "")

    with direct_auth.get_db_connection() as conn:
        login_role = conn.execute(text("SELECT current_user")).scalar()
    assert login_role

    with engine.connect() as runtime:
        runtime_role = runtime.execute(text("SELECT current_user")).scalar()
    assert login_role == runtime_role


# ── compose: who holds the owner credentials under db-roles ───────────────────────

_needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not present in this checkout"
)

OWNER_KEYS = ("POSTGRES_PASSWORD", "MIGRATION_DATABASE_URL")
CHAINS = {
    "prod+db-roles": (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.db-roles.yml",
    ),
    "dev+db-roles": (
        "docker-compose.yml",
        "docker-compose.override.yml",
        "docker-compose.db-roles.yml",
    ),
}


def _resolve(files: tuple[str, ...], project_dir: Path) -> dict:
    """``docker compose config`` with a stand-in ``.env`` (never the checkout's real one).

    The owner password reaches a container only through ``env_file: .env``, so the stand-in is
    what makes "who holds it" observable. ``--project-directory`` points ``env_file`` there.
    """
    (project_dir / ".env").write_text(
        "POSTGRES_PASSWORD=owner-secret\n"
        "POSTGRES_APP_USER=ot_app\n"
        "POSTGRES_APP_PASSWORD=app-secret\n"
        "FLOWER_PASSWORD=rendered-test-value\n"
    )
    args = ["docker", "compose", "-p", "ot-db-roles-test", "--project-directory", str(project_dir)]
    for f in files:
        args += ["-f", str(REPO_ROOT / f)]
    args.append("config")
    result = subprocess.run(
        args, cwd=project_dir, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    services: dict = yaml.safe_load(result.stdout)["services"]
    return services


def _env(service: dict) -> dict[str, str]:
    raw = service.get("environment") or {}
    return {k: ("" if v is None else str(v)) for k, v in raw.items()}


@_needs_docker
@pytest.mark.parametrize("label", sorted(CHAINS))
def test_only_the_migrate_step_holds_the_owner_credentials(label: str, tmp_path: Path) -> None:
    services = _resolve(CHAINS[label], tmp_path)
    holders = sorted(
        name
        for name, svc in services.items()
        if (name in ("backend", "migrate") or name.startswith("celery-"))
        and any(_env(svc).get(k) for k in OWNER_KEYS)
    )
    assert holders == ["migrate"], f"{label}: owner credentials reach {holders}"


@_needs_docker
@pytest.mark.parametrize("label", sorted(CHAINS))
def test_the_api_does_not_migrate_under_the_role_split(label: str, tmp_path: Path) -> None:
    """Without the owner password it could not; it must not try (dev included)."""
    services = _resolve(CHAINS[label], tmp_path)
    assert _env(services["backend"]).get("RUN_MIGRATIONS_ON_STARTUP") == "false"
    assert _env(services["migrate"]).get("POSTGRES_APP_USER") == "ot_app"
