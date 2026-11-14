"""What a segment text edit does on the request thread, counted, not timed.

LOADRUN3 measured ``PUT .../transcript/segments/{uuid}`` at p95 1,000 ms. The edit committed
twice (the redaction stale mark committed on its own, then the segment), published the
full-file re-detection BETWEEN the two commits, and queued a new full-file scan on every
edit, so an edit-and-undo pair queued two. These tests drive the real endpoint function with
a recording session and assert the shape: one commit, every publish after it, and one
debounced scan per burst.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.core import redaction_defaults
from app.schemas.transcript import TranscriptSegmentUpdate
from app.services.redaction import detectors
from app.services.redaction.service import RedactionService


class _RecordingSession:
    """A session that only records commits and the row-locked status re-read."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def commit(self) -> None:
        self.log.append("commit")

    def rollback(self) -> None:
        self.log.append("rollback")

    def refresh(self, _obj: Any, attribute_names: Any = None, with_for_update: Any = None):
        self.log.append("lock" if with_for_update else "refresh")


@pytest.fixture
def edit(monkeypatch):
    """Run one edit against a file in ``status``; returns ``(log, media)``."""
    from app.api import hold_refusal
    from app.api.endpoints.files import crud
    from app.services.search import reindex_dispatch
    from app.tasks import analytics_dispatch
    from app.tasks import redaction_task

    log: list[str] = []

    def _publish(name: str):
        def _record(*_a, **kw):
            log.append(f"publish:{name}:countdown={kw.get('countdown')}")

        return _record

    monkeypatch.setattr(redaction_task.redaction_detect_task, "apply_async", _publish("scan"))
    monkeypatch.setattr(redaction_task.redaction_detect_task, "delay", _publish("scan"))
    monkeypatch.setattr(
        reindex_dispatch, "dispatch_transcript_reindex", lambda **_kw: log.append("reindex")
    )
    monkeypatch.setattr(
        analytics_dispatch, "dispatch_analytics", lambda _u: log.append("analytics")
    )
    monkeypatch.setattr(hold_refusal, "refuse_if_frozen", lambda *_a, **_kw: None)
    monkeypatch.setattr(detectors, "_request_warmup", lambda: None)

    def _cold_detectors(_text, _words, _cfg, failures=None):
        # The API process has no detector resident: the real loader refuses, as here.
        detectors.refuse_model_load("pii")
        if failures is not None:
            failures.append("pii")
        return [], None

    def _run(status: str | None, *, cold: bool = True):
        if cold:
            monkeypatch.setattr(
                RedactionService, "detect_segment_spans", staticmethod(_cold_detectors)
            )
        else:
            monkeypatch.setattr(
                RedactionService, "detect_segment_spans", staticmethod(lambda *_a, **_k: ([], None))
            )
        media = SimpleNamespace(
            id=11, uuid=uuid.uuid4(), user_id=5, language="en", redaction_status=status
        )
        segment = SimpleNamespace(
            uuid=uuid.uuid4(),
            text="old words",
            words=None,
            start_time=0.0,
            end_time=1.0,
            speaker=None,
            redactions=None,
            toxicity=None,
        )
        monkeypatch.setattr(crud, "get_media_file_by_uuid", lambda *_a, **_kw: media)
        monkeypatch.setattr(crud, "get_segment_with_speaker", lambda *_a: segment)
        crud.update_single_transcript_segment(
            cast(Session, _RecordingSession(log)),
            str(media.uuid),
            str(segment.uuid),
            TranscriptSegmentUpdate(text="new words"),
            cast(Any, SimpleNamespace(id=5)),
        )
        return log, media

    return _run


def test_an_unexaminable_edit_commits_once_and_publishes_only_after_it(edit):
    log, media = edit(redaction_defaults.REDACTION_STATUS_DONE)

    assert log.count("commit") == 1, f"the edit and its stale mark commit together: {log}"
    commit_at = log.index("commit")
    published = [
        i for i, e in enumerate(log) if e.startswith("publish") or e in ("reindex", "analytics")
    ]
    assert published and all(i > commit_at for i in published), (
        f"a task published before the commit can read the pre-edit rows: {log}"
    )
    assert log.index("lock") < commit_at, "the transition is decided under the row lock"
    assert media.redaction_status == redaction_defaults.REDACTION_STATUS_PENDING


def test_the_rescan_is_debounced_not_immediate(edit):
    log, _media = edit(redaction_defaults.REDACTION_STATUS_DONE)

    scans = [e for e in log if e.startswith("publish:scan")]
    assert scans == [
        f"publish:scan:countdown={redaction_defaults.REDACTION_EDIT_RESCAN_DELAY_SECONDS}"
    ]


@pytest.mark.parametrize(
    "status",
    [redaction_defaults.REDACTION_STATUS_PENDING, redaction_defaults.REDACTION_STATUS_PROCESSING],
)
def test_an_edit_while_a_scan_is_coming_rides_it(edit, status):
    """The second PUT of an edit-and-undo pair, or any edit during a scan, queues nothing.

    ``pending``: the queued scan has not read the text yet. ``processing``: the running scan
    loses its terminal write to this edit's ``pending`` and re-queues itself.
    """
    log, media = edit(status)

    assert not [e for e in log if e.startswith("publish:scan")], log
    assert log.count("commit") == 1
    assert media.redaction_status == redaction_defaults.REDACTION_STATUS_PENDING


def test_a_trusted_edit_during_a_running_scan_still_supersedes_it(edit):
    """Detectors resident and clean: the spans are fresh, but a running scan read the old
    text and would overwrite them under ``done``. Re-marking ``pending`` makes it re-scan."""
    log, media = edit(redaction_defaults.REDACTION_STATUS_PROCESSING, cold=False)

    assert media.redaction_status == redaction_defaults.REDACTION_STATUS_PENDING
    assert not [e for e in log if e.startswith("publish:scan")]


def test_a_trusted_edit_on_a_scanned_file_changes_no_status(edit):
    """Control: the ordinary warm path queues no scan and leaves ``done`` alone."""
    log, media = edit(redaction_defaults.REDACTION_STATUS_DONE, cold=False)

    assert media.redaction_status == redaction_defaults.REDACTION_STATUS_DONE
    assert not [e for e in log if e.startswith("publish:scan")]
    assert log.count("commit") == 1
