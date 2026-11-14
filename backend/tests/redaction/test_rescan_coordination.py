"""A redaction scan that an edit superseded mid-run must not mark the file ``done``.

``detect_and_store`` reads every segment, scans for seconds, then writes its terminal status.
An edit that commits in between leaves the scan holding spans for the OLD text. Writing
``done`` unconditionally cached those spans as the truth for the edited segment: every later
read took the cached path and masked the old offsets, not the new text. The terminal write
is now conditional on the file still being ``processing``; a lost write leaves the edit's
``pending`` and queues one more scan. Counted with a recording session, no database needed.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core import redaction_defaults
from app.services.redaction import rescan_coordination
from app.services.redaction.service import RedactionService


class _RecordingSession:
    """Records statements and commits; ``rows_won`` is what the conditional UPDATE matches."""

    def __init__(self, media: Any = None, rows_won: int = 1) -> None:
        self.media = media
        self.rows_won = rows_won
        self.statements: list[str] = []
        self.commits = 0

    def query(self, *_entities):
        return _Query(self.media)

    def execute(self, statement):
        compiled = statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
        self.statements.append(str(compiled))
        return SimpleNamespace(rowcount=self.rows_won)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


class _Query:
    def __init__(self, media: Any) -> None:
        self._media = media

    def filter(self, *_a):
        return self

    def order_by(self, *_a):
        return self

    def first(self):
        return self._media

    def all(self):
        return []


@pytest.fixture
def queued(monkeypatch):
    from app.tasks import redaction_task

    calls: list[dict] = []

    def _apply_async(kwargs=None, **options):
        calls.append({"kwargs": kwargs, **options})

    monkeypatch.setattr(redaction_task.redaction_detect_task, "apply_async", _apply_async)
    return calls


def test_the_terminal_write_only_lands_while_the_scan_still_owns_the_file():
    session = _RecordingSession(rows_won=1)

    won = rescan_coordination.finish_scan(
        cast(Session, session), 7, {"redaction_status": redaction_defaults.REDACTION_STATUS_DONE}
    )

    assert won is True
    [statement] = session.statements
    assert "SET redaction_status='done'" in statement
    assert "media_file.redaction_status = 'processing'" in statement.split("WHERE", 1)[1]
    assert session.commits == 1


def test_a_lost_terminal_write_reports_supersession_and_still_commits_the_spans():
    session = _RecordingSession(rows_won=0)

    won = rescan_coordination.finish_scan(
        cast(Session, session), 7, {"redaction_status": redaction_defaults.REDACTION_STATUS_DONE}
    )

    assert won is False
    assert session.commits == 1, "the segment spans flush in the same commit either way"


def test_the_rescan_is_queued_with_the_debounce_countdown(queued):
    assert RedactionService.queue_rescan(7, 3) is True
    assert queued == [
        {
            "kwargs": {"file_id": 7, "user_id": 3},
            "countdown": redaction_defaults.REDACTION_EDIT_RESCAN_DELAY_SECONDS,
        }
    ]


def test_a_failed_publish_never_raises(monkeypatch):
    from app.tasks import redaction_task

    def _boom(**_kw):
        raise ConnectionError("broker down")

    monkeypatch.setattr(redaction_task.redaction_detect_task, "apply_async", _boom)
    assert RedactionService.queue_rescan(7, 3) is False


def _scan_stand_ins(monkeypatch) -> None:
    """Everything detect_and_store reaches besides the session, made inert."""
    from app.services import system_settings_service
    from app.services.redaction import device

    monkeypatch.setattr(system_settings_service, "get_setting_bool", lambda *_a: False)
    monkeypatch.setattr(
        RedactionService, "_owner_wants_llm_or_defer", staticmethod(lambda *_a: False)
    )
    monkeypatch.setattr(device, "inference_guard", nullcontext)


@pytest.mark.parametrize("rows_won", [1, 0], ids=["no-edit-during-scan", "edit-during-scan"])
def test_a_scan_superseded_by_an_edit_stays_pending_and_rescans(monkeypatch, queued, rows_won):
    """The edit committed ``pending`` while the scan ran: the scan must not write ``done``.

    The control (``rows_won=1``) proves the same scan does finish ``done`` when nothing
    interrupted it, so the superseded case is not a scan that never finishes.
    """
    _scan_stand_ins(monkeypatch)
    media = SimpleNamespace(id=7, user_id=3, language=None, redaction_status=None)
    session = _RecordingSession(media, rows_won=rows_won)

    result = RedactionService.detect_and_store(cast(Session, session), 7)

    if rows_won:
        assert result["status"] == redaction_defaults.REDACTION_STATUS_DONE
        assert queued == []
    else:
        assert result["status"] == redaction_defaults.REDACTION_STATUS_PENDING
        assert result["reason"] == "superseded"
        assert [c["kwargs"] for c in queued] == [{"file_id": 7, "user_id": 3}]
    assert media.redaction_status == redaction_defaults.REDACTION_STATUS_PROCESSING, (
        "the terminal status goes through the conditional UPDATE, never the ORM row"
    )
    [terminal] = session.statements
    assert "redaction_status = 'processing'" in terminal.split("WHERE", 1)[1]
