"""The Flower wait in test_gpu_scale_smoke_live must end when the workers the
stack can have are registered, not burn every retry waiting for a gpu-scaled@
worker that a non-``--gpu-scale`` stack can never produce."""

from __future__ import annotations

import pytest

from tests.integration import test_gpu_scale_smoke_live as mod

_REPLY = {"gpu-transcription@host": {"timestamp": 1}}


class _Resp:
    def __init__(self, data: dict) -> None:
        self.ok = True
        self._data = data

    def json(self) -> dict:
        return self._data


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    rec: dict = {"get": 0, "sleep": 0}

    def fake_get(*_a: object, **_k: object) -> _Resp:
        rec["get"] += 1
        return _Resp(_REPLY)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: rec.__setitem__("sleep", rec["sleep"] + 1))
    return rec


def test_returns_immediately_when_only_default_worker_is_wanted(calls: dict) -> None:
    assert mod._flower_workers_refreshed(("gpu-transcription@",)) == _REPLY
    assert calls == {"get": 1, "sleep": 0}


def test_still_retries_full_budget_when_gpu_scaled_worker_is_wanted(calls: dict) -> None:
    mod._flower_workers_refreshed(("gpu-scaled@",))
    assert calls["get"] == mod.FLOWER_REFRESH_ATTEMPTS + 1
    assert calls["sleep"] == mod.FLOWER_REFRESH_ATTEMPTS


def test_empty_wanted_accepts_first_ok_reply(calls: dict) -> None:
    assert mod._flower_workers_refreshed(()) == _REPLY
    assert calls["get"] == 1


def test_request_error_then_good_reply_takes_two_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    seq: list[Exception | _Resp] = [mod.requests.ConnectionError("boom"), _Resp(_REPLY)]
    n = {"get": 0}

    def fake_get(*_a: object, **_k: object) -> _Resp:
        item = seq[n["get"]]
        n["get"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    assert mod._flower_workers_refreshed(("gpu-transcription@",)) == _REPLY
    assert n["get"] == 2
