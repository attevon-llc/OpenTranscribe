"""`run-e2e.sh --fresh <name>` must point the auth fixtures at the IdP overlays of THAT stack.

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


STUB_LIB = """\
st_target() {
    [ "$1" = "--fresh" ] || return 2
    export ST_PROJECT="otfresh-$2" E2E_FRONTEND_URL=http://localhost:5273 \
           E2E_BACKEND_URL=http://localhost:5274 \
           KEYCLOAK_PORT=8280 LDAP_TEST_UI_PORT=17270 LDAP_TEST_PORT=3990
}
"""


def _runner_env(tmp_path: Path, stub: str, upto: str) -> subprocess.CompletedProcess[str]:
    """Run the runner's `--fresh` parsing and target-resolution block against a stubbed
    stack-target library, then print the environment."""
    text = RUNNER.read_text()
    start = text.index('FRESH_NAME=""')
    end = text.index(upto)
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "stack-target.sh").write_text(stub)
    script = (
        f'PROJECT_ROOT="{tmp_path}"; RED=""; YELLOW=""; NC=""; set -- --fresh probe\n'
        + text[start:end]
        + "\nenv\n"
    )
    clean = {k: v for k, v in os.environ.items() if "KEYCLOAK" not in k and "LLDAP" not in k}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=clean)


def test_fresh_exports_the_observed_idp_ports(tmp_path: Path) -> None:
    proc = _runner_env(tmp_path, STUB_LIB, "# Frontend and backend must be the SAME project")
    assert proc.returncode == 0, proc.stderr
    env = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)

    assert env.get("KEYCLOAK_URL") == "http://localhost:8280"
    assert env.get("LLDAP_URL") == "http://localhost:17270"
    assert env.get("LLDAP_LDAP_PORT") == "3990"


def test_an_unresolvable_target_stops_the_runner_with_the_library_exit_code(
    tmp_path: Path,
) -> None:
    """No `.fresh/<name>.offset` fallback to offset 0 (the live stack): the resolver's code wins."""
    stub = "st_target() { return 21; }\n"
    proc = _runner_env(tmp_path, stub, "# Hand-given URLs win over the observed ones")
    assert proc.returncode == 21, proc.stdout + proc.stderr


def test_the_runner_does_no_port_arithmetic() -> None:
    code = RUNNER.read_text()
    assert "FRESH_OFFSET" not in code
    assert "5173 +" not in code and "5174 +" not in code


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
