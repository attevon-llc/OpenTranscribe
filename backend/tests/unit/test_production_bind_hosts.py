"""Which host addresses the production ports publish on (issues #1332, #1264).

The browser only ever talks to one front door: the frontend's nginx, or the HTTPS overlay in
front of it. Both reach the backend and docs over the compose network, so their published
ports serve tools on the host and nothing else. These tests pin that:

* the rendered prod / prod+nginx / lite / offline chains publish backend and docs on
  127.0.0.1 by default, exactly once each (compose appends port lists, issue #343);
* the frontend stays on every interface by default when it IS the front door, and the two
  management scripts default it to loopback when the HTTPS overlay is loaded;
* the installer asks before publishing plain HTTP on the LAN, default no.

The chains are rendered with the real ``docker compose config`` from a temp copy of the
compose files and an empty env file, so an operator's ``.env`` can neither leak in nor
change the answer.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import COMMON
from tests.unit._shell_extract import INSTALLER
from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_function
from tests.unit._shell_extract import run_shell

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

#: Expected-value data compared against rendered config and .env lines.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - an expected address string; nothing binds a socket

CHAINS: dict[str, tuple[str, ...]] = {
    "prod": ("docker-compose.yml", "docker-compose.prod.yml"),
    "prod+nginx": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.nginx.yml"),
    "prod+lite": ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.lite.yml"),
    "prod+lite+nginx": (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.lite.yml",
        "docker-compose.nginx.yml",
    ),
    "offline": ("docker-compose.yml", "docker-compose.offline.yml"),
}

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not present")


def _render(tmp_path: Path, chain: tuple[str, ...], env: str = "") -> dict:
    for name in chain:
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    (tmp_path / "test.env").write_text(env, encoding="utf-8")
    args = ["docker", "compose", "--env-file", "test.env"]
    for name in chain:
        args += ["-f", name]
    result = subprocess.run(
        [*args, "config", "--format", "json"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"compose config failed for {chain}: {result.stderr}"
    document: dict = json.loads(result.stdout)
    return document


def _bindings(document: dict, service: str) -> list[tuple[str, str]]:
    ports = document["services"][service].get("ports") or []
    return [(port.get("host_ip", ALL_INTERFACES), str(port["published"])) for port in ports]


@needs_docker
@pytest.mark.parametrize("label", sorted(CHAINS))
def test_backend_and_docs_publish_on_loopback_only_by_default(tmp_path, label):
    document = _render(tmp_path, CHAINS[label])

    for service in ("backend", "docs"):
        bindings = _bindings(document, service)
        assert len(bindings) == 1, f"{label}: {service} is published {len(bindings)} times"
        assert bindings[0][0] == "127.0.0.1", f"{label}: {service} binds {bindings}"
    for host_ip, _ in _bindings(document, "flower"):
        assert host_ip == "127.0.0.1", f"{label}: flower binds {host_ip}"


@needs_docker
@pytest.mark.parametrize("label", ["prod", "offline"])
def test_frontend_is_the_lan_front_door_by_default_and_follows_its_bind_host(tmp_path, label):
    default = _bindings(_render(tmp_path, CHAINS[label]), "frontend")
    assert default == [(ALL_INTERFACES, "5173")], f"{label}: {default}"

    narrowed = _render(tmp_path, CHAINS[label], "FRONTEND_BIND_HOST=127.0.0.1\n")
    assert _bindings(narrowed, "frontend") == [("127.0.0.1", "5173")]


@needs_docker
def test_bind_host_overrides_are_honoured(tmp_path):
    env = "BACKEND_BIND_HOST=192.0.2.10\nDOCS_BIND_HOST=192.0.2.11\n"
    document = _render(tmp_path, CHAINS["prod"], env)
    assert _bindings(document, "backend") == [("192.0.2.10", "5174")]
    assert _bindings(document, "docs") == [("192.0.2.11", "5183")]


# ─── the HTTPS overlay's frontend default (opentranscribe.sh / opentr.sh) ──────────────


def _pin_frontend(tmp_path: Path, env_text: str, *, certs: bool, overlay: bool = True) -> str:
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    if overlay:
        (tmp_path / "docker-compose.nginx.yml").write_text("services: {}\n", encoding="utf-8")
    if certs:
        (tmp_path / "nginx" / "ssl").mkdir(parents=True)
        for name in ("server.crt", "server.key"):
            (tmp_path / "nginx" / "ssl" / name).write_text("x", encoding="utf-8")
    snippet = "\n".join(
        [
            f"source {COMMON}",
            extract_function(MANAGER, "nginx_overlay_ready"),
            extract_function(MANAGER, "pin_frontend_bind_host"),
            "pin_frontend_bind_host",
            'echo "BIND=[${FRONTEND_BIND_HOST:-unset}]"',
        ]
    )
    return run_shell(snippet, cwd=tmp_path)


def test_https_overlay_moves_the_frontend_port_to_loopback(tmp_path):
    out = _pin_frontend(tmp_path, "NGINX_SERVER_NAME=lan.example.test\n", certs=True)
    assert "BIND=[127.0.0.1]" in out, out


def test_an_explicit_frontend_bind_host_wins_over_the_https_default(tmp_path):
    env = "NGINX_SERVER_NAME=lan.example.test\nFRONTEND_BIND_HOST=0.0.0.0\n"
    out = _pin_frontend(tmp_path, env, certs=True)
    # Nothing exported: compose reads the .env value itself.
    assert "BIND=[unset]" in out, out


@pytest.mark.parametrize(
    ("env_text", "certs"),
    [("NGINX_SERVER_NAME=\n", True), ("NGINX_SERVER_NAME=lan.example.test\n", False)],
    ids=["no-server-name", "no-certificates"],
)
def test_without_the_https_overlay_the_frontend_default_is_untouched(tmp_path, env_text, certs):
    out = _pin_frontend(tmp_path, env_text, certs=certs)
    assert "BIND=[unset]" in out, out


def test_every_compose_arm_applies_the_frontend_pin():
    assert "pin_frontend_bind_host" in extract_function(MANAGER, "apply_deployment_pins")


def test_opentr_defaults_the_frontend_to_loopback_wherever_it_loads_the_https_overlay():
    text = OPENTR.read_text(encoding="utf-8")
    loads = [m.start() for m in re.finditer(r"-f docker-compose\.nginx\.yml\"\n", text)]
    assert loads, "opentr.sh no longer loads docker-compose.nginx.yml"
    for start in loads:
        following = text[start : start + 600]
        assert 'export FRONTEND_BIND_HOST="${FRONTEND_BIND_HOST:-127.0.0.1}"' in following


def test_the_old_wildcard_backend_default_is_reported_as_stale(tmp_path):
    env_file = tmp_path / "dotenv"
    snippet_parts = [
        f"source {COMMON}",
        subprocess.run(
            ["sed", "-n", "/^STALE_ENV_CHECKS=(/,/^)/p", str(MANAGER)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
        extract_function(MANAGER, "check_stale_env_values"),
        f"check_stale_env_values {env_file}",
    ]
    env_file.write_text("BACKEND_BIND_HOST=0.0.0.0\n", encoding="utf-8")
    assert "BACKEND_BIND_HOST=0.0.0.0" in run_shell("\n".join(snippet_parts))
    env_file.write_text("BACKEND_BIND_HOST=127.0.0.1\n", encoding="utf-8")
    assert run_shell("\n".join(snippet_parts)) == ""


# ─── installer: "Expose directly on your LAN without HTTPS?" ───────────────────────────


def _https_step(tmp_path: Path, env_text: str, env: dict[str, str], keys: str = "") -> str:
    """Run the installer's real configure_https_settings(); ``keys`` answers its prompts."""
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    body = extract_function(INSTALLER, "configure_https_settings").replace("</dev/tty", "")
    helpers = (
        "is_unattended",
        "ot_log_unattended",
        "print_info",
        "print_warning",
        "_upsert_env",
        "_set_frontend_bind_host",
        "write_nginx_server_name_to_env",
    )
    snippet = "\n".join(
        [
            "BLUE=''; NC=''; YELLOW=''; GREEN=''; RED=''; CYAN=''; _FRESH_INSTALL=true",
            f"source {COMMON}",
            *(extract_function(INSTALLER, name) for name in helpers),
            body,
            f"configure_https_settings <<< '{keys}'",
        ]
    )
    return run_shell(snippet, env=env, cwd=tmp_path)


