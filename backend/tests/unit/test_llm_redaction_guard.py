"""Fail-closed masking for transcript text sent to LLM providers.

Chat has its own guard (``test_chat_redactor.py``). These tests cover the *other*
three paths that post transcript text to a third-party provider — summarization,
speaker identification and topic extraction — which share
``services/redaction/llm_guard.py`` and ``utils/transcript_builders.py``.

The controlling property is the same one chat enforces: when we cannot establish
that text is safe to send, we must not send it. Every test here previously passed
raw transcript text to an external API.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core import constants as C  # noqa: N812
from app.services.redaction.llm_guard import RedactionNotReadyError
from app.services.redaction.llm_guard import defer_for_redaction
from app.services.redaction.llm_guard import resolve_llm_masking
from app.utils.transcript_builders import build_full_transcript
from app.utils.transcript_builders import build_speaker_segments
from app.utils.transcript_builders import build_transcript_and_stats
from app.utils.transcript_builders import mask_segment_text

SECRET = "my ssn is 123-45-6789"


def _segment(text: str = SECRET, *, speaker: str = "Dana"):
    return SimpleNamespace(
        text=text,
        redactions=[{"category": "pii", "char_start": 10, "char_end": 21}],
        words=None,
        start_time=10.0,
        end_time=20.0,
        speaker=SimpleNamespace(name=speaker, display_name=speaker, verified=True, confidence=None),
    )


def _cfg(*, enabled=True, redact_before_llm=True):
    return SimpleNamespace(enabled=enabled, redact_before_llm=redact_before_llm)


def _media_file(*, status, user_id=7, file_id=5):
    return SimpleNamespace(id=file_id, user_id=user_id, redaction_status=status)


# --------------------------------------------------------------------------
# mask_segment_text — the fail-open that used to leak on every masking error
# --------------------------------------------------------------------------


def test_masking_error_withholds_text_instead_of_returning_it_raw():
    """The regression this module exists for.

    ``mask_segment`` raising used to return the ORIGINAL text, so a masking bug
    silently became a data leak to whatever provider the user configured.
    """
    with patch(
        "app.services.redaction.service.RedactionService.mask_segment",
        side_effect=RuntimeError("detector exploded"),
    ):
        out = mask_segment_text(_segment(), _cfg())

    assert SECRET not in out
    assert out == C.REDACTION_LLM_FAILSAFE_TEXT


def test_no_config_returns_text_unchanged():
    """No policy means no masking — the failsafe must not corrupt normal prompts."""
    assert mask_segment_text(_segment(), None) == SECRET


def test_disabled_config_returns_text_unchanged():
    assert mask_segment_text(_segment(), _cfg(enabled=False)) == SECRET


def test_successful_masking_returns_the_masked_text():
    with patch(
        "app.services.redaction.service.RedactionService.mask_segment",
        return_value=("my ssn is [PII]", []),
    ):
        assert mask_segment_text(_segment(), _cfg()) == "my ssn is [PII]"


# --------------------------------------------------------------------------
# builders — every prompt-facing path must route through the guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builder",
    [
        lambda segs, cfg: build_full_transcript(segs, cfg),
        lambda segs, cfg: build_transcript_and_stats(segs, cfg)[0],
        lambda segs, cfg: str(build_speaker_segments(segs, redaction_cfg=cfg)),
    ],
    ids=["full_transcript", "transcript_and_stats", "speaker_segments"],
)
def test_builders_never_emit_raw_text_when_masking_fails(builder):
    """All three builders feed prompts; none may fall back to the original text."""
    with patch(
        "app.services.redaction.service.RedactionService.mask_segment",
        side_effect=RuntimeError("detector exploded"),
    ):
        out = builder([_segment()], _cfg())

    assert SECRET not in out
    assert C.REDACTION_LLM_FAILSAFE_TEXT in out


def test_build_speaker_segments_masks_at_all():
    """``build_speaker_segments`` took no config, so speaker ID sent raw text[:200]."""
    with patch(
        "app.services.redaction.service.RedactionService.mask_segment",
        return_value=("my ssn is [PII]", []),
    ):
        rows = build_speaker_segments([_segment()], redaction_cfg=_cfg())

    assert rows[0]["text"] == "my ssn is [PII]"


def test_speaker_segments_mask_before_truncating():
    """Truncating first could slice a mask open and expose the tail of a span."""
    long_secret = "x" * 190 + SECRET
    with patch(
        "app.services.redaction.service.RedactionService.mask_segment",
        return_value=("x" * 190 + "my ssn is [PII]", []),
    ):
        rows = build_speaker_segments([_segment(long_secret)], redaction_cfg=_cfg())

    assert "123-45-6789" not in rows[0]["text"]
    assert len(rows[0]["text"]) == 200


def test_speaker_segments_without_config_are_unchanged():
    rows = build_speaker_segments([_segment()])
    assert rows[0]["text"] == SECRET


# --------------------------------------------------------------------------
# resolve_llm_masking — the status gate
# --------------------------------------------------------------------------


def test_policy_off_returns_none():
    with patch(
        "app.services.redaction.llm_guard.resolve_effective_config",
        return_value=_cfg(redact_before_llm=False),
    ):
        assert resolve_llm_masking(MagicMock(), _media_file(status=None)) is None


def test_redaction_disabled_returns_none():
    with patch(
        "app.services.redaction.llm_guard.resolve_effective_config",
        return_value=_cfg(enabled=False),
    ):
        assert resolve_llm_masking(MagicMock(), _media_file(status=None)) is None


def test_done_status_returns_the_config():
    cfg = _cfg()
    with patch("app.services.redaction.llm_guard.resolve_effective_config", return_value=cfg):
        got = resolve_llm_masking(MagicMock(), _media_file(status=C.REDACTION_STATUS_DONE))
    assert got is cfg


@pytest.mark.parametrize(
    "status", [None, C.REDACTION_STATUS_PENDING, C.REDACTION_STATUS_PROCESSING]
)
def test_detection_still_running_defers_rather_than_sending(status):
    """The dominant leak path.

    ``redaction_detect_task`` is dispatched alongside these tasks on a separate
    queue, so spans are routinely absent when the LLM task runs. ``mask_segment``
    with an empty span list masks NOTHING and returns the text unchanged — the
    call looks masked and isn't. Only the status gate catches this.
    """
    with patch("app.services.redaction.llm_guard.resolve_effective_config", return_value=_cfg()):
        with pytest.raises(RedactionNotReadyError) as err:
            resolve_llm_masking(MagicMock(), _media_file(status=status))

    assert err.value.retryable is True


def test_failed_detection_is_not_retryable():
    """Waiting cannot fix a failed scan, so fail loudly instead of looping."""
    with patch("app.services.redaction.llm_guard.resolve_effective_config", return_value=_cfg()):
        with pytest.raises(RedactionNotReadyError) as err:
            resolve_llm_masking(MagicMock(), _media_file(status=C.REDACTION_STATUS_FAILED))

    assert err.value.retryable is False


def test_unresolvable_policy_propagates_rather_than_defaulting_to_none():
    """ "Could not read the policy" must never be treated as "there is no policy"."""
    with patch(
        "app.services.redaction.llm_guard.resolve_effective_config",
        side_effect=RuntimeError("db down"),
    ):
        with pytest.raises(RuntimeError):
            resolve_llm_masking(MagicMock(), _media_file(status=C.REDACTION_STATUS_DONE))


# --------------------------------------------------------------------------
# defer_for_redaction
# --------------------------------------------------------------------------


def _task(retries: int = 0):
    task = MagicMock()
    task.name = "ai.generate_summary"
    task.request.retries = retries
    task.retry.side_effect = lambda **kw: RuntimeError("retry-signal")
    return task


def test_retryable_error_defers_the_task():
    task = _task()
    with pytest.raises(RuntimeError, match="retry-signal"):
        defer_for_redaction(task, RedactionNotReadyError("wait", retryable=True))

    assert task.retry.call_args.kwargs["countdown"] == 60


def test_non_retryable_error_is_reraised_without_deferring():
    task = _task()
    with pytest.raises(RedactionNotReadyError):
        defer_for_redaction(task, RedactionNotReadyError("failed", retryable=False))

    task.retry.assert_not_called()


def test_deferrals_are_bounded():
    """An unbounded wait would queue-spin forever if detection never lands."""
    task = _task(retries=C.REDACTION_LLM_MAX_DEFERRALS)
    with pytest.raises(RedactionNotReadyError):
        defer_for_redaction(task, RedactionNotReadyError("wait", retryable=True))

    task.retry.assert_not_called()


# --------------------------------------------------------------------------
# Self-healing: a scan that was never dispatched
# --------------------------------------------------------------------------


def test_null_status_marks_detection_as_never_started():
    with patch("app.services.redaction.llm_guard.resolve_effective_config", return_value=_cfg()):
        with pytest.raises(RedactionNotReadyError) as err:
            resolve_llm_masking(MagicMock(), _media_file(status=None))

    assert err.value.never_started is True
    assert err.value.file_id == 5


@pytest.mark.parametrize("status", [C.REDACTION_STATUS_PENDING, C.REDACTION_STATUS_PROCESSING])
def test_in_flight_detection_is_not_marked_never_started(status):
    """Something is already scanning — re-dispatching would duplicate the work."""
    with patch("app.services.redaction.llm_guard.resolve_effective_config", return_value=_cfg()):
        with pytest.raises(RedactionNotReadyError) as err:
            resolve_llm_masking(MagicMock(), _media_file(status=status))

    assert err.value.never_started is False


def test_unscanned_file_gets_detection_dispatched():
    """Enabling redaction after upload leaves files unscanned.

    Detection is otherwise only queued when the owner next opens the file, so
    waiting alone would burn every deferral and then fail the task.
    """
    task = _task()
    exc = RedactionNotReadyError("wait", retryable=True, file_id=5, never_started=True)
    with patch("app.tasks.redaction_task.redaction_detect_task") as detect:
        with pytest.raises(RuntimeError, match="retry-signal"):
            defer_for_redaction(task, exc)

    detect.delay.assert_called_once_with(file_id=5)


def test_detection_is_dispatched_only_on_the_first_attempt():
    """Re-dispatching every 60s would pile up duplicate CPU scans."""
    task = _task(retries=1)
    exc = RedactionNotReadyError("wait", retryable=True, file_id=5, never_started=True)
    with patch("app.tasks.redaction_task.redaction_detect_task") as detect:
        with pytest.raises(RuntimeError, match="retry-signal"):
            defer_for_redaction(task, exc)

    detect.delay.assert_not_called()


def test_dispatch_failure_still_defers():
    """Best-effort: a broker hiccup must not turn a deferral into a leak or a crash."""
    task = _task()
    exc = RedactionNotReadyError("wait", retryable=True, file_id=5, never_started=True)
    with patch("app.tasks.redaction_task.redaction_detect_task") as detect:
        detect.delay.side_effect = RuntimeError("broker down")
        with pytest.raises(RuntimeError, match="retry-signal"):
            defer_for_redaction(task, exc)


# --------------------------------------------------------------------------
# Layering: services mask, tasks decide what to do when they can't
# --------------------------------------------------------------------------


def test_topic_extraction_service_does_not_resolve_policy_itself():
    """``TopicExtractionService`` is constructed inside request handlers
    (``api/endpoints/topics.py``), so it must not raise an error whose only
    correct handler is a Celery retry. It masks with the config it is handed;
    resolving — and deferring — belongs to the task.
    """
    import ast
    import inspect

    from app.services.topic_extraction_service import TopicExtractionService

    # AST, not a substring scan — the docstring legitimately names the helper.
    tree = ast.parse(inspect.getsource(TopicExtractionService))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_llm_masking" not in called
    assert "redaction_cfg" in inspect.signature(TopicExtractionService.extract_topics).parameters


def test_topic_extraction_service_masks_with_the_supplied_config():
    from app.services.topic_extraction_service import TopicExtractionService

    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [_segment()]
    service = TopicExtractionService(db)
    media_file = cast(Any, SimpleNamespace(id=5, user_id=7))

    with patch(
        "app.services.redaction.service.RedactionService.mask_segment",
        return_value=("my ssn is [PII]", []),
    ):
        text = service._get_transcript_text(media_file, _cfg())

    assert text == "Dana: my ssn is [PII]"
