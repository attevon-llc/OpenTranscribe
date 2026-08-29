"""Two edges a `--with-pki` stack must keep closed (issue #620).

`PKI_TRUSTED_PROXIES` answers "may this peer assert an identity?", and
`test_pki_trusted_proxies_default.py` pins the allowlist itself. That is necessary and not
sufficient: an allowlist only protects the requests that actually reach the trust check, and a
`--with-pki` deployment has two ways to reach it that the allowlist cannot narrow.

**1. The backend's own published port.** `docker-compose.yml` publishes it on the host, and no
PKI/prod/nginx overlay rebinds it — so with the old wide allowlist a LAN device could skip the
mTLS-terminating nginx entirely and POST a forged `X-Client-Cert-DN` straight to the API. The
allowlist cannot help here whatever it contains, because docker preserves the external source
address on a published port: narrowing it only moves the boundary, it does not remove the door.
The fix binds that port to loopback for a `--with-pki` stack, driven by `BACKEND_BIND_HOST`,
which `scripts/pki/generate-test-env.sh` writes into the fragment `opentr.sh` sources before it
assembles the compose chain.

**2. The PKI nginx's own non-mTLS listeners.** nginx sits *inside* the trusted network by
construction, so anything it forwards is believed. Its plain-HTTP `:8080` server had no
`/api/auth/pki` block at all, meaning `POST /api/auth/pki/authenticate` fell through to the
generic `location /api/`, which forwards request headers verbatim — a client-supplied
`X-Client-Cert-DN` reached the backend from a peer the allowlist trusts *by design*. That is
the same vulnerability one layer up, and it survives any narrowing of the CIDR. Only the block
that actually terminated mTLS may emit a certificate header; every other backend-facing block
must clear it.

Both checks are static (compose text, nginx text) — the deployment shape is decided at config
time, so there is nothing a running stack would tell us that the files do not, and standing up
real mTLS to assert it would be slower and less reproducible. `test_release_manifest.py` is the
existing precedent for asserting on compose selection logic without starting containers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
GENERATE_TEST_ENV = REPO_ROOT / "scripts" / "pki" / "generate-test-env.sh"
PKI_NGINX_CONFS = (
    REPO_ROOT / "frontend" / "nginx-pki.conf",
    REPO_ROOT / "scripts" / "pki" / "nginx-pki-dev.conf",
)

#: The headers `pki_auth` reads. `PKI_CERT_HEADER` / `PKI_CERT_DN_HEADER` are configurable, but
#: these are what every shipped nginx config and the generated fragment agree on.
CERT_HEADERS = ("X-Client-Cert", "X-Client-Cert-Verify", "X-Client-Cert-DN")

#: nginx's own reading of the presented certificate. A block using these verified the cert
#: itself; a block without them did not and must therefore assert nothing.
MTLS_VARIABLES = ("$ssl_client_s_dn", "$ssl_client_escaped_cert", "$ssl_client_verify")


# ─── 1. the backend's published port ────────────────────────────────────────


def _backend_ports(compose_path: Path) -> list[str]:
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    backend = (document.get("services") or {}).get("backend") or {}
    return [str(entry) for entry in (backend.get("ports") or [])]


def test_the_base_compose_publishes_the_backend_through_a_configurable_bind_host():
    """A hardcoded `${BACKEND_PORT}:8080` binds the wildcard address with no way to narrow it."""
    ports = _backend_ports(BASE_COMPOSE)
    assert ports == ["${BACKEND_BIND_HOST:-0.0.0.0}:${BACKEND_PORT:-5174}:8080"], (
        f"docker-compose.yml's backend ports are {ports!r}; a --with-pki stack needs the bind "
        "host to be overridable so the API port can be kept off the LAN (issue #620)"
    )


def test_no_overlay_republishes_the_backend_port():
    """Compose APPENDS `ports:` across files (issue #343), so a second entry anywhere would
    publish the wildcard binding alongside the loopback one and silently undo the fix."""
    offenders = {
        path.name: ports
        for path in sorted(REPO_ROOT.glob("docker-compose*.yml"))
        if path != BASE_COMPOSE and (ports := _backend_ports(path))
    }
    assert not offenders, (
        f"these overlays add a second published port for `backend`: {offenders}. Compose "
        "appends port lists, so the base file's bind host would no longer be the only binding."
    )


def test_the_pki_fragment_pins_the_backend_port_to_loopback():
    """`--with-pki` must emit BACKEND_BIND_HOST, and it must be loopback."""
    source = GENERATE_TEST_ENV.read_text(encoding="utf-8")

    assignment = re.search(r'^BACKEND_BIND_HOST="([^"]*)"', source, re.MULTILINE)
    assert assignment, "generate-test-env.sh no longer sets a BACKEND_BIND_HOST default"
    assert assignment.group(1) == "127.0.0.1", (
        f"the --with-pki backend bind host is {assignment.group(1)!r}; anything but loopback "
        "puts the API port back within reach of the LAN"
    )
    assert "_env_kv BACKEND_BIND_HOST" in source, (
        "BACKEND_BIND_HOST is resolved but never written into pki-test.env, so compose would "
        "never see it"
    )


#: An unescaped `${BACKEND_BIND_HOST:-...}` in shell code — i.e. the script actually reading
#: the ambient value. A comment, or an escaped `\${...}` echoed into the generated fragment as
#: documentation, is neither an expansion nor a read; counting those was a false positive here
#: exactly as it was for `test_shell_expansion_guards.py`.
_AMBIENT_BIND_HOST = re.compile(r"(?<!\\)\$\{BACKEND_BIND_HOST:-")


def _ambient_bind_host_reads(source: str) -> list[str]:
    return [
        line
        for line in source.splitlines()
        if not line.lstrip().startswith("#") and _AMBIENT_BIND_HOST.search(line)
    ]


def test_the_pki_fragment_does_not_read_an_ambient_bind_host():
    """opentr.sh sources `.env` (which carries a live `BACKEND_BIND_HOST` line for ordinary
    deployments) *before* calling the generator. Honouring the ambient value would let a stock
    `.env` switch the control off with no message — the same trap `add_pki_overlay` documents
    for `PKI_HTTP_PORT`. Widening it must take the explicit flag."""
    source = GENERATE_TEST_ENV.read_text(encoding="utf-8")
    ambient = _ambient_bind_host_reads(source)
    assert not ambient, (
        f"generate-test-env.sh reads BACKEND_BIND_HOST from the environment ({ambient}); a "
        ".env value would then silently defeat the loopback binding"
    )
    assert "--backend-bind-host" in source, (
        "there is no explicit way to widen the bind host, which makes the control unusable for "
        "the deployment that legitimately needs the API port published"
    )


# ─── 2. nginx must not forward a client-claimed certificate header ──────────


def _location_blocks(config_text: str) -> list[tuple[str, str]]:
    """Return `(match, body)` for every `location` block, handling nesting by brace counting."""
    blocks: list[tuple[str, str]] = []
    for opener in re.finditer(r"^\s*location\s+([^{]+?)\s*\{", config_text, re.MULTILINE):
        depth = 1
        index = opener.end()
        while index < len(config_text) and depth:
            if config_text[index] == "{":
                depth += 1
            elif config_text[index] == "}":
                depth -= 1
            index += 1
        blocks.append((opener.group(1).strip(), config_text[opener.end() : index - 1]))
    return blocks


def _unprotected_backend_locations(config_text: str) -> list[str]:
    """Backend-facing `location` blocks that would forward a client-claimed cert header."""
    offenders: list[str] = []
    for match, body in _location_blocks(config_text):
        if not re.search(r"proxy_pass\s+https?://backend\b", body):
            continue
        if any(variable in body for variable in MTLS_VARIABLES):
            # The mTLS block. It must overwrite ALL three, not just the DN, or the two it
            # leaves alone stay client-controlled.
            missing = [
                header
                for header in CERT_HEADERS
                if not re.search(rf"proxy_set_header\s+{re.escape(header)}\s+\$ssl_client", body)
            ]
            if missing:
                offenders.append(f"{match} (mTLS block does not overwrite {missing})")
            continue
        cleared = [
            header
            for header in CERT_HEADERS
            if re.search(rf'proxy_set_header\s+{re.escape(header)}\s+""\s*;', body)
        ]
        if len(cleared) != len(CERT_HEADERS):
            still_open = [header for header in CERT_HEADERS if header not in cleared]
            offenders.append(f"{match} (forwards {still_open})")
    return offenders


@pytest.mark.parametrize("config_path", PKI_NGINX_CONFS, ids=lambda path: path.name)
def test_only_the_mtls_block_may_send_a_certificate_header(config_path):
    if not config_path.exists():
        pytest.skip(f"{config_path} not present")

    offenders = _unprotected_backend_locations(config_path.read_text(encoding="utf-8"))
    assert not offenders, (
        f"{config_path.name} proxies to the backend without clearing the certificate headers "
        f"in: {offenders}. nginx is inside PKI_TRUSTED_PROXIES, so anything it forwards is "
        "believed as an identity — a client-supplied X-Client-Cert-DN on a non-mTLS path is "
        "unauthenticated impersonation (issue #620)."
    )


@pytest.mark.parametrize("config_path", PKI_NGINX_CONFS, ids=lambda path: path.name)
def test_the_mtls_block_exists_and_is_reachable(config_path):
    """Clearing the headers everywhere is only correct while one block still SETS them —
    otherwise PKI sign-in is broken rather than hardened."""
    if not config_path.exists():
        pytest.skip(f"{config_path} not present")

    text = config_path.read_text(encoding="utf-8")
    pki_blocks = [
        match
        for match, body in _location_blocks(text)
        if "$ssl_client_s_dn" in body and re.search(r"proxy_pass\s+https?://backend\b", body)
    ]
    assert pki_blocks, f"{config_path.name} has no mTLS block left to authenticate PKI users"
    # nginx picks the longest matching prefix, so the PKI block must be more specific than the
    # generic /api/ one it sits beside or it would never be selected.
    assert all(block.startswith("/api/auth/pki") for block in pki_blocks), pki_blocks


# ─── guard the guard ────────────────────────────────────────────────────────
#
# A scanner that matches nothing reports zero findings, which is indistinguishable from a clean
# tree. Each detector above needs a case that MUST fire and one that must stay clean.

_UNPROTECTED = """
server {
    location /api/ {
        proxy_pass http://backend:8080/api/;
        proxy_set_header Host $host;
    }
}
"""

_PROTECTED = """
server {
    location /api/ {
        proxy_pass http://backend:8080/api/;
        proxy_set_header X-Client-Cert "";
        proxy_set_header X-Client-Cert-Verify "";
        proxy_set_header X-Client-Cert-DN "";
    }
    location /api/auth/pki {
        proxy_set_header X-Client-Cert $ssl_client_escaped_cert;
        proxy_set_header X-Client-Cert-Verify $ssl_client_verify;
        proxy_set_header X-Client-Cert-DN $ssl_client_s_dn;
        proxy_pass http://backend:8080/api/auth/pki;
    }
}
"""

_PARTIALLY_CLEARED = """
server {
    location /api/ {
        proxy_pass http://backend:8080/api/;
        proxy_set_header X-Client-Cert-DN "";
    }
}
"""

_MTLS_BLOCK_TRUSTING_THE_CLIENT_CERT = """
server {
    location /api/auth/pki {
        proxy_set_header X-Client-Cert-DN $ssl_client_s_dn;
        proxy_pass http://backend:8080/api/auth/pki;
    }
}
"""


def test_ambient_read_detector_fires_on_a_real_expansion_only():
    """Must fire on the shape it exists to catch, and stay clean on the two shapes that look
    like it but are not reads — the comment, and the escaped echo into the fragment."""
    assert _ambient_bind_host_reads('BACKEND_BIND_HOST="${BACKEND_BIND_HOST:-127.0.0.1}"')
    assert _ambient_bind_host_reads("# NOT `${BACKEND_BIND_HOST:-127.0.0.1}`") == []
    assert _ambient_bind_host_reads(r'  echo "# \${BACKEND_BIND_HOST:-0.0.0.0}:8080"') == []


def test_detector_fires_on_an_unprotected_backend_location():
    assert _unprotected_backend_locations(_UNPROTECTED) == [
        "/api/ (forwards ['X-Client-Cert', 'X-Client-Cert-Verify', 'X-Client-Cert-DN'])"
    ]


def test_detector_fires_when_only_the_dn_header_is_cleared():
    """Clearing the DN alone leaves the raw certificate and the verify result client-supplied."""
    offenders = _unprotected_backend_locations(_PARTIALLY_CLEARED)
    assert len(offenders) == 1
    assert "X-Client-Cert'" in offenders[0]
    assert "X-Client-Cert-Verify" in offenders[0]


def test_detector_fires_when_the_mtls_block_leaves_a_header_client_controlled():
    offenders = _unprotected_backend_locations(_MTLS_BLOCK_TRUSTING_THE_CLIENT_CERT)
    assert len(offenders) == 1
    assert "does not overwrite" in offenders[0]


def test_detector_stays_clean_on_a_correctly_configured_server():
    assert _unprotected_backend_locations(_PROTECTED) == []


def test_block_parser_handles_nesting():
    """Brace counting, not a lazy `}` match — an inner block would otherwise truncate the body
    and every header check after it would read as missing."""
    nested = """
server {
    location /api/ {
        if ($request_method = OPTIONS) { return 204; }
        proxy_pass http://backend:8080/api/;
        proxy_set_header X-Client-Cert "";
        proxy_set_header X-Client-Cert-Verify "";
        proxy_set_header X-Client-Cert-DN "";
    }
}
"""
    assert _unprotected_backend_locations(nested) == []
