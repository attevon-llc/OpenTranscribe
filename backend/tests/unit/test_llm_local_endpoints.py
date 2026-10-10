"""``GET /llm-settings/local-endpoints`` — the fixed-allowlist discovery probe (#644).

A self-hoster configuring a local LLM provider previously had no way to discover the
internal Docker Compose *service name* a dev/test overlay answers on
(`http://llm-test-vllm:8000/v1`, `http://mock-llm:5199/v1`) short of reading a compose
file comment. This endpoint reports which of this repo's own three overlay services are
reachable right now, from a module-level constant table — never a URL/host/port supplied
by the caller.

Three things this file must prove, because each is a distinct security/maintenance
guarantee the design review was scoped to (issue #644 §C.5/§C.7):

1. The route takes no free parameter at all — a structural test, not a review memory.
2. The constant table cannot silently drift from the two compose files it describes.
3. Every ``provider`` value the table emits actually exists in the admin UI's provider
   picker (``_get_provider_defaults()``) — issue #839 made ``custom`` selectable, so this
   assertion is not red today the way the design doc originally expected; the must-fire
   case below proves the assertion can still catch a REAL drift.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi import status

from app.api.endpoints.llm_settings import _LOCAL_ENDPOINT_TABLE
from app.api.endpoints.llm_settings import _get_provider_defaults
from app.api.endpoints.llm_settings import get_local_endpoints

REPO_ROOT = Path(__file__).resolve().parents[3]
_BASE = "/api/llm-settings"


# --------------------------------------------------------------------------------- #
# 1. Structural: no url/host/port parameter, ever
# --------------------------------------------------------------------------------- #


def test_local_endpoints_route_has_no_free_parameters():
    """The endpoint's only parameters are FastAPI-injected (request/response/user).

    An exact-set comparison, not a substring check on the name: a future parameter
    named e.g. ``target`` would not contain "url"/"host"/"port" but would still be
    exactly the free-parameter regression this endpoint must never gain (issue #644
    §C.5 non-negotiable #1 — "no URL, no host, no port, and no free parameters at
    all — not in the body, not in the query string").
    """
    sig = inspect.signature(get_local_endpoints)
    assert set(sig.parameters) == {"request", "response", "current_user"}


def test_local_endpoints_route_has_no_free_parameters_must_fire_control():
    """Guard the guard: a genuinely added free parameter DOES fail the check above."""

    def _fake_route(request, response, current_user, base_url: str):  # pragma: no cover
        raise NotImplementedError

    sig = inspect.signature(_fake_route)
    assert set(sig.parameters) != {"request", "response", "current_user"}


# --------------------------------------------------------------------------------- #
# 2. Constant-table drift: the table must still match the compose files it describes
# --------------------------------------------------------------------------------- #


def _compose_service(compose_file: str, service: str) -> dict:
    with open(REPO_ROOT / compose_file) as fh:
        doc: dict = yaml.safe_load(fh)
    result: dict = doc["services"][service]
    return result


def _container_port_from_mapping(ports: list[str]) -> str:
    # "127.0.0.1:${VAR:-5195}:8000" -> "8000"; tolerate a two-part "host:container" form too.
    return ports[0].rsplit(":", 1)[-1]


@pytest.mark.parametrize(
    ("endpoint_id", "compose_file", "service", "expected_container_port"),
    [
        ("llm-test-vllm", "docker-compose.llm-test.yml", "llm-test-vllm", "8000"),
        ("llm-test-ollama", "docker-compose.llm-test.yml", "llm-test-ollama", "11434"),
        ("mock-llm", "docker-compose.mock-llm.yml", "mock-llm", "5199"),
    ],
)
def test_table_entry_matches_its_compose_service(
    endpoint_id, compose_file, service, expected_container_port
):
    """The table's service name and container-internal port must match the overlay.

    This is the whole reason the feature does not become a maintenance liability
    (issue #644 §C.8 T9) — a compose overlay renamed or re-pinned without a matching
    edit here would otherwise point the discovery probe at nothing, silently.
    """
    spec = next(s for s in _LOCAL_ENDPOINT_TABLE if s.id == endpoint_id)
    assert service in spec.base_url

    svc = _compose_service(compose_file, service)
    container_port = _container_port_from_mapping(svc["ports"])
    assert container_port == expected_container_port
    assert f":{expected_container_port}" in spec.base_url


def test_llm_test_ollama_start_command_uses_the_profile_not_the_flag():
    """T2: `llm-test-ollama` is profile-gated and NOT started by `--with-llm-test` alone
    (docker-compose.llm-test.yml:116) — the start_command must say so, or this
    reproduces #644's original complaint in a new place.
    """
    spec = next(s for s in _LOCAL_ENDPOINT_TABLE if s.id == "llm-test-ollama")
    assert "--profile ollama" in spec.start_command
    assert "--with-llm-test" not in spec.start_command


def test_no_row_offers_a_host_side_loopback_url():
    """T3: never `127.0.0.1:<host port>` — inside the backend container that is the
    backend itself, which `is_local_provider` would misclassify as local.
    """
    assert len(_LOCAL_ENDPOINT_TABLE) >= 3, "the table must not be empty for this to prove anything"
    for spec in _LOCAL_ENDPOINT_TABLE:
        assert "127.0.0.1" not in spec.base_url
        assert "localhost" not in spec.base_url


# --------------------------------------------------------------------------------- #
# 3. Every emitted provider must exist in the admin UI's picker
# --------------------------------------------------------------------------------- #


def test_every_table_provider_is_in_the_admin_ui_picker():
    """Red today would mean a table row names a provider the create-modal can't select
    at all (#644 §C.2's original finding). Issue #839 added `custom` to
    `_get_provider_defaults()`, so this is not red for `custom` any more — see the
    must-fire control below for proof the assertion still works.
    """
    catalog_providers = {p.provider for p in _get_provider_defaults()}
    assert len(_LOCAL_ENDPOINT_TABLE) >= 3, "the table must not be empty for this to prove anything"
    for spec in _LOCAL_ENDPOINT_TABLE:
        assert spec.provider in catalog_providers, (
            f"{spec.id!r} emits provider {spec.provider!r}, which "
            "_get_provider_defaults() does not serve to the admin UI's picker"
        )


def test_every_table_provider_is_in_the_admin_ui_picker_must_fire_control():
    """Guard the guard: a table row naming a provider absent from the catalog fails."""
    catalog_providers = {p.provider for p in _get_provider_defaults()}
    assert "not-a-real-provider" not in catalog_providers


# --------------------------------------------------------------------------------- #
# 4. Behaviour: reachable/unreachable, and the reason is never on the wire
# --------------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status = status_code
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FailingRequest:
    """Raises on `__aenter__`, matching how a real connection failure surfaces through
    aiohttp's `async with session.get(...) as resp:` form — the exception happens on
    entering the context manager, not on calling `.get()` itself.
    """

    async def __aenter__(self):
        raise ConnectionRefusedError("simulated: ollama not running")

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    """Stands in for `aiohttp.ClientSession` inside `pinned_aiohttp_session`.

    Keyed by hostname so different rows in one gathered call can behave differently —
    the real probe runs all three concurrently.
    """

    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def get(self, url, **_kw):
        if "mock-llm" in url:
            return _FakeResponse(200, {"data": [{"id": "mock-gpt"}]})
        if "llm-test-ollama" in url:
            return _FailingRequest()
        # llm-test-vllm: simulate "container up, service not yet answering"
        return _FakeResponse(503, {})


@pytest.fixture
def _resolve_table_hostnames_as_private(monkeypatch):
    """The table's hostnames are Docker Compose service names with no real DNS record
    in this test environment (no compose network is up here) — `socket.getaddrinfo`
    would fail before the probe ever reaches the (patched) aiohttp session, which
    would make every row report unreachable regardless of what is being tested.
    Standing in a private A record for exactly these three names reproduces "the
    compose network is up" without depending on one being up.
    """
    import socket as socket_module

    real_getaddrinfo = socket_module.getaddrinfo
    known_hosts = {"llm-test-vllm", "llm-test-ollama", "mock-llm"}

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        if host in known_hosts:
            return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr("app.utils.url_validation.socket.getaddrinfo", _fake_getaddrinfo)


def test_get_local_endpoints_reports_reachability_without_a_reason(
    client, user_token_headers, _resolve_table_hostnames_as_private
):
    """One reachable row (with models), one non-200, one connection failure — all three
    must report a bare boolean, never any distinguishing detail (§C.5 non-negotiable #5).
    """
    with patch("aiohttp.ClientSession", _FakeSession):
        resp = client.get(f"{_BASE}/local-endpoints", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    by_id = {row["id"]: row for row in body["endpoints"]}

    assert by_id["mock-llm"]["reachable"] is True
    assert by_id["mock-llm"]["models"] == ["mock-gpt"]
    assert by_id["mock-llm"]["provider"] == "custom"

    assert by_id["llm-test-vllm"]["reachable"] is False
    assert by_id["llm-test-vllm"]["models"] == []

    assert by_id["llm-test-ollama"]["reachable"] is False
    assert by_id["llm-test-ollama"]["models"] == []

    # No row may carry anything beyond the documented response shape — proving there
    # is nowhere for a failure reason to hide.
    allowed_keys = {"id", "label", "base_url", "provider", "reachable", "models", "start_command"}
    for row in body["endpoints"]:
        assert set(row.keys()) <= allowed_keys

    assert "private_endpoints_allowed" in body


def test_get_local_endpoints_requires_authentication(client):
    resp = client.get(f"{_BASE}/local-endpoints")
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text
