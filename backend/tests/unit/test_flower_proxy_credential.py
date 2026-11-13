"""Every nginx that admits an admin to ``/flower/`` also sends Flower's own credential.

Flower runs with ``--basic_auth`` (docker-compose.yml). nginx gates ``/flower/`` with
``auth_request /api/auth/flower-authz`` and, when the backend answers 200, proxies the request
to Flower. A request that reaches Flower without the Basic credential gets Flower's own 401
(``WWW-Authenticate: Basic realm="flower"``), so an admin admitted by the backend still could not
open the dashboard. That is what a PKI stack showed: ``flower-authz`` 200 for the admin's cookie,
``/flower/`` 401 from Flower, because ``frontend/nginx.conf`` and ``frontend/nginx-pki.conf``
never supplied the credential. Only ``nginx/site.conf.template`` (the HTTPS overlay) and the dev
Vite proxy did.

The credential is RFC 7617 Basic: ``base64(user-id ":" password)``. It comes from the container
environment at start (``frontend/docker-entrypoint.d/15-flower-basic-auth.sh``, run by the
official nginx image's entrypoint), never from the image.

The authz endpoint's admin / non-admin answers are covered by ``test_flower_access.py``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT_SCRIPT = REPO_ROOT / "frontend" / "docker-entrypoint.d" / "15-flower-basic-auth.sh"
DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile.prod"

#: Where the image's entrypoint writes the credential; the frontend configs include it.
IMAGE_AUTH_CONF = "/etc/nginx/flower-auth/authorization.conf"
#: Where scripts/pki/nginx-pki-dev.conf (a host-run nginx) includes it from: relative to that
#: file's directory, inside the gitignored test-certs/.
HOST_DEV_AUTH_CONF = "test-certs/flower-auth.conf"

#: Each nginx config with a /flower/ block, and how it must supply the credential.
FLOWER_CONFIGS = {
    "frontend/nginx.conf": f"include {IMAGE_AUTH_CONF};",
    "frontend/nginx-pki.conf": f"include {IMAGE_AUTH_CONF};",
    "scripts/pki/nginx-pki-dev.conf": f"include {HOST_DEV_AUTH_CONF};",
    "nginx/site.conf.template": 'proxy_set_header Authorization "Basic ${FLOWER_AUTH_B64}";',
}

#: Test values only; never a credential of any deployment.
TEST_USER = "flower-test-user"
TEST_PASSWORD = "flower-test-value:with/odd+chars="


def _flower_locations(text: str) -> list[str]:
    """Bodies of every ``location ^~ /flower/`` block, comments stripped, brace-matched."""
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    bodies = []
    for match in re.finditer(r"location\s+\^~\s+/flower/\s*\{", text):
        depth, start = 1, match.end()
        index = start
        while depth:
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        bodies.append(text[start : index - 1])
    return bodies


# ─── every gated /flower/ block supplies the credential ──────────────────────


@pytest.mark.parametrize("config", sorted(FLOWER_CONFIGS))
def test_every_admitted_flower_request_carries_flowers_credential(config):
    bodies = _flower_locations((REPO_ROOT / config).read_text(encoding="utf-8"))
    assert bodies, f"{config} has no /flower/ location; update FLOWER_CONFIGS"
    for body in bodies:
        assert "auth_request /api/auth/flower-authz;" in body, (
            f"{config}: a /flower/ block is not gated by the admin check"
        )
        assert FLOWER_CONFIGS[config] in body, (
            f"{config}: a /flower/ block admits an admin but sends Flower no credential, so "
            f"Flower answers 401 itself. Expected `{FLOWER_CONFIGS[config]}` in the block."
        )


def test_every_repo_config_with_a_flower_block_is_checked():
    """A new nginx config with a /flower/ block must be added above, not slip past."""
    found = {
        str(path.relative_to(REPO_ROOT))
        for pattern in ("frontend/*.conf", "nginx/*.template", "scripts/**/*.conf")
        for path in REPO_ROOT.glob(pattern)
        if _flower_locations(path.read_text(encoding="utf-8"))
    }
    assert found == set(FLOWER_CONFIGS)


def test_the_host_dev_config_tells_the_operator_to_write_the_file_it_includes():
    """The host-run dev nginx has no entrypoint; its usage note runs the same script."""
    text = (REPO_ROOT / "scripts" / "pki" / "nginx-pki-dev.conf").read_text(encoding="utf-8")
    assert f"FLOWER_AUTH_CONF=scripts/pki/{HOST_DEV_AUTH_CONF}" in text
    assert "frontend/docker-entrypoint.d/15-flower-basic-auth.sh" in text


# ─── the entrypoint script that writes the credential ───────────────────────


def _run_entrypoint(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, str]:
    out = tmp_path / "authorization.conf"
    completed = subprocess.run(
        ["sh", str(ENTRYPOINT_SCRIPT)],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "FLOWER_AUTH_CONF": str(out), **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed, out.read_text(encoding="utf-8") if out.exists() else ""


def test_the_entrypoint_writes_the_rfc7617_credential(tmp_path):
    completed, written = _run_entrypoint(
        tmp_path, FLOWER_USER=TEST_USER, FLOWER_PASSWORD=TEST_PASSWORD
    )
    assert completed.returncode == 0, completed.stderr
    match = re.fullmatch(r'proxy_set_header Authorization "Basic ([A-Za-z0-9+/=]+)";\n', written)
    assert match, written
    assert base64.b64decode(match[1]).decode() == f"{TEST_USER}:{TEST_PASSWORD}"
    assert (tmp_path / "authorization.conf").stat().st_mode & 0o077 == 0, "credential is readable"


def test_the_entrypoint_defaults_the_user_like_the_flower_service(tmp_path):
    _, written = _run_entrypoint(tmp_path, FLOWER_PASSWORD=TEST_PASSWORD)
    encoded = re.search(r'"Basic ([^"]+)"', written)
    assert encoded, written
    assert base64.b64decode(encoded[1]).decode() == f"admin:{TEST_PASSWORD}"


@pytest.mark.parametrize("password", ["", None], ids=["empty", "unset"])
def test_without_a_password_the_header_is_cleared_not_invented(tmp_path, password):
    env = {"FLOWER_USER": TEST_USER}
    if password is not None:
        env["FLOWER_PASSWORD"] = password
    completed, written = _run_entrypoint(tmp_path, **env)
    assert completed.returncode == 0, completed.stderr
    assert written == 'proxy_set_header Authorization "";\n'


def test_the_image_runs_the_script_and_ships_a_default_include():
    """The official image runs executable *.sh in /docker-entrypoint.d/; nginx -t without the
    entrypoint (the release rehearsal's arm64 check) still needs the included file to exist."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY docker-entrypoint.d/15-flower-basic-auth.sh /docker-entrypoint.d/15-flower-basic-auth.sh"
        in dockerfile
    )
    assert "chmod 0755 /docker-entrypoint.d/15-flower-basic-auth.sh" in dockerfile
    assert f"> {IMAGE_AUTH_CONF}" in dockerfile
    assert "chown -R nginx:nginx /etc/nginx/flower-auth" in dockerfile
    script = ENTRYPOINT_SCRIPT.read_text(encoding="utf-8")
    assert f'out="${{FLOWER_AUTH_CONF:-{IMAGE_AUTH_CONF}}}"' in script


