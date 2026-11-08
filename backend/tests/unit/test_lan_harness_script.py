"""``scripts/test-lan-install.sh`` argument handling and verdict logic (issue #1264).

The harness itself needs a real server and a second device, so it cannot run in CI. What can
be pinned is the part that decides PASS or FAIL: the script is sourced with
``OT_LAN_SOURCE_ONLY=1`` and its classifiers are executed. A harness whose verdicts are
wrong is worse than none - it would tell the owner a phone install works when it does not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test-lan-install.sh"


def _bash(snippet: str, *, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", f'OT_LAN_SOURCE_ONLY=1 source "{SCRIPT}"; {snippet}'],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("rc", "closed"),
    [(7, True), (28, True), (52, True), (56, True), (0, False), (60, False), (22, False)],
)
def test_port_verdict_treats_only_no_answer_as_closed(rc, closed):
    """A connect that succeeds, even to an error page, means the port is exposed."""
    assert (_bash(f"port_rc_is_closed {rc}").returncode == 0) is closed


@pytest.mark.parametrize(
    ("curl_out", "ok"),
    [
        ("301 https://host/", True),
        ("308 https://host:8443/", True),
        ("200 ", False),
        ("301 http://host/", False),
        ("000 ", False),
    ],
)
def test_http_port_must_only_redirect_to_https(curl_out, ok):
    assert (_bash(f'redirect_is_https "{curl_out}"').returncode == 0) is ok


SANS = "X509v3 Subject Alternative Name: \n    DNS:opentranscribe.local, IP Address:192.168.1.50"


@pytest.mark.parametrize(
    ("host", "covered"),
    [
        ("opentranscribe.local", True),
        ("192.168.1.50", True),
        ("192.168.1.5", False),
        ("opentranscribe.localx", False),
        ("other.local", False),
    ],
)
def test_certificate_must_name_the_exact_address(host, covered):
    """A prefix match would accept 192.168.1.5 against a cert for 192.168.1.50."""
    # SANS goes through the environment to keep the snippet quoting simple.
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'OT_LAN_SOURCE_ONLY=1 source "{SCRIPT}"; san_covers_host "{host}" "$SANS"',
        ],
        env={"SANS": SANS, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode == 0) is covered


@pytest.mark.parametrize(
    ("headers", "secure"),
    [
        ("", True),
        ("Set-Cookie: a=b; Path=/; Secure; HttpOnly\n", True),
        ("Set-Cookie: a=b; Path=/; secure\n", True),
        ("Set-Cookie: a=b; Path=/; HttpOnly\n", False),
        ("Set-Cookie: a=b; Secure\nSet-Cookie: c=d; Path=/\n", False),
    ],
)
def test_every_cookie_must_carry_secure(headers, secure):
    assert (_bash("cookies_are_secure", stdin=headers).returncode == 0) is secure


def test_missing_host_is_a_usage_error(tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("x")
    result = _run("--ca", str(ca))
    assert result.returncode == 2
    assert "--host is required" in result.stderr


def test_missing_ca_file_is_a_usage_error():
    result = _run("--host", "192.168.1.50", "--ca", "/nonexistent/ca.crt")
    assert result.returncode == 2
    assert "--ca must point" in result.stderr


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_refuses_to_run_against_the_server_itself(tmp_path, host):
    ca = tmp_path / "ca.crt"
    ca.write_text("x")
    result = _run("--host", host, "--ca", str(ca))
    assert result.returncode == 2
    assert "another device" in result.stderr


def test_unknown_flag_is_a_usage_error():
    result = _run("--bogus")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_help_exits_zero_and_describes_the_checks():
    result = _run("--help")
    assert result.returncode == 0
    assert "plain app port" in result.stdout
