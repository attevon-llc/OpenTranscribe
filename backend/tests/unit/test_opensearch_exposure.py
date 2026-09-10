"""OpenSearch's published ports must stay loopback-only (issue #858).

The security plugin ships disabled for every deployment mode (docker-compose.yml's
`DISABLE_SECURITY_PLUGIN=true`, restated in docker-compose.prod.yml). That is
tolerable ONLY because the cluster's ports are bound to 127.0.0.1 -- an
unauthenticated OpenSearch holding every transcript, speaker embedding and audit
log record must never be reachable off-host. This test pins the property the
whole "safe by default" argument rests on, so a future overlay cannot quietly
publish 9200 on 0.0.0.0 while the plugin stays off.

Parses every tracked `docker-compose*.yml` with PyYAML (these are ordinary YAML,
not templates -- unlike nginx/site.conf.template, which is why a real YAML parser
is used here instead of a hand-rolled one). For any file whose `services:` block
defines a REAL `opensearch` service (not merely a `depends_on: {opensearch: ...}`
reference -- docker-compose.blackwell.yml has exactly that shape and no
`opensearch` service of its own), every `ports:` entry -- in both the short
string form (`"127.0.0.1:5180:9200"`) and the long mapping form
(`{published: ..., target: ...}`) -- must be loopback-bound.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _compose_files() -> list[Path]:
    files = sorted(REPO_ROOT.glob("docker-compose*.yml"))
    assert files, f"no docker-compose*.yml files found under {REPO_ROOT}"
    return files


def _opensearch_service(compose_path: Path) -> dict[str, Any] | None:
    """The `opensearch` entry under `services:`, or None if this file declares no
    such service (a bare `depends_on: {opensearch: ...}` reference elsewhere in
    the file does not count -- docker-compose.blackwell.yml is exactly that case
    and must be treated as "nothing to check", not "found, ports missing")."""
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = (data or {}).get("services", {}) or {}
    return services.get("opensearch")


def _port_host_bindings(ports_entry: list[Any]) -> list[str]:
    """Extract the host-bind address from every entry in a compose `ports:` list.

    Handles both forms:
      - short string: "127.0.0.1:5180:9200" -> "127.0.0.1"
      - short string, no host: "5180:9200" -> "" (binds all interfaces -- a finding)
      - long mapping: {"target": 9200, "published": "5180", "host_ip": "127.0.0.1"}
    """
    bindings = []
    for entry in ports_entry:
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) >= 3:
                bindings.append(parts[0])
            else:
                # "5180:9200" or a bare "9200" -- no host IP specified at all,
                # which docker publishes on every interface.
                bindings.append("")
        elif isinstance(entry, dict):
            bindings.append(str(entry.get("host_ip", "")))
        else:
            raise TypeError(f"unrecognized ports: entry shape: {entry!r}")
    return bindings


def test_every_opensearch_service_binds_ports_to_loopback_only():
    checked_files: list[str] = []
    for compose_path in _compose_files():
        service = _opensearch_service(compose_path)
        if service is None:
            continue
        ports = service.get("ports")
        if not ports:
            # No ports: key at all means this overlay adds no NEW exposure -- it
            # inherits (or doesn't) whatever the base file already declares, which
            # is checked separately when this loop reaches docker-compose.yml.
            continue

        checked_files.append(compose_path.name)
        bindings = _port_host_bindings(ports)
        for binding, raw_entry in zip(bindings, ports, strict=True):
            assert binding == "127.0.0.1", (
                f"{compose_path.name}: opensearch port entry {raw_entry!r} is not "
                f"loopback-bound (host_ip={binding!r}) -- this would expose an "
                f"unauthenticated OpenSearch cluster (security plugin disabled by "
                f"default) beyond localhost"
            )

    assert "docker-compose.yml" in checked_files, (
        "the base compose file's opensearch ports: block was not found/checked -- "
        "the parser or the file layout changed under this test"
    )


def test_blackwell_compose_has_no_standalone_opensearch_service():
    """docker-compose.blackwell.yml has no `opensearch:` service of its own, only a
    `depends_on: opensearch` reference -- confirming _opensearch_service correctly
    returns None for it rather than silently reporting nothing to check because of
    a parsing mismatch."""
    path = REPO_ROOT / "docker-compose.blackwell.yml"
    assert path.exists()
    assert _opensearch_service(path) is None
