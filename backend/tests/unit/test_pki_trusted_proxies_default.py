"""`--with-pki` must trust its own docker network — and nothing on the LAN (#615, #620).

`./opentr.sh ... --with-pki` — dev, prod and `--fresh` alike — never asks an operator for
`PKI_TRUSTED_PROXIES`; it always goes through `scripts/pki/generate-test-env.sh`
(`opentr.sh`'s `add_pki_overlay()`), which writes that value **and**
`RATE_LIMIT_TRUSTED_PROXIES` into the whole stack.

Those two settings are not the same kind of thing, and the difference is the whole point of
this file:

* `RATE_LIMIT_TRUSTED_PROXIES` decides whose `X-Forwarded-For` is believed for per-IP rate
  limiting, lockout and the audit trail. Too wide costs attribution.
* `PKI_TRUSTED_PROXIES` decides whether a bare `X-Client-Cert-DN` header is believed **as an
  identity**. `pki_auth._extract_user_info_from_request` accepts a DN with *no certificate at
  all* whenever `header_trust.header_source_is_trusted()` is true, and `pki_mode` defaults to
  `header`, so DN-only is the default transport. Too wide is unauthenticated admin
  impersonation — the admin DN is not a secret (`setup-test-pki.sh` hardcodes it, and
  `pkiadmin` is the generator's own default `--admin-cert`).

The two failure modes this default sits between:

* **Too narrow** (#615): the allowlist misses the subnet docker actually used, the fail-closed
  trust check refuses the PKI nginx, and sign-in fails *silently* — a valid client cert, page
  stays on `/login`. Docker spills out of its `172.17.0.0/16`-`172.31.0.0/16` pools into
  `192.168.0.0/16` `/20` chunks once a host has enough networks; measured live, the ordinary
  non-`--fresh` `opentranscribe_default` network landed on `192.168.96.0/20`.
* **Too wide** (#620): the previous fix for the above was a blanket `192.168.0.0/16`, which is
  the range ordinary consumer/office routers hand out. On such a LAN every other device could
  POST a forged DN header to the published backend port and be handed admin tokens.

The resolution is to stop guessing: ask docker which subnet it gave this compose project. That
covers #615 exactly, and it is safe by construction against #620 — docker's IPAM refuses a pool
overlapping an existing host route, so a subnet it allocated is never the LAN this host is on.

Every test here drives the real script (`--print-trusted-proxies`, which resolves the allowlist
and exits before touching certificates) and feeds the result to the real trust check in
`app.auth.header_trust`. Docker is stubbed rather than required, so the *fallback* path — the
one that runs on a first-ever start, before the network exists — is measured too instead of
being taken on faith.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.auth.header_trust import UNKNOWN_PEER
from app.auth.header_trust import header_source_is_trusted
from app.auth.header_trust import ip_in_networks
from app.auth.header_trust import parse_trusted_proxies

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATE_TEST_ENV = REPO_ROOT / "scripts" / "pki" / "generate-test-env.sh"

#: Externals the script reaches for before it exits on `--print-trusted-proxies`.
_REQUIRED_TOOLS = ("dirname", "basename", "tr")

pytestmark = pytest.mark.skipif(
    not GENERATE_TEST_ENV.exists(), reason="scripts/pki/generate-test-env.sh not present"
)


class _FakeRequest:
    """The two attributes the trust check and the DN extractor read."""

    class _Client:
        def __init__(self, host: str) -> None:
            self.host = host

    def __init__(self, host: str | None, headers: dict[str, str] | None = None) -> None:
        self.client = None if host is None else self._Client(host)
        self.headers = headers or {}


def _isolated_bin(tmp_path: Path, docker_script: str | None) -> Path:
    """A PATH containing only the tools the script needs, plus an optional fake docker.

    Trimming PATH is what makes "docker is not installed" testable: the real binary lives in
    a system directory that cannot simply be removed from the inherited PATH.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in _REQUIRED_TOOLS:
        resolved = shutil.which(tool)
        assert resolved, f"{tool} is required to run generate-test-env.sh"
        (bin_dir / tool).symlink_to(resolved)

    if docker_script is not None:
        fake = bin_dir / "docker"
        fake.write_text(docker_script, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _resolve_allowlist(
    tmp_path: Path,
    *,
    docker_script: str | None = None,
    project_name: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> str:
    bash = shutil.which("bash")
    assert bash, "bash is required to run generate-test-env.sh"

    env = {"PATH": str(_isolated_bin(tmp_path, docker_script)), "HOME": str(tmp_path)}
    if project_name is not None:
        env["COMPOSE_PROJECT_NAME"] = project_name

    completed = subprocess.run(
        [bash, str(GENERATE_TEST_ENV), "--print-trusted-proxies", *extra_args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        f"--print-trusted-proxies exited {completed.returncode}: {completed.stderr}"
    )
    return completed.stdout.strip()


#: A stub daemon that answers for one network and 404s for anything else, exactly as
#: `docker network inspect` does.
_FAKE_DOCKER = """#!/bin/sh
if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then
  case "$3" in
    %(network)s) echo "%(subnet)s "; exit 0 ;;
    *) echo "Error: No such network: $3" >&2; exit 1 ;;
  esac
fi
exit 1
"""

#: A daemon that is installed but unreachable — the shape of `docker` on a host where the
#: service is stopped, which must land on the fallback rather than on an empty allowlist.
_UNREACHABLE_DOCKER = """#!/bin/sh
echo "Cannot connect to the Docker daemon" >&2
exit 1
"""


def _fake_docker(network: str, subnet: str) -> str:
    return _FAKE_DOCKER % {"network": network, "subnet": subnet}


# ─── the derived allowlist ──────────────────────────────────────────────────


def test_the_allowlist_is_the_projects_own_subnet_not_a_private_range(tmp_path):
    """#615's measured subnet, derived — not the /16 it sits inside."""
    allowlist = _resolve_allowlist(
        tmp_path,
        docker_script=_fake_docker("opentranscribe_default", "192.168.96.0/20"),
        project_name="opentranscribe",
    )
    assert allowlist == "127.0.0.1/32,192.168.96.0/20"


def test_a_fresh_deployments_own_network_is_derived_too(tmp_path):
    """`--fresh` exports COMPOSE_PROJECT_NAME before add_pki_overlay runs, so it resolves."""
    allowlist = _resolve_allowlist(
        tmp_path,
        docker_script=_fake_docker("otfresh-verify593_default", "192.168.128.0/20"),
        project_name="otfresh-verify593",
    )
    assert allowlist == "127.0.0.1/32,192.168.128.0/20"


def test_the_measured_non_fresh_gateway_is_still_trusted(tmp_path):
    """#615 must stay fixed: the PKI nginx's own address has to pass the trust check."""
    networks = parse_trusted_proxies(
        _resolve_allowlist(
            tmp_path,
            docker_script=_fake_docker("opentranscribe_default", "192.168.96.0/20"),
            project_name="opentranscribe",
        ),
        label="PKI trusted proxy",
    )
    # .1 is the bridge gateway (a host-side caller arrives as this); .9 is a container.
    assert ip_in_networks("192.168.96.1", networks)
    assert ip_in_networks("192.168.96.9", networks)
    assert ip_in_networks("127.0.0.1", networks)


@pytest.mark.parametrize(
    "lan_ip",
    [
        "192.168.1.50",  # the archetypal home/office router's DHCP range
        "192.168.0.7",
        "192.168.50.12",
        "10.10.10.20",  # a LAN that uses 10/8 instead
        "203.0.113.7",  # TEST-NET-3 — a public address
    ],
)
def test_a_lan_peer_cannot_assert_a_certificate_dn(tmp_path, lan_ip):
    """THE #620 regression test.

    Under the old blanket `192.168.0.0/16` default, every 192.168.* address here was trusted,
    which meant any device on an ordinary LAN could POST a forged `X-Client-Cert-DN` to the
    published backend port and receive admin tokens with no certificate and no password.
    """
    networks = parse_trusted_proxies(
        _resolve_allowlist(
            tmp_path,
            docker_script=_fake_docker("opentranscribe_default", "192.168.96.0/20"),
            project_name="opentranscribe",
        ),
        label="PKI trusted proxy",
    )
    assert not ip_in_networks(lan_ip, networks)
    assert not header_source_is_trusted(_FakeRequest(lan_ip), networks)


def test_a_lan_peer_gets_no_identity_out_of_the_real_extractor(tmp_path):
    """The same refusal one level up, through the function that actually mints the identity.

    `header_source_is_trusted` returning False is the mechanism; this is the consequence. With
    HEAD's `127.0.0.1/32,172.16.0.0/12,192.168.0.0/16` default this call returned the admin DN
    — a certificate-free, password-free admin login for any device on a 192.168/16 LAN.
    """
    from app.auth.pki_auth import _extract_user_info_from_request

    admin_dn = "CN=PKI Admin User,OU=IT,O=OpenTranscribe,C=US"
    request = _FakeRequest("192.168.1.50", {"X-Client-Cert-DN": admin_dn})
    networks = parse_trusted_proxies(
        _resolve_allowlist(
            tmp_path,
            docker_script=_fake_docker("opentranscribe_default", "192.168.96.0/20"),
            project_name="opentranscribe",
        ),
        label="PKI trusted proxy",
    )

    assert (
        _extract_user_info_from_request(
            request,
            cert=None,
            cert_pem=None,
            require_certificate=False,
            networks=networks,
            cert_dn_header="X-Client-Cert-DN",
        )
        is None
    )

    # The control: the same call from inside the derived network still authenticates, so the
    # refusal above is about WHERE the header came from, not a blanket break of DN-only mode.
    from_the_proxy = _FakeRequest("192.168.96.1", {"X-Client-Cert-DN": admin_dn})
    extracted = _extract_user_info_from_request(
        from_the_proxy,
        cert=None,
        cert_pem=None,
        require_certificate=False,
        networks=networks,
        cert_dn_header="X-Client-Cert-DN",
    )
    assert extracted is not None
    assert extracted[0] == admin_dn


# ─── the fallback path ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case", "docker_script"),
    [
        ("docker not installed", None),
        ("docker daemon unreachable", _UNREACHABLE_DOCKER),
        ("project network does not exist yet", _fake_docker("someone_elses", "172.29.0.0/16")),
    ],
)
def test_the_fallback_is_dockers_own_pool_and_excludes_the_lan(tmp_path, case, docker_script):
    """A first-ever `--with-pki` start creates the network only during `up`, so the fallback
    is a path that really runs — it must not be the wide default it replaced."""
    allowlist = _resolve_allowlist(
        tmp_path, docker_script=docker_script, project_name="opentranscribe"
    )
    assert allowlist == "127.0.0.1/32,172.16.0.0/12", case

    networks = parse_trusted_proxies(allowlist, label="PKI trusted proxy")
    assert ip_in_networks("172.20.0.5", networks), case
    assert ip_in_networks("127.0.0.1", networks), case
    assert not ip_in_networks("192.168.1.50", networks), case
    assert not ip_in_networks("10.10.10.20", networks), case


def test_an_explicit_allowlist_wins_over_derivation(tmp_path):
    """The operator escape hatch for a topology the derivation cannot see."""
    allowlist = _resolve_allowlist(
        tmp_path,
        docker_script=_fake_docker("opentranscribe_default", "192.168.96.0/20"),
        project_name="opentranscribe",
        extra_args=("--trusted-proxies", "10.1.2.0/24"),
    )
    assert allowlist == "10.1.2.0/24"


# ─── static: no wide range may return as a coded default ────────────────────


@pytest.mark.parametrize("wide_range", ["192.168.0.0/16", "10.0.0.0/8", "0.0.0.0/0"])
def test_no_lan_wide_range_is_hardcoded_as_a_default(wide_range):
    """A derived value can regress back into a literal in one careless edit."""
    source = GENERATE_TEST_ENV.read_text(encoding="utf-8")
    code_lines = [
        line
        for line in source.splitlines()
        if wide_range in line and not line.lstrip().startswith("#")
    ]
    assert not code_lines, (
        f"generate-test-env.sh hardcodes {wide_range} outside a comment: {code_lines}. "
        "That range is what ordinary LAN routers issue; see issue #620."
    )


def test_the_help_text_describes_the_derivation_rather_than_a_stale_literal():
    """The `--trusted-proxies` help line is prose, and prose drifts. It used to name a literal
    default; naming one again is how an operator gets told the wrong value."""
    source = GENERATE_TEST_ENV.read_text(encoding="utf-8")
    help_line = next(
        (line for line in source.splitlines() if "--trusted-proxies CIDR" in line), None
    )
    assert help_line, "generate-test-env.sh's usage text no longer documents --trusted-proxies"
    assert "derived" in help_line, help_line


# ─── the fail-closed floor everything above sits on ─────────────────────────


def test_an_empty_allowlist_still_trusts_nobody():
    networks = parse_trusted_proxies("", label="PKI trusted proxy")
    assert networks == []
    assert not ip_in_networks("192.168.96.1", networks)
    assert not ip_in_networks("127.0.0.1", networks)
    assert not header_source_is_trusted(_FakeRequest("127.0.0.1"), networks)


def test_a_peerless_transport_is_refused_like_any_other_untrusted_peer():
    """ASGI test clients and unix sockets expose no peer; that must not read as trusted."""
    networks = parse_trusted_proxies("127.0.0.1/32,192.168.96.0/20", label="PKI trusted proxy")
    assert not ip_in_networks(UNKNOWN_PEER, networks)
    assert not header_source_is_trusted(_FakeRequest(None), networks)


def test_the_generator_is_executable_from_a_worktree_without_env():
    """The script must not need `.env`; opentr.sh calls it from any checkout."""
    assert os.access(GENERATE_TEST_ENV, os.X_OK), "generate-test-env.sh is not executable"
