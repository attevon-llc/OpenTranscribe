"""The LLDAP test overlay serves LDAPS that the backend can VERIFY.

Since v0.6.1 the backend refuses a plaintext bind and checks the directory's certificate, so an
overlay that serves plain LDAP makes every LDAP e2e login fail. These tests pin the pieces that
make the overlay usable again without ever turning verification off:

* the compose files wire LLDAP's documented ``[ldaps_options]`` (``LLDAP_LDAPS_OPTIONS__*``) to
  a mounted certificate and publish the port on loopback only;
* the CA is mounted read-only into exactly the services that open LDAP connections, in a file of
  its own, because ``docker-compose.ldap-test.yml`` is also started standalone;
* ``scripts/pki/issue-test-server-cert.sh`` issues a leaf that passes OpenSSL strict
  verification (Python 3.13's ``ssl.create_default_context`` enables it) and a real TLS handshake
  with a verifying client, and refuses a wrong host name;
* the e2e test configures LDAPS with the same container-side values and never ticks the
  skip-verification switch.
"""

from __future__ import annotations

import re
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
LLDAP_COMPOSE = REPO / "docker-compose.ldap-test.yml"
TRUST_COMPOSE = REPO / "docker-compose.ldap-test-trust.yml"
CERT_SCRIPT = REPO / "scripts" / "pki" / "issue-test-server-cert.sh"
OPENTR = REPO / "opentr.sh"
TRUST_LIB = REPO / "scripts" / "lib" / "ldap-test-overlay.sh"
E2E_TEST = REPO / "backend" / "tests" / "e2e" / "test_ldap_oidc.py"

SANS = ("DNS:lldap-test", "DNS:lldap", "DNS:localhost", "IP:127.0.0.1")
BACKEND_CA_PATH = "/etc/opentranscribe/ldap-test-ca.crt"

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def _env(service: dict) -> dict[str, str]:
    entries = service["environment"]
    if isinstance(entries, dict):
        return {k: str(v) for k, v in entries.items()}
    return dict(item.split("=", 1) for item in entries)


@pytest.fixture(scope="module")
def opentr_source() -> str:
    return OPENTR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def e2e_source() -> str:
    return E2E_TEST.read_text(encoding="utf-8")


class TestComposeWiring:
    def test_lldap_enables_ldaps_with_the_documented_options(self):
        env = _env(_load(LLDAP_COMPOSE)["services"]["lldap"])
        assert env["LLDAP_LDAPS_OPTIONS__ENABLED"] == "true"
        assert env["LLDAP_LDAPS_OPTIONS__PORT"] == "6360"
        assert env["LLDAP_LDAPS_OPTIONS__CERT_FILE"] == "/certs/server.crt"
        assert env["LLDAP_LDAPS_OPTIONS__KEY_FILE"] == "/certs/server.key"

    def test_the_configured_cert_and_key_are_actually_mounted(self):
        lldap = _load(LLDAP_COMPOSE)["services"]["lldap"]
        env = _env(lldap)
        mounted = {v.split(":")[1]: v.split(":")[0] for v in lldap["volumes"] if "/certs/" in v}
        assert set(mounted) == {
            env["LLDAP_LDAPS_OPTIONS__CERT_FILE"],
            env["LLDAP_LDAPS_OPTIONS__KEY_FILE"],
        }
        assert all(
            src.startswith("./scripts/pki/test-certs/servers/ldap-test/")
            for src in mounted.values()
        )

    def test_ldaps_is_published_on_loopback_only(self):
        ports = _load(LLDAP_COMPOSE)["services"]["lldap"]["ports"]
        ldaps = [p for p in ports if p.endswith(":6360")]
        assert len(ldaps) == 1
        assert ldaps[0].startswith("127.0.0.1:${LDAP_TEST_LDAPS_PORT:-6360}")

    def test_the_overlay_file_stays_valid_when_started_standalone(self):
        """``idp_overlay.start_overlay`` runs ``docker compose -f`` on this file alone, and
        compose rejects a service that has neither ``image`` nor ``build``."""
        services = _load(LLDAP_COMPOSE)["services"]
        assert set(services) == {"lldap"}
        assert "image" in services["lldap"]

    def test_the_trust_overlay_mounts_the_ca_read_only_where_ldap_is_spoken(self):
        services = _load(TRUST_COMPOSE)["services"]
        assert set(services) == {"backend", "celery-cpu-worker"}
        for name, service in services.items():
            (mount,) = service["volumes"]
            source, target, mode = mount.split(":")
            assert source == "./scripts/pki/test-certs/servers/ldap-test/ca.crt", name
            assert target == BACKEND_CA_PATH, name
            assert mode == "ro", name

    def test_the_trust_overlay_never_switches_verification_off(self):
        text = TRUST_COMPOSE.read_text(encoding="utf-8") + LLDAP_COMPOSE.read_text(encoding="utf-8")
        assert not re.search(r"SKIP_VERIFY", text, re.IGNORECASE)


