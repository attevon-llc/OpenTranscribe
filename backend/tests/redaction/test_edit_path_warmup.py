"""Startup warm-up covers the detectors a segment edit uses (issue #1190).

``RedactionService.redetect_edited_segment`` runs every detector, so the first edit after
an API restart used to load ``unitary/toxic-bert`` inside the PUT: measured 12.05 s cold
against 0.02 s warm. These tests count model loads and order them against the request.
The transformers import is faked at ``sys.modules`` so no weights are touched and the
tests are identical on a CPU-only CI box.
"""

from __future__ import annotations

import sys
import threading
import types
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services.redaction import warmup
from app.services.redaction.detectors import pii_presidio
from app.services.redaction.detectors import toxicity
from app.services.redaction.service import RedactionService


class _FakePipe:
    def __call__(self, _text, **_kw):
        return [[{"label": "toxic", "score": 0.01}]]


class _FakeAnalyzer:
    def analyze(self, text: str, language: str):  # noqa: ARG002
        return []


@pytest.fixture
def loads(monkeypatch):
    """Record every toxicity model load; ``gate`` lets a test hold a load open."""
    state = SimpleNamespace(count=0, gate=None, entered=threading.Event())

    def _pipeline(*_a, **_kw):
        state.count += 1
        state.entered.set()
        if state.gate is not None:
            assert state.gate.wait(timeout=10), "test never released the load"
        return _FakePipe()

    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(pipeline=_pipeline))
    monkeypatch.setattr(toxicity, "_pipes", {})
    monkeypatch.setattr(toxicity, "_load_failed", set())
    monkeypatch.setattr(pii_presidio, "_analyzer", None)
    monkeypatch.setattr(pii_presidio, "_analyzer_gliner", None)
    monkeypatch.setattr(pii_presidio, "_load_failed", False)
    monkeypatch.setattr(pii_presidio, "_build_analyzer", lambda _g: _FakeAnalyzer())
    return state


def _gate(monkeypatch, in_use: bool) -> None:
    @contextmanager
    def _scope():
        yield None

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    monkeypatch.setattr("app.services.redaction.config.redaction_is_in_use", lambda _db: in_use)


def _edit() -> str:
    media = SimpleNamespace(language="en", user_id=1, id=1)
    segment = SimpleNamespace(text="hello there", words=None, redactions=None, toxicity=None)
    return RedactionService.redetect_edited_segment(None, media, segment)  # type: ignore[arg-type]


def test_the_first_segment_edit_after_startup_loads_no_model(monkeypatch, loads):
    """Fails on the old code: the edit itself performed the (12 s) toxicity load."""
    _gate(monkeypatch, in_use=True)

    warmup._warm_if_in_use()
    loaded_at_startup = loads.count
    result = _edit()

    assert loaded_at_startup == 1, "startup did not warm the toxicity model"
    assert loads.count == loaded_at_startup, "the first edit loaded a model inline"
    assert result == "done"


def test_a_deployment_with_redaction_disabled_loads_nothing_at_startup(monkeypatch, loads):
    """Control: the new warm-up sits behind the same gate as the Presidio one."""
    _gate(monkeypatch, in_use=False)

    warmup._warm_if_in_use()

    assert loads.count == 0
    assert pii_presidio._analyzer is None


def test_two_concurrent_callers_load_the_toxicity_model_once(loads):
    """A request arriving mid-warm-up must wait, not start a second ~500 MB load."""
    loads.gate = threading.Event()
    results: list[object] = []
    first = threading.Thread(target=lambda: results.append(toxicity.preload()))
    first.start()
    assert loads.entered.wait(timeout=5)
    second = threading.Thread(
        target=lambda: results.append(toxicity._get_pipe(toxicity._model_for_language("en")))
    )
    second.start()
    second.join(timeout=0.3)
    assert second.is_alive(), "the second caller did not wait for the in-flight load"
    loads.gate.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert loads.count == 1, "the model was loaded more than once"
    assert len(results) == 2


def test_a_missing_presidio_does_not_leave_toxicity_cold(monkeypatch, loads):
    """The two warm-ups are independent: one optional dependency absent leaves the other."""
    _gate(monkeypatch, in_use=True)

    def _explode(_g: bool):
        raise RuntimeError("no spaCy model on this box")

    monkeypatch.setattr(pii_presidio, "_build_analyzer", _explode)

    warmup._warm_if_in_use()

    assert loads.count == 1


def test_a_toxicity_load_failure_does_not_raise_out_of_the_warm_up(monkeypatch, loads):
    def _boom(*_a, **_kw):
        raise OSError("weights not on this box")

    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(pipeline=_boom))

    assert warmup.warm_edit_path_detectors() is False
