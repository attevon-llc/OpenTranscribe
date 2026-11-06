"""``register_mock_llm_provider`` activates the mock and puts the account back as it found it.

The e2e suites that call a model (summarize, speaker ID) resolve the account's ACTIVE LLM
configuration, and the active pointer is dev data: a fixture that left the mock active, or left
its configurations behind, would change what every later run on that stack talks to. Run here
against a recording stand-in for the API so the ordering is checked without a stack.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from tests.fixtures import mock_llm

pytestmark = pytest.mark.unit

BACKEND_URL = "http://mock-llm:5199/v1"
API = "http://backend.test/api"


class _Response:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self._body = body or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _RecordingSession:
    """Just enough of ``requests.Session`` for the fixture, recording what it was asked."""

    def __init__(self, active: str | None, *, delete_raises: bool = False) -> None:
        self.cookies: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._active = active
        self._delete_raises = delete_raises
        self._created = 0

    def get(self, url: str, **_kw: Any) -> _Response:
        self.calls.append(("GET", url.removeprefix(API), None))
        return _Response({"active_configuration": {"uuid": self._active} if self._active else None})

    def post(self, url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _Response:
        self.calls.append(("POST", url.removeprefix(API), json))
        if url.endswith("/llm-settings"):
            self._created += 1
            return _Response({"uuid": f"config-{self._created}"})
        return _Response()

    def delete(self, url: str, **_kw: Any) -> _Response:
        self.calls.append(("DELETE", url.removeprefix(API), None))
        if self._delete_raises:
            raise ConnectionError("backend went away")
        return _Response()


def _fixture():
    """The fixture's generator, driven by hand so teardown can be observed."""
    return inspect.unwrap(mock_llm.register_mock_llm_provider)(BACKEND_URL)


def test_registering_creates_the_config_and_makes_it_active():
    session = _RecordingSession(active="prior")
    gen = _fixture()
    register = next(gen)

    config = register(session, API, model="mock-summary-json", name="Probe")

    assert config["uuid"] == "config-1"
    (_g, status_path, _), (_p, create_path, created), (_a, active_path, activated) = session.calls
    assert status_path == "/llm-settings/status"
    assert create_path == "/llm-settings"
    assert created is not None
    assert (created["provider"], created["model_name"], created["base_url"]) == (
        "custom",
        "mock-summary-json",
        BACKEND_URL,
    )
    assert created["name"].startswith("Probe ")
    assert (active_path, activated) == (
        "/llm-settings/set-active",
        {"configuration_id": "config-1"},
    )


def test_two_registrations_get_distinct_names_so_a_leftover_cannot_409_the_next_run():
    session = _RecordingSession(active=None)
    register = next(_fixture())

    register(session, API, model="mock-gpt")
    register(session, API, model="mock-gpt")

    names = [
        (c[2] or {})["name"] for c in session.calls if c[0] == "POST" and c[1] == "/llm-settings"
    ]
    assert len(names) == 2 and names[0] != names[1]


def test_the_prior_active_config_is_read_once_before_anything_is_changed():
    session = _RecordingSession(active="prior")
    register = next(_fixture())

    register(session, API)
    register(session, API)

    assert [c for c in session.calls if c[0] == "GET"] == [("GET", "/llm-settings/status", None)]
    assert session.calls[0][0] == "GET", "the prior state must be read before the first write"


def test_teardown_deletes_what_it_made_then_restores_the_prior_active_config():
    session = _RecordingSession(active="prior")
    gen = _fixture()
    register = next(gen)
    register(session, API)
    register(session, API)
    session.calls.clear()

    with pytest.raises(StopIteration):
        next(gen)

    assert session.calls == [
        ("DELETE", "/llm-settings/config/config-1", None),
        ("DELETE", "/llm-settings/config/config-2", None),
        ("POST", "/llm-settings/set-active", {"configuration_id": "prior"}),
    ]


def test_teardown_does_not_invent_an_active_config_when_there_was_none():
    session = _RecordingSession(active=None)
    gen = _fixture()
    register = next(gen)
    register(session, API)
    session.calls.clear()

    with pytest.raises(StopIteration):
        next(gen)

    assert session.calls == [("DELETE", "/llm-settings/config/config-1", None)]


def test_teardown_never_fails_the_test_and_still_restores_when_a_delete_fails():
    session = _RecordingSession(active="prior", delete_raises=True)
    gen = _fixture()
    register = next(gen)
    register(session, API)
    session.calls.clear()

    with pytest.raises(StopIteration):
        next(gen)

    assert session.calls[-1] == ("POST", "/llm-settings/set-active", {"configuration_id": "prior"})


def test_an_unused_fixture_touches_nothing():
    gen = _fixture()
    next(gen)

    with pytest.raises(StopIteration):
        next(gen)
