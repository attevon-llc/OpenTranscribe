"""The default `--with-pki` trusted-proxy allowlist must cover Docker's real IPAM range (#615).

`./opentr.sh ... --with-pki` — dev, prod, and `--fresh` alike — never asks an operator for
`PKI_TRUSTED_PROXIES`; it always goes through `scripts/pki/generate-test-env.sh`
(`opentr.sh`'s `add_pki_overlay()`), whose own hardcoded default was
`127.0.0.1/32,172.16.0.0/12`. That is not Docker's whole auto-assigned bridge-network range:
once the default pools (172.17.0.0/16-172.31.0.0/16) are exhausted by other concurrent Docker
networks on the host, the daemon spills into `192.168.0.0/16` chunks.

Measured live while building this fix, on a host running ~34 unrelated Docker networks:

    $ docker network inspect opentranscribe_default --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
    192.168.96.0/20

That is the **ordinary, non-fresh, already-running main dev stack's own network** — not a
`--fresh` deployment — already outside the old default. `app/auth/pki_auth.py`'s trusted-proxy
check (`header_trust.header_source_is_trusted`) is fail-closed by design: a peer outside the
allowlist is refused, silently, with no error surfaced to the user (issue #615's original
symptom — a valid client cert, page stays on `/login`). This is why the default is widened
(`127.0.0.1/32,172.16.0.0/12,192.168.0.0/16`) rather than only pinning a subnet for `--fresh`:
a pinned `--fresh` subnet would not have fixed the plain non-fresh stack measured above, since
that stack's network was never `--fresh` at all.

Both added ranges are private RFC1918 space Docker itself hands out for its own bridge
networks — never attacker-reachable from outside the host — so widening the allowlist does not
change what this control actually defends against (a header injected by something outside our
own docker network); it only stops the allowlist missing the range Docker actually used.

Two tiers:

1. Static — `scripts/pki/generate-test-env.sh`'s hardcoded default literally contains both
   ranges (a `--print` dry-run needs pre-existing test certs this checkout may not have, so the
   default is read directly out of the script source, the same tier
   `test_opentr_gpu_device_flag.py` uses for `opentr.sh` itself).
2. Behavioural — the real trust-check code path (`app.auth.header_trust`) parses that exact
   default string and (a) trusts the literal IP measured above plus a `--fresh`-shaped
   192.168.x address from the issue, while (b) still refusing an address outside all three
   ranges — proving the widening did not turn the fail-closed check into an allow-all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.auth.header_trust import ip_in_networks
from app.auth.header_trust import parse_trusted_proxies

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATE_TEST_ENV = REPO_ROOT / "scripts" / "pki" / "generate-test-env.sh"

pytestmark = pytest.mark.skipif(
    not GENERATE_TEST_ENV.exists(), reason="scripts/pki/generate-test-env.sh not present"
)


def _script_default() -> str:
    source = GENERATE_TEST_ENV.read_text(encoding="utf-8")
    match = re.search(r'^TRUSTED_PROXIES="([^"]+)"', source, re.MULTILINE)
    assert match, "generate-test-env.sh no longer declares a TRUSTED_PROXIES default"
    return match.group(1)


# ─── static ─────────────────────────────────────────────────────────────────


def test_default_covers_both_docker_ipam_ranges():
    default = _script_default()
    ranges = {entry.strip() for entry in default.split(",")}
    assert "172.16.0.0/12" in ranges, (
        f"default {default!r} dropped the original 172.16.0.0/12 range"
    )
    assert "192.168.0.0/16" in ranges, (
        f"default {default!r} does not cover Docker's spillover range (issue #615)"
    )
    assert "127.0.0.1/32" in ranges, f"default {default!r} dropped the loopback entry"


def test_help_text_default_matches_the_actual_default():
    """The `--trusted-proxies` help line is prose, not code — it drifts silently."""
    source = GENERATE_TEST_ENV.read_text(encoding="utf-8")
    help_match = re.search(r"--trusted-proxies CIDR\[,CIDR\.\.\.\]\s+\(default: ([^)]+)\)", source)
    assert help_match, "generate-test-env.sh's usage text no longer documents a default"
    assert help_match.group(1) == _script_default(), (
        "the --trusted-proxies help text default has drifted from the real TRUSTED_PROXIES "
        "default — an operator reading --help would be told the wrong value"
    )


# ─── behavioural: the real fail-closed trust check ─────────────────────────


def test_the_measured_non_fresh_stack_ip_is_now_trusted():
    """The exact regression: the ordinary main stack's own network, not `--fresh`."""
    networks = parse_trusted_proxies(_script_default(), label="PKI trusted proxy")
    # 192.168.96.0/20 was `opentranscribe_default`'s measured subnet; .1 is its gateway,
    # the address a container-originated request from that network's frontend would carry.
    assert ip_in_networks("192.168.96.1", networks)


def test_a_fresh_deployment_ip_from_the_issue_is_now_trusted():
    """Issue #615's own repro: `otfresh-verify593`'s frontend container at 192.168.128.9."""
    networks = parse_trusted_proxies(_script_default(), label="PKI trusted proxy")
    assert ip_in_networks("192.168.128.9", networks)


def test_the_original_172_range_is_still_trusted():
    """The widening must be additive — the range that already worked must keep working."""
    networks = parse_trusted_proxies(_script_default(), label="PKI trusted proxy")
    assert ip_in_networks("172.20.0.5", networks)
    assert ip_in_networks("127.0.0.1", networks)


@pytest.mark.parametrize(
    "untrusted_ip",
    [
        "203.0.113.7",  # TEST-NET-3, a public address — must never be trusted
        "10.5.5.5",  # RFC1918 but outside every configured range
        "8.8.8.8",  # a real public IP, for good measure
    ],
)
def test_an_ip_outside_every_configured_range_is_still_refused(untrusted_ip):
    """The widening must not become an allow-all — fail-closed stays fail-closed."""
    networks = parse_trusted_proxies(_script_default(), label="PKI trusted proxy")
    assert not ip_in_networks(untrusted_ip, networks)


def test_an_empty_allowlist_still_trusts_nobody():
    """The fail-closed floor this whole allowlist sits on must survive the widening."""
    networks = parse_trusted_proxies("", label="PKI trusted proxy")
    assert networks == []
    assert not ip_in_networks("192.168.96.1", networks)
    assert not ip_in_networks("127.0.0.1", networks)
