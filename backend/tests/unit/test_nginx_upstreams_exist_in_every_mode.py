"""Every nginx config the stack serves must start in every deployment mode that loads it.

nginx resolves a literal host in ``proxy_pass`` once, while it parses the config, and refuses
to start when the name does not resolve ("host not found in upstream"). Inside a compose
network a service name resolves only while that service has a running container, so a
literal upstream is safe only for a service that is (a) never absent in any mode and
(b) started before the proxy container (a ``depends_on`` of it). ``docker-compose.demo.yml``
scales Flower to 0, and ``frontend/nginx.conf`` named ``flower:5555`` literally: the public
demo's frontend crash-looped on start and the demo could not serve a single page.

An upstream that may be absent is written the documented way instead: a variable in
``proxy_pass`` plus a ``resolver``, so nginx resolves the name per request and a missing
service answers 502 on its own path rather than taking the whole site down
(nginx.org ngx_http_proxy_module ``proxy_pass``: "Parameter value can contain variables. In
this case ... the name ... is determined using a resolver"). The resolver is Docker's
embedded DNS, 127.0.0.11, which serves every user-defined network
(docs.docker.com/engine/network, "DNS services").

The compose model is read from the compose files themselves, so a service scaled to 0 by a
future overlay fails here instead of on a live stack. The dev stack serves the SPA from Vite
and never loads these files, which is why the demo checks run on ``./opentr.sh start dev
--demo`` could not see the crash.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
import yaml

from tests.unit.test_compose_file_selection import BASE
from tests.unit.test_compose_file_selection import DEMO
from tests.unit.test_compose_file_selection import LITE
from tests.unit.test_compose_file_selection import NGINX
from tests.unit.test_compose_file_selection import PROD
from tests.unit.test_compose_file_selection import _files
from tests.unit.test_compose_file_selection import _resolve

REPO_ROOT = Path(__file__).resolve().parents[3]
PKI = "docker-compose.pki.yml"
DOCKER_DNS = "127.0.0.11"

#: Each config the stack serves, and the compose service whose container loads it.
FRONTEND_CONF = "frontend/nginx.conf"
FRONTEND_PKI_CONF = "frontend/nginx-pki.conf"
SITE_TEMPLATE = "nginx/site.conf.template"
CONFIGS = {FRONTEND_CONF: "frontend", FRONTEND_PKI_CONF: "frontend", SITE_TEMPLATE: "nginx"}

_REPLICA_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-(\d+)\}")
_PROXY_PASS = re.compile(r"\bproxy_pass\s+([^;]+);")
_SET = re.compile(r"\bset\s+\$(\w+)\s+([^;]+);")
_RESOLVER = re.compile(r"\bresolver\s+([^;]+);")
_TARGET = re.compile(r"^https?://(?P<var>\$\w+)?(?P<host>[A-Za-z0-9._-]*)")


# --------------------------------------------------------------------------- #
# nginx config reading
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Upstream:
    """One ``proxy_pass`` target: the service host and whether nginx resolves it at start."""

    host: str
    literal: bool


@dataclass(frozen=True)
class Server:
    """What one ``server`` block proxies to, and the resolver declared at its top level."""

    upstreams: tuple[Upstream, ...]
    resolver: str | None


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _blocks(text: str, opener: str) -> list[str]:
    """Bodies of every ``<opener> { ... }`` block, brace-matched."""
    bodies = []
    for match in re.finditer(opener, text):
        start = text.index("{", match.start())
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                bodies.append(text[start + 1 : i])
                break
    return bodies


def _top_level(body: str) -> str:
    """``body`` with every nested ``{ ... }`` removed: only the block's own directives remain."""
    out, depth = [], 0
    for char in body:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def parse_servers(text: str) -> list[Server]:
    text = _strip_comments(text)
    servers = []
    for body in _blocks(text, r"(?m)^\s*server\s*\{"):
        variables = {name: value.strip() for name, value in _SET.findall(body)}
        upstreams = []
        for target in _PROXY_PASS.findall(body):
            match = _TARGET.match(target.strip())
            assert match, f"unrecognised proxy_pass target {target!r}"
            if match["var"]:
                value = variables.get(match["var"][1:], "")
                upstreams.append(Upstream(value.split(":", 1)[0], literal=False))
            else:
                upstreams.append(Upstream(match["host"], literal=True))
        resolver = _RESOLVER.search(_top_level(body))
        servers.append(Server(tuple(upstreams), resolver[1].strip() if resolver else None))
    return servers


