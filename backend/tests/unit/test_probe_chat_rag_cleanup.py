"""``scripts/probe_chat_rag.py`` puts back what it changes, even when the run dies (#1195).

The probe used to leave every conversation it created (``probe: <label>``, a title nothing
could sweep), the admin chat flags it flipped, and the user's active LLM switched to its own
config. ``main()`` is driven here with a recording stand-in for ``requests.Session`` and the
probe body replaced by one that records a conversation and then raises.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "probe_chat_rag.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_chat_rag_cleanup", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load()


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Session:
    """Answers GETs from a table; records every write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, Any]] = []
        self.active_llm = "prior-llm-uuid"

    def get(self, url: str, **_: Any) -> _Response:
        if url.endswith("/admin/chat-settings"):
            return _Response({"speaker_resolver_enabled": False})
        if url.endswith("/llm-settings"):
            return _Response({"active_configuration_id": self.active_llm, "configurations": []})
        raise AssertionError(f"unexpected GET {url}")

    def put(self, url: str, json: Any = None, **_: Any) -> _Response:
        self.writes.append(("PUT", url, json))
        return _Response({})

    def post(self, url: str, json: Any = None, **_: Any) -> _Response:
        self.writes.append(("POST", url, json))
        return _Response({})

    def delete(self, url: str, **_: Any) -> _Response:
        self.writes.append(("DELETE", url, None))
        return _Response({})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Session:
    session = _Session()
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(Session=lambda: session))
    monkeypatch.setattr(probe, "login", lambda *a, **k: None)
    monkeypatch.setattr(
        probe,
        "_resolve_questions",
        lambda args: [probe.Question(label="q1", category="c", question="?", file_uuids=["f"])],
    )

    def dies_after_one_conversation(args, sess, base_url, questions, flags, records, req):
        session.active_llm = "probe-llm-uuid"  # what ensure_llm_config does
        records.append({"conversation_uuid": "conv-1"})
        raise RuntimeError("the probe died mid-run")

    monkeypatch.setattr(probe, "_run", dies_after_one_conversation)
    return session


_ARGS = ["--question", "?", "--scope", "f", "--chat-flag", "speaker_resolver_enabled=true"]


@pytest.mark.unit
def test_a_dying_probe_deletes_its_conversations_and_restores_what_it_changed(fake) -> None:
    with pytest.raises(RuntimeError, match="died mid-run"):
        probe.main(_ARGS)
    assert any(
        m == "DELETE" and u.endswith("/chat/conversations/conv-1") for m, u, _ in fake.writes
    )
    assert any(
        m == "PUT"
        and u.endswith("/admin/chat-settings")
        and b == {"speaker_resolver_enabled": False}
        for m, u, b in fake.writes
    ), fake.writes
    assert any(
        m == "POST"
        and u.endswith("/llm-settings/set-active")
        and b == {"configuration_id": "prior-llm-uuid"}
        for m, u, b in fake.writes
    ), fake.writes
    assert any(m == "POST" and u.endswith("/auth/logout") for m, u, _ in fake.writes)


@pytest.mark.unit
def test_keep_conversations_keeps_them_but_still_restores_settings(fake) -> None:
    with pytest.raises(RuntimeError):
        probe.main([*_ARGS, "--keep-conversations"])
    assert not any(m == "DELETE" for m, _, _ in fake.writes)
    assert any(u.endswith("/admin/chat-settings") for m, u, _ in fake.writes if m == "PUT")


@pytest.mark.unit
def test_conversation_titles_carry_the_registered_probe_shape() -> None:
    assert re.fullmatch(r"[0-9a-f]{8}", probe.RUN_TAG)
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "f'probe-{RUN_TAG}: {question.label}'" in source
