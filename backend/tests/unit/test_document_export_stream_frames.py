"""What the signed-job stream tells the browser about a DOCX/PDF export (issue #1278).

A document export can be refused in milliseconds (a pending redaction scan), long before the
browser has opened the stream, so a failure is cached like a result and a late subscriber must
still be told. The error frame keeps ``code`` one fixed literal slug (the SSE contract guard
pins that) and carries the reason as a separate field, with OUR sentence rather than the
worker's text.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.api.endpoints.files import subtitles
from app.models.user import User
from app.services.download_events import DOCUMENT_EXPORT_ERRORS

pytestmark = pytest.mark.unit


def _data(frame: str) -> dict:
    payload: dict = json.loads(frame.split("data: ", 1)[1])
    return payload


@pytest.mark.parametrize("reason", sorted(DOCUMENT_EXPORT_ERRORS))
def test_a_known_reason_gets_its_fixed_sentence_and_the_shared_code(reason: str) -> None:
    frame = subtitles._export_error_frame({"reason": reason, "message": "worker free text"})
    assert frame.startswith("event: error\n")
    assert _data(frame) == {
        "code": "export_failed",
        "reason": reason,
        "message": DOCUMENT_EXPORT_ERRORS[reason],
    }


def test_an_unknown_reason_is_never_echoed() -> None:
    frame = subtitles._export_error_frame({"reason": "<script>", "message": "boom"})
    assert "reason" not in _data(frame)
    assert "<script>" not in frame


def test_the_bulk_zip_error_keeps_its_original_shape() -> None:
    """CONTROL: the older worker publishes no reason and its frame must not change."""
    frame = subtitles._export_error_frame({"message": "No files could be exported."})
    assert _data(frame) == {"code": "export_failed", "message": "No files could be exported."}


RESULT: dict[str, Any] = {"url": "https://storage.test/x.docx", "filename": "x.docx", "exported": 1}


@pytest.fixture
def hidden(monkeypatch):
    """What the replay's quarantine re-check answers, recorded with its arguments."""
    answer: dict[str, Any] = {"hidden": False, "calls": []}

    def _check(file_ids, requester_id):
        answer["calls"].append((list(file_ids), requester_id))
        return answer["hidden"]

    monkeypatch.setattr("app.services.media_revocation_cache.hidden_from_requester", _check)
    return answer


def test_a_result_is_still_a_ready_frame(hidden) -> None:
    frame = subtitles._cached_outcome_frame(json.dumps({**RESULT, "file_ids": [7]}), 1278)
    assert frame.startswith("event: ready\n")
    assert _data(frame) == RESULT  # the file ids are not sent to the browser
    assert hidden["calls"] == [([7], 1278)]


def test_a_result_holding_a_file_taken_down_since_is_not_replayed(hidden) -> None:
    hidden["hidden"] = True
    frame = subtitles._cached_outcome_frame(json.dumps({**RESULT, "file_ids": [7]}), 1278)
    assert frame.startswith("event: error\n")
    assert _data(frame)["reason"] == "unavailable"
    assert RESULT["url"] not in frame


def test_a_result_that_does_not_name_its_files_is_not_replayed(hidden) -> None:
    frame = subtitles._cached_outcome_frame(json.dumps(RESULT), 1278)
    assert frame.startswith("event: error\n")
    assert RESULT["url"] not in frame
    assert hidden["calls"] == []


class _CachedRedis:
    def __init__(self, cached: dict) -> None:
        self._cached = cached

    async def get(self, key: str) -> str:
        return json.dumps(self._cached)

    async def close(self) -> None:
        return None


async def _first_frame(response) -> str:
    async for frame in response.body_iterator:
        text: str = frame
        return text
    raise AssertionError("the stream closed without emitting a frame")


def test_a_cached_failure_reaches_a_subscriber_that_connects_late(monkeypatch) -> None:
    cached = {"status": "error", "reason": "redaction_pending", "message": "ignored"}
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **kw: _CachedRedis(cached))
    owner = User(id=1278)
    job = subtitles.new_bulk_job_id(owner.id)

    frame = asyncio.run(_first_frame(subtitles.bulk_export_stream(job=job, current_user=owner)))

    assert frame.startswith("event: error\n"), frame
    assert _data(frame)["reason"] == "redaction_pending"
