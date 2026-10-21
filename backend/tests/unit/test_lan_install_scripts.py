"""Installer and ``setup-ssl`` behaviour for a LAN HTTPS install (issue #1239).

The shell functions are extracted from the real scripts and EXECUTED in a temp directory:
the installer itself pulls images and mutates the host, so it is never run whole, but the
functions that decide what lands in ``.env`` and what the operator is told are plain bash.

Three defects this pins:

* unattended installs never wrote ``NGINX_SERVER_NAME`` to ``.env`` (``opentranscribe.sh``
  reads only ``.env``), so the nginx overlay was never selected and HTTPS silently was not on;
* ``setup-ssl`` spliced an unvalidated hostname into a ``sed`` replacement and an openssl config;
* the address hint used ``hostname -I`` - GNU-only, empty on macOS, first entry not the LAN one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON = REPO_ROOT / "scripts" / "common.sh"
MANAGER = REPO_ROOT / "opentranscribe.sh"
SETUP = REPO_ROOT / "setup-opentranscribe.sh"
CERT_SCRIPT = REPO_ROOT / "scripts" / "generate-ssl-cert.sh"

HELPERS = ("ot_is_valid_hostname", "ot_detect_lan_ip")


def _function_body(text: str, name: str) -> str:
    """Source of one ``name() { ... }`` block (brace-counted), indented or not."""
    match = re.search(rf"^[ \t]*{re.escape(name)}\(\)\s*\{{", text, re.MULTILINE)
    assert match, f"{name}() not found"
    start = match.end() - 1
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return f"{name}() " + text[start : index + 1]
    raise AssertionError(f"unbalanced braces scanning {name}()")


def _normalised(body: str) -> list[str]:
    return [line.strip() for line in body.splitlines() if line.strip()]


def _bash(
    script: str, *, cwd: Path, env: dict[str, str] | None = None, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), **(env or {})},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _helpers_source() -> str:
    common = COMMON.read_text(encoding="utf-8")
    return "\n".join(_function_body(common, name) for name in HELPERS)


def test_the_function_extractor_can_actually_fail():
    with pytest.raises(AssertionError):
        _function_body("echo hi\n", "ot_not_a_function")


@pytest.mark.parametrize("name", HELPERS)
@pytest.mark.parametrize(
    "path", [MANAGER, SETUP], ids=["opentranscribe.sh", "setup-opentranscribe.sh"]
)
def test_standalone_fallbacks_are_identical_to_common_sh(name, path):
    """Both scripts ship standalone, so they carry a copy; a copy that drifts is a second bug."""
    canonical = _normalised(_function_body(COMMON.read_text(encoding="utf-8"), name))
    fallback = _normalised(_function_body(path.read_text(encoding="utf-8"), name))
    assert fallback == canonical


@pytest.mark.parametrize(
    "hostname",
    ["opentranscribe.local", "a", "host-1.lan", "Transcribe.Example.COM", "x" * 253],
)
def test_valid_hostnames_are_accepted(tmp_path, hostname):
    result = _bash(
        f'{_helpers_source()}\not_is_valid_hostname "$H"', cwd=tmp_path, env={"H": hostname}
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "hostname",
    ["", "-x", "x-", ".x", "x.", "a b", "a|b", "a;b", "a&b", "a/b", "a\nb", "x" * 254],
)
def test_hostnames_that_could_break_sed_or_openssl_config_are_refused(tmp_path, hostname):
    result = _bash(
        f'{_helpers_source()}\not_is_valid_hostname "$H"', cwd=tmp_path, env={"H": hostname}
    )
    assert result.returncode != 0


def _fake_bin(tmp_path: Path, scripts: dict[str, str], real: tuple[str, ...] = ()) -> Path:
    """A PATH directory holding fake commands plus symlinks to the real tools wanted."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
    for tool in real:
        found = shutil.which(tool)
        assert found, f"{tool} not found on this host"
        (bin_dir / tool).symlink_to(found)
    return bin_dir


