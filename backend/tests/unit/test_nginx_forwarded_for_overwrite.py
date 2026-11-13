"""nginx must hand the backend ONE X-Forwarded-For entry: the client it actually saw.

The public demo let one client dodge every per-IP rate limit: 60 requests, each with a
different forged ``X-Forwarded-For``, never hit 429. nginx used ``$proxy_add_x_forwarded_for``,
which nginx.org documents as "the X-Forwarded-For client request header field with the
$remote_addr variable appended to it". The client's own value therefore reached the backend,
and whenever the address nginx saw was itself inside ``RATE_LIMIT_TRUSTED_PROXIES`` (the Docker
gateway every host-local or docker-proxy connection arrives from sits in 172.16.0.0/12) the
backend's right-to-left walk stepped over it and returned the forged entry.

MDN (X-Forwarded-For, "Security and privacy concerns"): any security use "must only use IP
addresses added by a trusted proxy". nginx is the edge, so the only address it can vouch for is
``$remote_addr`` (already replaced by the realip module when a trusted hop such as Cloudflare
sits in front). These tests read each nginx config the stack ships, apply nginx's documented
semantics for the value it sets, and feed the result through the backend's real
``resolve_client_ip`` with the trust list the compose overlays default to.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from starlette.requests import Request

from app.utils import client_ip

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every nginx config that proxies to the backend: the HTTPS overlay, the production frontend
#: (the front door without that overlay), its PKI variant, and the PKI dev proxy.
NGINX_CONFS = (
    "nginx/site.conf.template",
    "frontend/nginx.conf",
    "frontend/nginx-pki.conf",
    "scripts/pki/nginx-pki-dev.conf",
)

#: Docker's bridge gateway: what nginx sees for a request from the host itself.
DOCKER_GATEWAY = "172.18.0.1"
NGINX_CONTAINER = "172.18.0.5"
FORGED = [f"198.51.100.{n}" for n in range(1, 61)]

_XFF_DIRECTIVE = re.compile(r"^\s*proxy_set_header\s+X-Forwarded-For\s+(\S+?);", re.M | re.I)


def _forwarded_for_values(conf: str) -> list[str]:
    text = (REPO_ROOT / conf).read_text(encoding="utf-8")
    return _XFF_DIRECTIVE.findall(text)


def _nginx_sets(expression: str, client_header: str | None, remote_addr: str) -> str:
    """The header nginx sends upstream for one of the two variables (nginx.org proxy module)."""
    if expression == "$remote_addr":
        return remote_addr
    if expression == "$proxy_add_x_forwarded_for":
        return f"{client_header}, {remote_addr}" if client_header else remote_addr
    raise AssertionError(f"unmodelled X-Forwarded-For value {expression!r}")


def _overlay_default_trust() -> str:
    """The RATE_LIMIT_TRUSTED_PROXIES default the demo overlay gives the backend."""
    env = yaml.safe_load((REPO_ROOT / "docker-compose.demo.yml").read_text(encoding="utf-8"))
    raw = env["services"]["backend"]["environment"]["RATE_LIMIT_TRUSTED_PROXIES"]
    match = re.fullmatch(r"\$\{RATE_LIMIT_TRUSTED_PROXIES:-(.+)\}", raw)
    assert match, raw
    return match.group(1)


@pytest.fixture
def overlay_trust(monkeypatch):
    monkeypatch.setattr(
        client_ip, "_TRUSTED_NETWORKS", client_ip._parse_networks(_overlay_default_trust())
    )


def _backend_key(conf: str, peer: str, forged: str | None) -> str:
    (expression,) = set(_forwarded_for_values(conf))
    header = _nginx_sets(expression, forged, peer)
    request = Request(
        {
            "type": "http",
            "client": (NGINX_CONTAINER, 40000),
            "headers": [(b"x-forwarded-for", header.encode())],
        }
    )
    return client_ip.resolve_client_ip(request)


@pytest.mark.parametrize("conf", NGINX_CONFS)
def test_every_backend_hop_overwrites_forwarded_for_with_the_peer(conf):
    values = _forwarded_for_values(conf)

    assert values, f"{conf} sets no X-Forwarded-For at all"
    assert set(values) == {"$remote_addr"}, f"{conf}: {values}"


@pytest.mark.parametrize("conf", NGINX_CONFS)
def test_forged_forwarded_for_from_a_trusted_range_peer_shares_one_bucket(conf, overlay_trust):
    keys = {_backend_key(conf, DOCKER_GATEWAY, forged) for forged in FORGED}

    assert len(keys) == 1, f"{len(keys)} rate-limit buckets for one client"
    assert keys.isdisjoint(FORGED)


@pytest.mark.parametrize("conf", NGINX_CONFS)
def test_forged_forwarded_for_from_a_public_client_is_ignored(conf, overlay_trust):
    assert _backend_key(conf, "203.0.113.9", "10.9.9.9") == "203.0.113.9"


@pytest.mark.parametrize("conf", NGINX_CONFS)
def test_two_real_clients_still_get_separate_buckets(conf, overlay_trust):
    assert _backend_key(conf, "203.0.113.9", None) == "203.0.113.9"
    assert _backend_key(conf, "203.0.113.10", None) == "203.0.113.10"