class TestOpentrWiring:
    def test_the_certificate_is_issued_with_the_same_sans_the_e2e_uses(self, e2e_source):
        match = re.search(
            r"issue-test-server-cert\.sh ldap-test \\\n\s+(DNS:[^\n]*?)\s+2>&1",
            TRUST_LIB.read_text(encoding="utf-8"),
        )
        assert match, "the LDAP overlay helper no longer issues the LDAP test certificate"
        assert tuple(match.group(1).split()) == SANS
        e2e_sans = re.search(r"LLDAP_TEST_CERT_SANS = \(([^)]*)\)", e2e_source)
        assert e2e_sans
        assert tuple(re.findall(r'"([^"]+)"', e2e_sans.group(1))) == SANS

    def test_both_start_paths_load_the_trust_overlay_after_the_lldap_one(self, opentr_source):
        # start_app and reset_and_init each add the overlay; a path that forgot the trust file
        # would start LLDAP whose certificate the backend cannot verify.
        calls = re.findall(
            r"add_flag_overlay WITH_LDAP_TEST_FLAG\n(\s*)add_ldap_test_trust_overlay", opentr_source
        )
        assert len(calls) == 2

    def test_a_fresh_deployment_records_the_trust_file_and_offsets_the_ldaps_port(
        self, opentr_source
    ):
        assert '"docker-compose.ldap-test-trust.yml")' in opentr_source
        assert '"LDAP_TEST_LDAPS_PORT=6360"' in opentr_source

    def test_opentr_sources_the_helper_it_calls(self, opentr_source):
        assert "source ./scripts/lib/ldap-test-overlay.sh" in opentr_source
        assert TRUST_LIB.stat().st_mode & 0o111

    def test_the_trust_overlay_is_only_added_after_the_certificate_exists(self):
        body = TRUST_LIB.read_text(encoding="utf-8")
        assert body.index("issue-test-server-cert.sh") < body.index('COMPOSE_FILES="$COMPOSE_FILES')


class TestE2eConfiguration:
    def test_the_container_side_values_match_the_overlay(self, e2e_source):
        assert 'LLDAP_BACKEND_HOST = "lldap-test"' in e2e_source
        assert "LLDAPS_BACKEND_PORT = 6360" in e2e_source
        assert f'LLDAP_BACKEND_CA_FILE = "{BACKEND_CA_PATH}"' in e2e_source

    def test_the_form_is_filled_for_verified_ldaps_and_never_skips_verification(self, e2e_source):
        assert 'page.fill("#ldap_ca_cert_file", LLDAP_BACKEND_CA_FILE)' in e2e_source
        assert "ssl_checkbox.check()" in e2e_source
        assert "skip_verify.check()" not in e2e_source
        assert "expect(skip_verify).not_to_be_checked()" in e2e_source

    def test_the_connection_test_asserts_the_real_feedback(self, e2e_source):
        assert "or True" not in e2e_source
        assert 'get_by_text("LDAP connection successful")' in e2e_source


