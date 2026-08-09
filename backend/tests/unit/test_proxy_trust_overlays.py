"""Every overlay that puts a proxy in front of the backend must set the proxy trust.

``RATE_LIMIT_TRUSTED_PROXIES`` existed only in ``.env.example``, shipped empty. With
no trusted proxy configured, ``utils/client_ip.resolve_client_ip`` returns the direct
peer — which, behind the bundled nginx, is the nginx container for **every** request.
Consequences, both silent:

* the auth rate limiter buckets everyone together, so one user can exhaust the whole
  deployment's auth limit (and one attacker can lock the login page for everyone);
* the audit log records the proxy's address as the source of every event.

These tests read the compose overlays as data. They are the cheap version of the
control: if someone adds a new proxy overlay, or drops the variable while editing an
existing one, this fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Overlays that terminate client connections in front of the backend. nginx.yml adds
#: a dedicated reverse proxy; prod.yml makes the frontend container an nginx that
#: proxies /api; the two pki overlays swap in an mTLS-terminating nginx frontend.
PROXY_OVERLAYS = [
    "docker-compose.nginx.yml",
    "docker-compose.prod.yml",
    "docker-compose.pki.yml",
    "docker-compose.pki-dev.yml",
]

VAR = "RATE_LIMIT_TRUSTED_PROXIES"


def _backend_environment(overlay: str) -> dict[str, str]:
    """Return the backend service's ``environment`` mapping from an overlay.

    Compose accepts either a mapping or a ``KEY=value`` list; normalise both.
    """
    with open(REPO_ROOT / overlay) as fh:
        doc = yaml.safe_load(fh)
    env = doc["services"]["backend"].get("environment") or {}
    if isinstance(env, list):
        return dict(entry.split("=", 1) for entry in env if "=" in entry)
    return {str(k): str(v) for k, v in env.items()}


@pytest.mark.unit
@pytest.mark.parametrize("overlay", PROXY_OVERLAYS)
class TestProxyOverlaysConfigureTrust:
    def test_backend_receives_the_trusted_proxy_list(self, overlay):
        env = _backend_environment(overlay)
        assert VAR in env, f"{overlay} puts a proxy in front of the backend but never sets {VAR}"

    def test_value_defaults_to_dockers_own_address_pool(self, overlay):
        """The default must cover the proxy container and nothing beyond the host.

        Published container ports preserve the caller's real source address, so
        trusting 10/8 or 192.168.0.0/16 would let any machine on a typical LAN reach
        the backend port directly and forge its own X-Forwarded-For — the very
        spoofing the resolver refuses to allow by default.
        """
        value = str(_backend_environment(overlay)[VAR])

        assert "172.16.0.0/12" in value, f"{overlay}: Docker's address pool must be trusted"
        assert "10.0.0.0/8" not in value, f"{overlay}: must not blanket-trust a LAN range"
        assert "192.168.0.0/16" not in value, f"{overlay}: must not blanket-trust a LAN range"

    def test_operator_can_override_from_env(self, overlay):
        """An external proxy or a custom Docker pool must be configurable."""
        value = str(_backend_environment(overlay)[VAR])
        assert value.startswith(f"${{{VAR}:-"), (
            f"{overlay}: the value must be an overridable ${{{VAR}:-default}} expansion"
        )


@pytest.mark.unit
def test_dev_overlay_keeps_its_direct_connection_posture():
    """The plain dev stack has no proxy — adding trust there would only add risk.

    Dev's relaxed auth limits live in the same file and are deliberately untouched by
    this change.
    """
    env = _backend_environment("docker-compose.override.yml")

    assert VAR not in env, "the proxy-less dev overlay must not trust forwarding headers"
