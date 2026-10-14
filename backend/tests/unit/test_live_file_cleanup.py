"""``delete_uploaded_file`` must not treat a refused delete as done (no live stack needed)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from tests.integration import live_file_cleanup


def _fake_delete(script: dict[str, list[int]], calls: list[str]):
    def _delete(url: str, **_kwargs):
        calls.append(url)
        key = "force" if url.endswith("/force") else "plain"
        codes = script[key]
        return SimpleNamespace(status_code=codes.pop(0) if len(codes) > 1 else codes[0], text="")

    return _delete


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace the module's `time` binding, not attributes of the real `time` module, so
    # pytest's own clock is untouched.
    clock = {"t": 0.0}
    fake_time = SimpleNamespace(
        monotonic=lambda: clock["t"],
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
    )
    monkeypatch.setattr(live_file_cleanup, "time", fake_time)


def test_a_busy_file_is_retried_then_deleted_without_force(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        requests, "delete", _fake_delete({"plain": [409, 409, 204], "force": [500]}, calls)
    )

    assert live_file_cleanup.delete_uploaded_file("http://x/api", {}, "f1") is True
    assert calls == ["http://x/api/files/f1"] * 3


def test_a_file_that_stays_busy_escalates_to_force(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(requests, "delete", _fake_delete({"plain": [409], "force": [204]}, calls))

    assert live_file_cleanup.delete_uploaded_file("http://x/api", {}, "f2") is True
    assert calls[-1] == "http://x/api/files/f2/force"
    assert calls.count("http://x/api/files/f2") > 1, "gave up on the plain delete without retrying"


def test_a_file_that_cannot_be_removed_is_reported_not_raised(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(requests, "delete", _fake_delete({"plain": [409], "force": [500]}, calls))

    with pytest.warns(UserWarning, match="f3"):
        assert live_file_cleanup.delete_uploaded_file("http://x/api", {}, "f3") is False