# --------------------------------------------------------------------------- #
# compose topology
# --------------------------------------------------------------------------- #


Loader = Callable[[str], dict]


@cache
def _load(name: str) -> dict:
    data: dict = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8")) or {}
    services: dict = data.get("services") or {}
    return services


def _replicas(service: dict | None) -> str | None:
    service = service or {}
    value = (service.get("deploy") or {}).get("replicas", service.get("scale"))
    if value is None:
        return None
    match = _REPLICA_DEFAULT.fullmatch(str(value))
    return match.group(1) if match else str(value)


def _depends_on(service: dict | None) -> set[str]:
    value = (service or {}).get("depends_on") or []
    return set(value)


@dataclass(frozen=True)
class Topology:
    """The services a set of compose files runs, and what starts before what."""

    running: frozenset[str]
    depends_on: dict[str, frozenset[str]]

    def started_before(self, service: str) -> set[str]:
        seen: set[str] = set()
        todo = list(self.depends_on.get(service, ()))
        while todo:
            name = todo.pop()
            if name not in seen:
                seen.add(name)
                todo.extend(self.depends_on.get(name, ()))
        return seen


def topology(files: Iterable[str], loader: Loader = _load) -> Topology:
    """Merge ``files`` in order the way compose does for the keys this check reads."""
    files = list(files)
    base = loader(files[0])
    replicas: dict[str, str | None] = {}
    depends: dict[str, set[str]] = {}
    for name in files:
        for service, spec in loader(name).items():
            replicas[service] = _replicas(spec) or replicas.get(service)
            depends.setdefault(service, set()).update(_depends_on(spec))
    running = {
        service
        for service in replicas
        if replicas[service] != "0" and not (base.get(service) or {}).get("profiles")
    }
    return Topology(frozenset(running), {k: frozenset(v) for k, v in depends.items()})


def _every_compose_file() -> list[str]:
    others = sorted(p.name for p in REPO_ROOT.glob("docker-compose.*.yml"))
    return [BASE, *others]


def may_be_absent(service: str, files: list[str], loader: Loader = _load) -> bool:
    """True when the base lacks ``service``, profile-gates it, or ANY overlay scales it to 0."""
    base = loader(files[0]).get(service)
    if base is None or base.get("profiles"):
        return True
    return any(_replicas(loader(name).get(service)) == "0" for name in files)


def _literal_fault(host: str, proxy: str, files: list[str], every: Topology, loader: Loader) -> str:
    if may_be_absent(host, files, loader):
        return f"literal, but some mode runs no {host!r} container"
    if host not in every.started_before(proxy):
        return f"literal, but {proxy!r} does not depend on it"
    return ""


def _variable_fault(resolver: str | None) -> str:
    if resolver is None:
        return "variable proxy_pass with no server-level resolver"
    if DOCKER_DNS not in resolver.split():
        return f"resolver {resolver!r} is not Docker's DNS"
    return ""


def violations(
    servers: list[Server], proxy: str, files: list[str], loader: Loader = _load
) -> list[str]:
    """Why each upstream in ``servers`` could stop ``proxy`` from starting, or 502 forever."""
    found = []
    every = topology(files, loader)
    for index, server in enumerate(servers):
        for up in server.upstreams:
            if up.host not in every.depends_on:
                fault = "no compose service of that name"
            elif up.literal:
                fault = _literal_fault(up.host, proxy, files, every, loader)
            else:
                fault = _variable_fault(server.resolver)
            if fault:
                found.append(f"server #{index + 1} upstream {up.host!r}: {fault}")
    return found