# ─── the frontend container gets the same credential Flower checks ──────────


CHAINS: dict[str, tuple[str, ...]] = {
    "dev": ("docker-compose.yml", "docker-compose.override.yml"),
    "prod": ("docker-compose.yml", "docker-compose.prod.yml"),
    "prod+pki": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.pki.yml"),
}


def _render(tmp_path: Path, chain: tuple[str, ...], env: str) -> dict[str, dict[str, Any]]:
    """The real ``docker compose config`` from a temp copy, so no ``.env`` or shell leaks in."""
    for name in chain:
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    (tmp_path / "test.env").write_text(env, encoding="utf-8")
    args = ["docker", "compose", "--env-file", "test.env"]
    for name in chain:
        args += ["-f", name]
    completed = subprocess.run(
        [*args, "config", "--format", "json", "--no-path-resolution"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    services: dict[str, dict[str, Any]] = json.loads(completed.stdout)["services"]
    return services


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not present")
@pytest.mark.parametrize("label", sorted(CHAINS))
@pytest.mark.parametrize(
    "env",
    [
        f"FLOWER_USER={TEST_USER}\nFLOWER_PASSWORD={TEST_PASSWORD}\n",
        f"FLOWER_PASSWORD={TEST_PASSWORD}\n",
    ],
    ids=["both-set", "user-defaulted"],
)
def test_the_frontend_sends_the_credential_flower_checks(tmp_path, label, env):
    services = _render(tmp_path, CHAINS[label], env)
    frontend_env = services["frontend"].get("environment") or {}
    assert frontend_env.get("FLOWER_PASSWORD") == TEST_PASSWORD, frontend_env
    user = frontend_env.get("FLOWER_USER")
    assert user == ("admin" if "FLOWER_USER" not in env else TEST_USER), frontend_env
    command = " ".join(services["flower"]["command"])
    assert f"--basic_auth={user}:{TEST_PASSWORD}" in command, (user, command)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not present")
def test_development_defaults_match_on_both_sides(tmp_path):
    services = _render(tmp_path, CHAINS["dev"], "")
    frontend_env = services["frontend"].get("environment") or {}
    command = " ".join(services["flower"]["command"])
    assert f"--basic_auth=admin:{frontend_env.get('FLOWER_PASSWORD')}" in command, (
        frontend_env,
        command,
    )