@needs_openssl
class TestCertificateScript:
    """Runs a COPY of the script so the repo's gitignored ``test-certs`` is never touched."""

    @pytest.fixture
    def script(self, tmp_path: Path) -> Path:
        copy = tmp_path / "pki" / CERT_SCRIPT.name
        copy.parent.mkdir()
        shutil.copy2(CERT_SCRIPT, copy)
        return copy

    @staticmethod
    def _issue(script: Path, name: str, *sans: str, check: bool = True):
        return subprocess.run(
            [str(script), name, *sans], capture_output=True, text=True, check=check, timeout=120
        )

    @staticmethod
    def _leaf_dir(script: Path, name: str = "t") -> Path:
        return script.parent / "test-certs" / "servers" / name

    def test_the_script_is_executable(self):
        assert CERT_SCRIPT.stat().st_mode & 0o111

    def test_the_leaf_passes_openssl_strict_verification(self, script):
        """The failure this guards: a CA without keyUsage (what setup-test-pki.sh makes) is
        rejected by strict mode, which Python 3.13 turns on, so the backend image would refuse it."""
        self._issue(script, "t", *SANS)
        leaf = self._leaf_dir(script)
        result = subprocess.run(
            [
                "openssl",
                "verify",
                "-x509_strict",
                "-purpose",
                "sslserver",
                "-CAfile",
                str(leaf / "ca.crt"),
                str(leaf / "server.crt"),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_leaf_lists_every_requested_name(self, script):
        self._issue(script, "t", *SANS)
        crt = str(self._leaf_dir(script) / "server.crt")
        for host in ("lldap-test", "lldap", "localhost"):
            out = subprocess.run(
                ["openssl", "x509", "-in", crt, "-noout", "-checkhost", host],
                capture_output=True,
                text=True,
            ).stdout
            assert "does match" in out, host
        out = subprocess.run(
            ["openssl", "x509", "-in", crt, "-noout", "-checkip", "127.0.0.1"],
            capture_output=True,
            text=True,
        ).stdout
        assert "does match" in out

    def test_a_second_run_keeps_the_certificate(self, script):
        self._issue(script, "t", *SANS)
        first = (self._leaf_dir(script) / "server.crt").read_bytes()
        again = self._issue(script, "t", *SANS)
        assert (self._leaf_dir(script) / "server.crt").read_bytes() == first
        assert "Reusing" in again.stderr

    def test_a_new_name_reissues(self, script):
        self._issue(script, "t", *SANS)
        first = (self._leaf_dir(script) / "server.crt").read_bytes()
        self._issue(script, "t", *SANS, "DNS:extra.example")
        assert (self._leaf_dir(script) / "server.crt").read_bytes() != first

    def test_the_ca_name_is_reserved_and_bad_sans_are_refused(self, script):
        assert self._issue(script, "ca", "DNS:x", check=False).returncode == 2
        assert self._issue(script, "t", "lldap-test", check=False).returncode == 2
        assert self._issue(script, "t", check=False).returncode == 2

    def test_a_verifying_python_client_completes_a_handshake_and_rejects_a_wrong_name(self, script):
        """Real TLS with the flags Python 3.13 enables by default, not just ``openssl verify``."""
        self._issue(script, "t", *SANS)
        leaf = self._leaf_dir(script)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(leaf / "server.crt", leaf / "server.key")

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]

        outcomes: list[str] = []

        def serve() -> None:
            for _ in range(2):
                conn, _addr = listener.accept()
                try:
                    with server_ctx.wrap_socket(conn, server_side=True) as tls:
                        tls.recv(1)
                    outcomes.append("handshake ok")
                except (ssl.SSLError, OSError) as exc:
                    outcomes.append(f"handshake aborted: {type(exc).__name__}")
                finally:
                    conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def client_ctx() -> ssl.SSLContext:
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(leaf / "ca.crt"))
            ctx.verify_flags |= ssl.VERIFY_X509_STRICT | ssl.VERIFY_X509_PARTIAL_CHAIN
            return ctx

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
                with client_ctx().wrap_socket(raw, server_hostname="lldap-test") as tls:
                    assert tls.version() is not None
                    tls.send(b"x")
            with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
                with pytest.raises(ssl.SSLCertVerificationError):
                    client_ctx().wrap_socket(raw, server_hostname="not-in-cert.example")
        finally:
            thread.join(timeout=10)
            listener.close()
        # The server saw the same split: the verified connection completed, the other one did not.
        assert outcomes[0] == "handshake ok"
        assert outcomes[1].startswith("handshake aborted")
