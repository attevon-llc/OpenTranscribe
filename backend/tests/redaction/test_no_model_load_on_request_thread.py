"""A segment edit must not compute analytics or build detector models on the request thread.

Counts calls rather than timing: every loader and the analytics compute are patched to fail
the test if reached from the request path (LOADRUN2 R4).
"""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services.redaction import service as redaction_service
from app.services.redaction.detectors import DetectorUnavailableError
from app.services.redaction.detectors import no_model_load
from app.services.redaction.detectors import pii_presidio
from app.services.redaction.detectors import toxicity
from app.services.redaction.service import RedactionService


def _record(sink: list[list[str]], detectors: list[str]) -> str:
    sink.append(detectors)
    return "stale"


@pytest.fixture
def cold(monkeypatch):
    builds: list[str] = []
    monkeypatch.setattr(pii_presidio, "_analyzer", None)
    monkeypatch.setattr(pii_presidio, "_load_failed", False)
    monkeypatch.setattr(toxicity, "_pipes", {})
    monkeypatch.setattr(toxicity, "_load_failed", set())
    monkeypatch.setattr(pii_presidio, "_build_analyzer", lambda _g: builds.append("pii"))
    import sys
    import types

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        types.SimpleNamespace(pipeline=lambda *a, **k: builds.append("tox")),
    )
    return builds


def test_cold_detectors_are_refused_not_built(cold):
    failures: list[str] = []
    with no_model_load() as refused:
        RedactionService.detect_segment_spans(
            "call 555-867-5309", None, {"language": "en"}, failures=failures
        )
    assert cold == []
    assert any(r == "pii" for r in refused)
    assert any(r.startswith("toxicity:") for r in refused)
    assert {"pii", "toxicity"} <= set(failures)


def test_outside_the_guard_loading_still_happens(cold):
    with pytest.raises(DetectorUnavailableError):
        pii_presidio.detect_pii("x y", None, {})
    assert cold == ["pii"]


def test_edit_redetect_hands_cold_file_to_worker(cold, monkeypatch):
    marked: list[list[str]] = []
    monkeypatch.setattr(
        RedactionService,
        "_mark_redaction_stale",
        staticmethod(lambda _db, _mf, detectors: _record(marked, detectors)),
    )

    class _Seg:
        text = "hello there"
        words = None
        redactions = None
        toxicity = None

    class _File:
        language = "en"
        id = 1
        user_id = 1

    assert (
        RedactionService.redetect_edited_segment(
            cast(Session, None), cast(MediaFile, _File()), cast(TranscriptSegment, _Seg())
        )
        == "stale"
    )
    assert cold == []
    assert marked and "pii" in marked[0]
    assert redaction_service.no_model_load is no_model_load


def test_segment_edit_queues_analytics_instead_of_computing_it():
    import ast
    import inspect

    from app.api.endpoints.files import crud

    calls = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(ast.parse(inspect.getsource(crud)))
        if isinstance(n, ast.Call)
    }
    assert "refresh_analytics" not in calls
    assert "dispatch_analytics" in calls


# ---- every request-thread caller fails CLOSED when the detector is unavailable ----


def _cfg(categories=("pii",)):
    from app.core.redaction_config import EffectiveRedactionConfig

    return EffectiveRedactionConfig(enabled=True, enabled_categories=set(categories))


def test_chat_input_masking_drops_the_chunk_when_pii_detector_is_cold(cold):
    from app.services.chat.redactor import _mask_inline

    assert _mask_inline("call 555-867-5309", _cfg()) == ""
    assert cold == []


def test_chat_output_masking_withholds_the_span_when_pii_detector_is_cold(cold):
    from app.core import redaction_defaults
    from app.services.chat.output_redactor import OutputRedactor

    red = OutputRedactor(_cfg())
    out = red.mask("call 555-867-5309")
    assert out == redaction_defaults.REDACTION_LLM_FAILSAFE_TEXT
    assert "555" not in out
    assert cold == []


def test_snippet_masking_raises_unavailable_when_pii_detector_is_cold(cold):
    from app.services.search import snippet_redaction

    with pytest.raises(snippet_redaction.SnippetMaskingUnavailableError):
        snippet_redaction.mask_snippets(["call 555-867-5309"], _cfg())
    assert cold == []


def test_refusal_requests_a_background_warmup_rate_limited(monkeypatch):
    from app.services.redaction import detectors
    from app.services.redaction import warmup

    started: list[int] = []
    monkeypatch.setattr(warmup, "start_pii_warmup", lambda: started.append(1))
    monkeypatch.setattr(detectors, "_last_warmup_request", float("-inf"))
    for _ in range(2):
        with no_model_load():
            detectors.refuse_model_load("pii")
    assert started == [1]
    with no_model_load():
        pass  # nothing refused: no warm-up requested
    assert started == [1]
