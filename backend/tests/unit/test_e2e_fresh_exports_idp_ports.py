"""`run-e2e.sh --fresh <name>` must point the auth fixtures at the offset IdP overlays.

The Keycloak and LLDAP overlays of a `--fresh --port-offset N` stack publish on stock+N
(issue #347). The runner exported only the app/data ports, and the auth fixture hard-coded
Keycloak's 8180, so a fresh-stack auth run reached whatever owned 8180 on the host.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RUNNER = REPO / "scripts" / "e2e" / "run-e2e.sh"
LDAP_E2E = REPO / "backend" / "tests" / "e2e" / "test_ldap_oidc.py"


def _fresh_block_env(offset: int, tmp_path: Path) -> dict[str, str]:
    """Run only the runner's `--fresh` parsing and export block, then print the env."""
    text = RUNNER.read_text()
    start = text.index('FRESH_NAME=""')
    end = text.index("export E2E_FRONTEND_URL E2E_BACKEND_URL")
    fresh_dir = tmp_path / ".fresh"
    fresh_dir.mkdir()
    (fresh_dir / "probe.offset").write_text(f"{offset}\n")
    script = (
        f'PROJECT_ROOT="{tmp_path}"; RED=""; YELLOW=""; NC=""; set -- --fresh probe\n'
        + text[start:end]
        + "\nenv\n"
    )
    clean = {k: v for k, v in os.environ.items() if "KEYCLOAK" not in k and "LLDAP" not in k}
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, env=clean
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_fresh_exports_offset_idp_ports(tmp_path: Path) -> None:
    env = _fresh_block_env(100, tmp_path)

    assert env.get("KEYCLOAK_URL") == "http://localhost:8280"
    assert env.get("LLDAP_URL") == "http://localhost:17270"
    assert env.get("LLDAP_LDAP_PORT") == "3990"


def test_the_auth_fixture_takes_keycloak_port_from_keycloak_url() -> None:
    code = LDAP_E2E.read_text()

    assert "port=8180" not in code
    assert '_is_port_open("localhost", 8180)' not in code
    assert "KEYCLOAK_PORT" in code


def test_the_backend_reaches_keycloak_by_service_name_not_container_name() -> None:
    """A container name is project-specific; the compose service name resolves in any project."""
    code = LDAP_E2E.read_text()

    assert "transcribe-app-keycloak" not in code
    assert '"http://keycloak:8080"' in code
