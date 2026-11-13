"""``register_mock_llm_provider`` serializes tests that flip the account's active LLM config.

The active pointer is one global per user. Without a cross-process lock two xdist workers
overwrite each other and restore to a mock the other worker owns.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from tests.fixtures import mock_llm
from tests.fixtures.mock_llm import mock_llm_registrar

#: A worker that is NOT blocked finishes in milliseconds; this is the "still waiting" window.
BLOCKED_WINDOW_S = 0.5
FINISH_WAIT_S = 15


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _Session:
    """Just enough of ``requests.Session`` for the fixture, recording the calls it gets."""

    def __init__(self, active: str | None) -> None:
        self.cookies = {"csrf_token": "t"}
        self._active = active
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **_kw: Any) -> _Response:
        self.calls.append(("GET", url))
        return _Response({"active_configuration": {"uuid": self._active} if self._active else None})

    def post(self, url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _Response:
        self.calls.append(("POST", url))
        return _Response({"uuid": f"cfg-{len(self.calls)}"})

    def delete(self, url: str, **_kw: Any) -> _Response:
        self.calls.append(("DELETE", url))
        return _Response({})


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    monkeypatch.setattr(mock_llm, "ACTIVE_LLM_CONFIG_LOCK", tmp_path / "active.lock", raising=False)


def _fixture_instance():
    return mock_llm_registrar("http://mock:5199/v1")


@pytest.mark.usefixtures("lock_path")
def test_a_second_registration_waits_for_the_first_to_restore():
    first_gen = _fixture_instance()
    first_register = next(first_gen)
    first_register(_Session("real-config"), "http://api", model="mock-speakers-json")

    second_session = _Session("cfg-1")  # what the second worker would see: the first's mock
    done = threading.Event()

    def second_worker() -> None:
        second_gen = _fixture_instance()
        second_register = next(second_gen)
        second_register(second_session, "http://api", model="mock-gpt")
        done.set()
        for _ in second_gen:
            pass

    thread = threading.Thread(target=second_worker, daemon=True)
    thread.start()

    assert not done.wait(BLOCKED_WINDOW_S), "second registration did not wait for the first"
    assert second_session.calls == [], "second worker touched the API while the lock was held"

    for _ in first_gen:
        pass

    assert done.wait(FINISH_WAIT_S), "second registration never got the lock after release"
    thread.join(FINISH_WAIT_S)


@pytest.mark.usefixtures("lock_path")
def test_the_lock_is_released_when_the_test_body_raised():
    gen = _fixture_instance()
    register = next(gen)
    register(_Session(None), "http://api")
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("test body failed"))

    again = _fixture_instance()
    next(again)(_Session(None), "http://api")  # would time out if the lock leaked
    for _ in again:
        pass