def _lan_ip(tmp_path: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return _bash(
        f"{_helpers_source()}\not_detect_lan_ip",
        cwd=tmp_path,
        env={"PATH": str(bin_dir)},
    )


def test_lan_ip_is_the_default_route_source_address(tmp_path):
    bin_dir = _fake_bin(
        tmp_path,
        {"ip": "echo '1.1.1.1 via 192.168.0.1 dev eth0 src 192.168.0.42 uid 1000'"},
        real=("sed", "head", "awk"),
    )
    result = _lan_ip(tmp_path, bin_dir)
    assert (result.returncode, result.stdout.strip()) == (0, "192.168.0.42")


def test_lan_ip_uses_the_bsd_route_when_ip_is_absent(tmp_path):
    """The macOS path ``hostname -I`` could not serve."""
    bin_dir = _fake_bin(
        tmp_path,
        {
            "route": "echo '   interface: en0'",
            "ipconfig": "echo 10.0.0.9",
        },
        real=("sed", "head", "awk"),
    )
    result = _lan_ip(tmp_path, bin_dir)
    assert (result.returncode, result.stdout.strip()) == (0, "10.0.0.9")


@pytest.mark.parametrize("ip_output", ["", "Network is unreachable", "src not-an-address uid 1"])
def test_lan_ip_is_empty_rather_than_wrong(tmp_path, ip_output):
    bin_dir = _fake_bin(tmp_path, {"ip": f"echo '{ip_output}'"}, real=("sed", "head", "awk"))
    result = _lan_ip(tmp_path, bin_dir)
    assert (result.returncode, result.stdout.strip()) == (0, "")


def _code_only(text: str) -> str:
    """Drop whole-line comments: common.sh explains why it avoids the flag."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_no_shell_script_uses_gnu_only_hostname_i():
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in [*REPO_ROOT.glob("*.sh"), *(REPO_ROOT / "scripts").rglob("*.sh")]
        if re.search(
            r"hostname\s+-I\b", _code_only(p.read_text(encoding="utf-8", errors="replace"))
        )
    ]
    assert not offenders, f"hostname -I is GNU-only and may be a Docker bridge address: {offenders}"


# ─── installer: configure_https_settings (unattended) ────────────────────────────────


def _installer_harness() -> str:
    setup = SETUP.read_text(encoding="utf-8")
    common = COMMON.read_text(encoding="utf-8")
    parts = [
        "set -e",
        "BLUE=''; NC=''; YELLOW=''; GREEN=''; RED=''",
        'NGINX_SERVER_NAME="${NGINX_SERVER_NAME:-}"; SSL_CONFIGURED=false',
        _helpers_source(),
        _function_body(common, "ensure_env_permissions"),
        *(
            _function_body(setup, name)
            for name in (
                "is_unattended",
                "ot_log_unattended",
                "print_warning",
                "write_nginx_server_name_to_env",
                "configure_https_settings",
            )
        ),
        "configure_https_settings",
        'echo "RESULT name=[$NGINX_SERVER_NAME] ssl=[$SSL_CONFIGURED]"',
    ]
    return "\n".join(parts)


def _run_unattended(tmp_path: Path, env_text: str, nginx_name: str | None, certs: bool):
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    if certs:
        (tmp_path / "nginx" / "ssl").mkdir(parents=True)
        (tmp_path / "nginx" / "ssl" / "server.crt").write_text("x", encoding="utf-8")
        (tmp_path / "nginx" / "ssl" / "server.key").write_text("x", encoding="utf-8")
    env = {"OPENTRANSCRIBE_UNATTENDED": "1"}
    if nginx_name is not None:
        env["NGINX_SERVER_NAME"] = nginx_name
    return _bash(_installer_harness(), cwd=tmp_path, env=env)


ENV_WITH_COMMENTED_NAME = "JWT=abc\n# NGINX_SERVER_NAME=\nOTHER=1\n"


def test_unattended_install_writes_the_server_name_to_env(tmp_path):
    result = _run_unattended(tmp_path, ENV_WITH_COMMENTED_NAME, "lan.example.test", certs=False)

    assert result.returncode == 0, result.stdout + result.stderr
    env_lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    assert "NGINX_SERVER_NAME=lan.example.test" in env_lines
    assert env_lines.count("OTHER=1") == 1
    assert "RESULT name=[lan.example.test] ssl=[false]" in result.stdout
    assert "generate-ssl-cert.sh lan.example.test --auto-ip" in result.stdout
    assert oct((tmp_path / ".env").stat().st_mode & 0o777) == "0o600"


def test_unattended_install_with_existing_certificates_reports_https_ready(tmp_path):
    result = _run_unattended(tmp_path, ENV_WITH_COMMENTED_NAME, "lan.example.test", certs=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT name=[lan.example.test] ssl=[true]" in result.stdout


@pytest.mark.parametrize("bad", ["a|b", "x;rm -rf", "a b"])
def test_unattended_install_refuses_a_hostname_it_would_splice_into_sed(tmp_path, bad):
    result = _run_unattended(tmp_path, ENV_WITH_COMMENTED_NAME, bad, certs=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / ".env").read_text(encoding="utf-8") == ENV_WITH_COMMENTED_NAME
    assert "RESULT name=[] ssl=[false]" in result.stdout


def test_unattended_install_without_a_server_name_leaves_env_alone(tmp_path):
    result = _run_unattended(tmp_path, ENV_WITH_COMMENTED_NAME, None, certs=False)

    assert (tmp_path / ".env").read_text(encoding="utf-8") == ENV_WITH_COMMENTED_NAME
    assert "RESULT name=[] ssl=[false]" in result.stdout


# ─── opentranscribe.sh setup-ssl ─────────────────────────────────────────────────────


def _setup_ssl_arm() -> str:
    text = MANAGER.read_text(encoding="utf-8")
    start = text.index("    setup-ssl)\n")
    end = text.index("    version)\n", start)
    return text[start:end]


def _run_setup_ssl(tmp_path: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "scripts").mkdir()
    shutil.copy(CERT_SCRIPT, tmp_path / "scripts" / "generate-ssl-cert.sh")
    (tmp_path / "docker-compose.nginx.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "nginx").mkdir()
    (tmp_path / "nginx" / "site.conf.template").write_text("x\n", encoding="utf-8")
    (tmp_path / ".env").write_text(ENV_WITH_COMMENTED_NAME, encoding="utf-8")
    script = "\n".join(
        [
            "RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''",
            "check_environment() { :; }",
            f"source {COMMON}",
            'case "setup-ssl" in',
            _setup_ssl_arm(),
            "esac",
        ]
    )
    return _bash(script, cwd=tmp_path, stdin=stdin)


def test_setup_ssl_rejects_an_invalid_hostname_without_touching_anything(tmp_path):
    result = _run_setup_ssl(tmp_path, "bad;host\n")

    assert result.returncode == 1
    assert "not a valid hostname" in result.stdout
    assert (tmp_path / ".env").read_text(encoding="utf-8") == ENV_WITH_COMMENTED_NAME
    assert not (tmp_path / "nginx" / "ssl").exists()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_setup_ssl_with_a_valid_hostname_generates_the_certificate_and_records_it(tmp_path):
    result = _run_setup_ssl(tmp_path, "lan.example.test\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "nginx" / "ssl" / "server.crt").is_file()
    assert "NGINX_SERVER_NAME=lan.example.test" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "https://lan.example.test" in result.stdout
    assert "5173" in result.stdout, "the summary must steer people away from the plain app port"
    assert os.path.exists(tmp_path / "nginx" / "ssl" / "server.key")
