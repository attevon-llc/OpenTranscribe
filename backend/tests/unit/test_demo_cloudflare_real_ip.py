"""The demo behind Cloudflare: the visitor address comes from CF-Connecting-IP, from Cloudflare only (#628).

nginx's ``ngx_http_realip_module`` replaces the client address with ``real_ip_header`` only for
peers matched by ``set_real_ip_from``; everyone else's header is ignored. These tests pin the
config that makes that so (the committed snapshot, the refresh script, the overlay and its
selection by the installer), then feed the address nginx would then hand over through the
backend's real ``resolve_client_ip``. The live check against a real nginx and Cloudflare is
documented in ``docs-site/docs/operations/public-demo.md``.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from starlette.requests import Request

from app.utils import client_ip
from tests.unit.test_compose_file_selection import DEMO
from tests.unit.test_compose_file_selection import DEMO_CLOUDFLARE
from tests.unit.test_compose_file_selection import NGINX
from tests.unit.test_compose_file_selection import _files
from tests.unit.test_compose_file_selection import _resolve

REPO_ROOT = Path(__file__).resolve().parents[3]
CONF = REPO_ROOT / "nginx" / "cloudflare-real-ip.conf"
SCRIPT = REPO_ROOT / "scripts" / "demo" / "refresh-cloudflare-ips.sh"
OVERLAY = DEMO_CLOUDFLARE

V4 = ["173.245.48.0/20", "104.16.0.0/13"]
V6 = ["2400:cb00::/32", "2a06:98c0::/29"]


def _ranges(text: str) -> list[str]:
    return re.findall(r"^set_real_ip_from\s+(\S+);$", text, flags=re.M)


def _nginx_client_address(peer: str, headers: dict[str, str], conf: str) -> str:
    """The address nginx's realip module would use (documented semantics, recursive off)."""
    networks = [ipaddress.ip_network(r) for r in _ranges(conf)]
    header = re.search(r"^real_ip_header\s+(\S+);$", conf, flags=re.M)
    assert header, "no real_ip_header"
    value = headers.get(header.group(1))
    if value and any(ipaddress.ip_address(peer) in n for n in networks):
        return value
    return peer


def _request(peer: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "client": (peer, 50000),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


@pytest.fixture
def nginx_is_the_only_trusted_proxy(monkeypatch):
    monkeypatch.setattr(client_ip, "_TRUSTED_NETWORKS", client_ip._parse_networks("172.16.0.0/12"))


def _backend_sees(peer: str, cf_header: str, xff: str | None = None) -> str:
    """nginx -> backend: nginx appends $remote_addr to the client's X-Forwarded-For."""
    conf = CONF.read_text(encoding="utf-8")
    seen = _nginx_client_address(peer, {"CF-Connecting-IP": cf_header}, conf)
    chain = f"{xff}, {seen}" if xff else seen
    return client_ip.resolve_client_ip(_request("172.18.0.5", {"X-Forwarded-For": chain}))


def test_the_snapshot_trusts_only_cloudflare_ranges_and_the_cloudflare_header():
    text = CONF.read_text(encoding="utf-8")
    ranges = _ranges(text)

    assert set(V4 + V6) <= set(ranges)
    assert all(ipaddress.ip_network(r).prefixlen >= 8 for r in ranges), "a range is far too wide"
    assert re.findall(r"^real_ip_header\s+(\S+);$", text, flags=re.M) == ["CF-Connecting-IP"]
    assert not re.search(r"^\s*real_ip_recursive\s+on", text, flags=re.M)


def test_a_visitor_header_from_a_non_cloudflare_peer_is_ignored(nginx_is_the_only_trusted_proxy):
    assert _backend_sees("203.0.113.9", "198.51.100.77") == "203.0.113.9"


def test_cloudflare_peers_header_is_the_visitor(nginx_is_the_only_trusted_proxy):
    assert _backend_sees("104.16.0.7", "198.51.100.77") == "198.51.100.77"
    assert _backend_sees("2606:4700::1", "198.51.100.77") == "198.51.100.77"


def test_a_forged_forwarded_for_behind_cloudflare_does_not_pick_the_bucket(
    nginx_is_the_only_trusted_proxy,
):
    assert _backend_sees("104.16.0.7", "198.51.100.77", xff="1.2.3.4") == "198.51.100.77"


def test_the_application_never_reads_the_cloudflare_header_itself():
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "backend" / "app").rglob("*.py")
        if "cf-connecting-ip" in p.read_text(encoding="utf-8").lower()
    ]

    assert offenders == []


def _run_script(tmp_path: Path, v4: str, v6: str, *args: str) -> subprocess.CompletedProcess:
    (tmp_path / "v4").write_text(v4)
    (tmp_path / "v6").write_text(v6)
    env = {
        **os.environ,
        "CF_IPS_V4_URL": f"file://{tmp_path}/v4",
        "CF_IPS_V6_URL": f"file://{tmp_path}/v6",
        "CF_REAL_IP_CONF": str(tmp_path / "out.conf"),
    }
    return subprocess.run(
        [str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def test_the_refresh_script_writes_the_lists_it_was_given(tmp_path):
    done = _run_script(tmp_path, "\n".join(V4) + "\n", "\n".join(V6) + "\n")

    assert done.returncode == 0, done.stderr
    out = (tmp_path / "out.conf").read_text()
    assert _ranges(out) == V4 + V6
    assert out.rstrip().endswith("real_ip_header CF-Connecting-IP;")
    assert (
        _run_script(tmp_path, "\n".join(V4) + "\n", "\n".join(V6) + "\n", "--check").returncode == 0
    )
    assert _run_script(tmp_path, "10.0.0.0/8\n", "\n".join(V6) + "\n", "--check").returncode == 1


@pytest.mark.parametrize(
    "v4", ["<html>429 Too Many Requests</html>\n", "", "1.2.3.4\n", "1.2.3.4/24; deny all\n"]
)
def test_the_refresh_script_refuses_anything_that_is_not_a_cidr_list(tmp_path, v4):
    done = _run_script(tmp_path, v4, "\n".join(V6) + "\n")

    assert done.returncode == 2
    assert not (tmp_path / "out.conf").exists()


def test_the_overlay_mounts_the_snapshot_into_the_http_level_include_dir():
    svc = yaml.safe_load((REPO_ROOT / OVERLAY).read_text(encoding="utf-8"))["services"]

    assert list(svc) == ["nginx"]
    assert svc["nginx"]["volumes"] == [
        "./nginx/cloudflare-real-ip.conf:/etc/nginx/conf.d/cloudflare-real-ip.conf:ro"
    ]


def test_installer_adds_the_overlay_to_a_demo_behind_nginx_and_only_then(tmp_path):
    base = ("DEMO_MODE=true", "NGINX_SERVER_NAME=demo.example.org")
    overlays = (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.lite.yml",
        NGINX,
        DEMO,
        OVERLAY,
    )
    install = tmp_path / "with"
    install.mkdir()
    chain, _ = _resolve(install, overlays=overlays, env_lines=base, certs=True)
    assert _files(chain)[-2:] == [DEMO, OVERLAY]

    no_nginx = tmp_path / "without"
    no_nginx.mkdir()
    chain, _ = _resolve(
        no_nginx,
        overlays=tuple(o for o in overlays if o != NGINX),
        env_lines=("DEMO_MODE=true",),
    )
    assert OVERLAY not in _files(chain)
