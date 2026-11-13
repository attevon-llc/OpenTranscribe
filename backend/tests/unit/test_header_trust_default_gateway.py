"""The backend's own default gateway can never vouch for an identity header.

On an isolated PKI stack a forged ``X-Client-Cert-DN`` sent from the host to the backend's
published port was accepted as an admin login. The host reaches a container on a Docker bridge
from the bridge's gateway address, and so does every client Docker's userland proxy relays to a
published port (docker/docs issue #17312; Docker docs "Port publishing and mapping"). That
address lies inside the container subnet that ``PKI_TRUSTED_PROXIES`` allow-lists for the PKI
nginx, so the subnet rule passed it.

The PKI nginx reaches the backend from its own container address, never the gateway, so the
rule is: the immediate peer must be in the allowlist and must not be this namespace's default
gateway. These tests feed ``header_trust.default_gateways`` a routing table in the kernel's own
``/proc/net/route`` format (proc(5)) and drive the real trust checks the PKI and trusted-header
logins use.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.auth import header_trust
from app.auth.header_trust import default_gateways
from app.auth.header_trust import header_assertion_permitted
from app.auth.header_trust import header_source_is_trusted
from app.auth.header_trust import parse_trusted_proxies
from app.auth.pki_auth import _extract_user_info_from_request
from app.auth.pki_auth import _validate_pki_headers_source

#: The isolated PKI stack's measured network and allowlist.
GATEWAY = "10.200.0.1"
NGINX = "10.200.0.5"
ALLOWLIST = "127.0.0.1/32,10.200.0.0/24"
ADMIN_DN = "CN=pkiadmin,OU=Admins,O=OpenTranscribe"
DN_HEADER = "X-Client-Cert-DN"

#: A container's table: default route via 10.200.0.1 (``0100C80A`` is that address's
#: network-order bytes read as a little-endian integer), plus the on-link subnet route.
CONTAINER_ROUTES = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t0100C80A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t0000C80A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


class _Request:
    class _Client:
        def __init__(self, host: str) -> None:
            self.host = host

    def __init__(self, host: str, headers: dict[str, str] | None = None) -> None:
        self.client = self._Client(host)
        self.headers = headers or {}


@pytest.fixture
def container_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    table = tmp_path / "route"
    table.write_text(CONTAINER_ROUTES, encoding="ascii")
    monkeypatch.setattr(header_trust, "ROUTE_TABLE", table)
    return table


# ─── reading the gateway ─────────────────────────────────────────────────────


def test_the_default_route_gateway_is_read_from_the_kernel_table(container_routes):
    assert default_gateways() == frozenset({GATEWAY})


def test_only_default_routes_through_a_gateway_count(tmp_path):
    table = tmp_path / "route"
    table.write_text(
        CONTAINER_ROUTES.splitlines(keepends=True)[0]
        # A default route without RTF_GATEWAY (a point-to-point link) names no gateway.
        + "tun0\t00000000\t0101A8C0\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
        # A non-default route through a gateway is not the default gateway.
        + "eth1\t0000A8C0\t0201A8C0\t0003\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        + "garbage line\n",
        encoding="ascii",
    )
    assert default_gateways(table) == frozenset()


def test_no_routing_table_leaves_the_allowlist_as_configured(tmp_path):
    assert default_gateways(tmp_path / "missing") == frozenset()
    networks = parse_trusted_proxies(ALLOWLIST)
    assert header_source_is_trusted(_Request(GATEWAY), networks)


# ─── the trust decision ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "allowlist",
    [ALLOWLIST, "172.16.0.0/12,10.200.0.0/16", f"{GATEWAY}/32"],
    ids=["derived-subnet", "wide-ranges", "gateway-by-name"],
)
def test_the_gateway_is_never_trusted_whatever_the_allowlist(container_routes, allowlist):
    networks = parse_trusted_proxies(allowlist)
    assert not header_source_is_trusted(_Request(GATEWAY), networks)


def test_the_proxy_container_on_the_same_network_is_still_trusted(container_routes):
    networks = parse_trusted_proxies(ALLOWLIST)
    assert header_source_is_trusted(_Request(NGINX), networks)


def test_a_forged_dn_from_the_gateway_mints_no_identity(container_routes):
    """The measured attack, through the function that turns a DN header into a login."""
    networks = parse_trusted_proxies(ALLOWLIST)
    forged = _Request(GATEWAY, {DN_HEADER: ADMIN_DN})
    assert (
        _extract_user_info_from_request(
            forged, None, None, networks=networks, cert_dn_header=DN_HEADER
        )
        is None
    )
    assert _validate_pki_headers_source(forged, networks, cert_dn_header=DN_HEADER) is False

    via_nginx = _Request(NGINX, {DN_HEADER: ADMIN_DN})
    identity = _extract_user_info_from_request(
        via_nginx, None, None, networks=networks, cert_dn_header=DN_HEADER
    )
    assert identity is not None and identity[0] == ADMIN_DN
    assert _validate_pki_headers_source(via_nginx, networks, cert_dn_header=DN_HEADER) is True


def test_the_refusal_names_the_gateway_so_an_operator_can_fix_the_topology(
    container_routes, caplog
):
    networks = parse_trusted_proxies(ALLOWLIST)
    with caplog.at_level(logging.WARNING, logger="app.auth.header_trust"):
        permitted = header_assertion_permitted(
            _Request(GATEWAY, {"X-Forwarded-Email": "a@example.com"}),
            networks,
            asserted=True,
            method="proxy",
            setting_name="PROXY_TRUSTED_PROXIES",
        )
    assert permitted is False
    gateway_lines = [r.getMessage() for r in caplog.records if "default gateway" in r.getMessage()]
    assert gateway_lines and GATEWAY in gateway_lines[0], caplog.text


def test_the_pki_smoke_script_logs_in_through_mtls_not_a_forged_header():
    """``test-pki-auth.sh`` used to POST a bare DN header to the backend port from the host,
    which is the forgery above; it now presents the client certificate to the mTLS nginx."""
    script = (
        Path(__file__).resolve().parents[3] / "scripts" / "pki" / "test-pki-auth.sh"
    ).read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert f'-H "{DN_HEADER}' not in code
    assert '--cert "$cert_file" --key "$key_file"' in code
    assert '"${PKI_URL}/api/auth/pki/authenticate"' in code