def _servers(config: str) -> list[Server]:
    return parse_servers((REPO_ROOT / config).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The real configs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("config", sorted(CONFIGS))
def test_every_upstream_can_be_reached_in_every_mode(config: str):
    servers = _servers(config)
    assert sum(len(s.upstreams) for s in servers) >= 5, f"parser saw too little of {config}"

    assert violations(servers, CONFIGS[config], _every_compose_file()) == []


@pytest.mark.parametrize(
    "env_lines",
    [
        ("DEPLOYMENT_MODE=lite", "DEMO_MODE=true"),
        ("DEPLOYMENT_MODE=lite",),
        (),
    ],
    ids=["demo", "lite", "full"],
)
@pytest.mark.parametrize("certs", [False, True], ids=["http", "nginx-overlay"])
def test_the_installers_chain_runs_every_literal_upstream(tmp_path, env_lines, certs):
    """The chain ``opentranscribe.sh`` really selects, per mode, against the real files.

    The demo row is the one that shipped broken: the base frontend image's literal
    ``flower`` on a chain that runs no Flower.
    """
    if certs:
        env_lines = (*env_lines, "NGINX_SERVER_NAME=opentranscribe.local")
    chain, _ = _resolve(tmp_path, nvidia_runtime=False, env_lines=env_lines, certs=certs)
    files = _files(chain)
    assert (DEMO in files) == ("DEMO_MODE=true" in env_lines), chain
    assert (NGINX in files) == certs, chain
    running = topology(files).running

    configs = [FRONTEND_CONF, *([SITE_TEMPLATE] if NGINX in files else [])]
    missing = {
        (config, up.host)
        for config in configs
        for server in _servers(config)
        for up in server.upstreams
        if up.literal and up.host not in running
    }

    assert missing == set(), files


@pytest.mark.parametrize(
    "files",
    [[BASE, PROD, PKI], [BASE, PROD, LITE, PKI], [BASE, PROD, LITE, DEMO, PKI]],
    ids=["pki", "pki-lite", "pki-demo"],
)
def test_the_pki_config_runs_every_literal_upstream(files):
    running = topology(files).running
    missing = {
        up.host
        for server in _servers(FRONTEND_PKI_CONF)
        for up in server.upstreams
        if up.literal and up.host not in running
    }

    assert missing == set(), files


def test_the_demo_runs_no_flower_so_the_rule_is_live():
    """Without a scaled-to-0 service the checks above would pass vacuously."""
    assert "flower" not in topology([BASE, PROD, LITE, DEMO]).running
    assert may_be_absent("flower", _every_compose_file())


# --------------------------------------------------------------------------- #
# The checker itself: must fire, must stay clean
# --------------------------------------------------------------------------- #

_FAKE: dict[str, dict] = {
    "base.yml": {
        "proxy": {"depends_on": {"api": {"condition": "service_healthy"}}},
        "api": {"depends_on": ["store"]},
        "store": {},
        "dash": {},
    },
    "demo.yml": {"dash": {"deploy": {"replicas": "${DASH_REPLICAS:-0}"}}},
}


def _check(conf: str, base_extra: dict | None = None) -> list[str]:
    fake = {**_FAKE, "base.yml": {**_FAKE["base.yml"], **(base_extra or {})}}
    return violations(parse_servers(conf), "proxy", list(fake), fake.__getitem__)


def test_checker_fires_on_a_literal_upstream_some_mode_scales_to_zero():
    found = _check("server { location /d/ { proxy_pass http://dash:80/d/; } }")

    assert found == ["server #1 upstream 'dash': literal, but some mode runs no 'dash' container"]


def test_checker_fires_on_a_literal_upstream_the_proxy_does_not_wait_for():
    found = _check("server { location /o/ { proxy_pass http://other:80; } }", {"other": {}})

    assert found == ["server #1 upstream 'other': literal, but 'proxy' does not depend on it"]


def test_checker_fires_on_a_variable_upstream_without_a_resolver():
    found = _check("server { location /d/ { set $d dash:80; proxy_pass http://$d; } }")

    assert found == ["server #1 upstream 'dash': variable proxy_pass with no server-level resolver"]


def test_checker_fires_on_a_resolver_scoped_to_another_location():
    conf = (
        "server { location /x/ { resolver 127.0.0.11; } "
        "location /d/ { set $d dash:80; proxy_pass http://$d; } }"
    )

    assert _check(conf) == [
        "server #1 upstream 'dash': variable proxy_pass with no server-level resolver"
    ]


def test_checker_fires_on_an_unknown_service_name():
    found = _check(
        "server { resolver 127.0.0.11; location / { set $d dsah:80; proxy_pass http://$d; } }"
    )

    assert found == ["server #1 upstream 'dsah': no compose service of that name"]


def test_checker_stays_clean_on_a_dependency_literal_and_a_resolved_variable():
    conf = """
    server {
        resolver 127.0.0.11 valid=10s;   # Docker DNS
        location /api/ { proxy_pass http://api:8080/api/; }
        location /s3/ { proxy_pass http://store:9000/; }
        location /d/ { set $dash_upstream dash:80; proxy_pass http://$dash_upstream; }
    }
    """

    assert _check(conf) == []