def _bind_line(tmp_path: Path) -> list[str]:
    lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.startswith("FRONTEND_BIND_HOST=")]


ENV_TEMPLATE = "JWT=abc\nFRONTEND_BIND_HOST=\nOTHER=1\n"


@pytest.mark.parametrize(
    ("lan_answer", "expected"), [("n", "127.0.0.1"), ("", "127.0.0.1"), ("y", ALL_INTERFACES)]
)
def test_declining_https_asks_before_publishing_plain_http_on_the_lan(
    tmp_path, lan_answer, expected
):
    out = _https_step(tmp_path, ENV_TEMPLATE, {}, "n" + lan_answer)

    # `read -p` prints its prompt only to a terminal, so check the source for it.
    body = extract_function(INSTALLER, "configure_https_settings")
    assert "Expose directly on your LAN without HTTPS? (y/N)" in body
    assert "Skipping HTTPS setup" in out
    assert _bind_line(tmp_path) == [f"FRONTEND_BIND_HOST={expected}"]


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (None, "127.0.0.1"),
        (ALL_INTERFACES, ALL_INTERFACES),
        (f"{ALL_INTERFACES};rm -rf /", "127.0.0.1"),
    ],
    ids=["default", "explicit-lan", "rejected-value"],
)
def test_unattended_install_without_https_defaults_to_loopback(tmp_path, preset, expected):
    env = {"OPENTRANSCRIBE_UNATTENDED": "1"}
    if preset is not None:
        env["FRONTEND_BIND_HOST"] = preset
    _https_step(tmp_path, ENV_TEMPLATE, env)
    assert _bind_line(tmp_path) == [f"FRONTEND_BIND_HOST={expected}"]


@pytest.mark.parametrize("server_name", ["lan.example.test", "bad;name"])
def test_an_https_install_keeps_the_plain_port_on_loopback(tmp_path, server_name):
    env = {"OPENTRANSCRIBE_UNATTENDED": "1", "NGINX_SERVER_NAME": server_name}
    _https_step(tmp_path, ENV_TEMPLATE, env)
    assert _bind_line(tmp_path) == ["FRONTEND_BIND_HOST=127.0.0.1"]


def test_a_rerun_never_changes_an_existing_choice(tmp_path):
    _https_step(tmp_path, f"FRONTEND_BIND_HOST={ALL_INTERFACES}\n", {}, "nn")
    assert _bind_line(tmp_path) == [f"FRONTEND_BIND_HOST={ALL_INTERFACES}"]


def test_the_https_prompt_says_credentials_cross_the_network_in_clear():
    body = extract_function(INSTALLER, "configure_https_settings")
    assert "Sign-in passwords and transcripts cross your network unencrypted" in body
