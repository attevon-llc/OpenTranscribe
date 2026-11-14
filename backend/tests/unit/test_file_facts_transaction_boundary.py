"""``generate_file_artifacts`` holds no transaction while it builds.

Building the facts, digest and keyphrases is CPU work. It used to run between the last read
(``SELECT file_facts``) and the write, inside the caller's transaction, so the hourly backfill's
burst of 200 tasks on a loaded host held connections idle in transaction for 1-2 s. The read
transaction now ends before the build, and the write is a short one of its own.

What is asserted is the ORDER of session calls and the build, against a session double that
records them; the builder is replaced by a recorder so the test needs no transcript.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.models.file_facts import FileFacts
from app.models.media import MediaFile
from app.services.ingest_artifacts import service

ARTIFACTS = {
    "facts": {"roster": ["A"]},
    "digest": {"language": "en", "word_count": 3, "sections": []},
    "keyphrases": {"phrases": []},
}


class _Session:
    """Records session calls into ``events``; answers each model's ``first()`` from a queue."""

    def __init__(self, events: list[str], answers: dict[type, list[Any]]) -> None:
        self.events = events
        self.added: list[Any] = []
        self._answers = {model: list(rows) for model, rows in answers.items()}

    def query(self, model: type) -> MagicMock:
        self.events.append(f"query:{model.__name__}")
        chain = MagicMock()
        chain.filter.return_value.first.return_value = self._answers[model].pop(0)
        return chain

    def add(self, row: Any) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.events.append("commit")

    def flush(self) -> None:
        self.events.append("flush")


@pytest.fixture
def events(monkeypatch) -> list[str]:
    """Segments without a database, and a builder that logs where among the session calls it ran."""
    log: list[str] = []
    monkeypatch.setattr(service, "load_ordered_segments", lambda db, file_id: [{"text": "hi"}])
    monkeypatch.setattr(service, "source_fingerprint", lambda segments: "new-fingerprint")

    def build(segments, *, duration, language, recorded_at):
        assert (duration, language) == (12.5, "en")  # copied off the row before the commit
        log.append("build")
        return ARTIFACTS

    monkeypatch.setattr(service, "build_artifacts", build)
    return log


def _media() -> SimpleNamespace:
    return SimpleNamespace(duration=12.5, language="en", upload_time=None)


def _row(fingerprint: str) -> FileFacts:
    return FileFacts(
        media_file_id=7, source_fingerprint=fingerprint, generator_version=service.GENERATOR_VERSION
    )


def test_the_read_transaction_ends_before_the_build_and_the_write_is_short(events) -> None:
    db = _Session(events, {MediaFile: [_media()], FileFacts: [_row("old-fingerprint"), None]})

    row = service.generate_file_artifacts(cast(Session, db), 7)

    assert events == [
        "query:MediaFile",
        "query:FileFacts",
        "commit",  # the reads' transaction ends here
        "build",  # no transaction open
        "query:FileFacts",  # the write's own, short transaction
        "flush",
    ]
    assert row is not None and row.source_fingerprint == "new-fingerprint"
    assert db.added == [row]


def test_a_row_written_while_this_one_built_is_updated_not_duplicated(events) -> None:
    meanwhile = _row("someone-elses")
    db = _Session(events, {MediaFile: [_media()], FileFacts: [None, meanwhile]})

    row = service.generate_file_artifacts(cast(Session, db), 7)

    assert row is meanwhile
    assert db.added == []
    assert meanwhile.source_fingerprint == "new-fingerprint"
    assert meanwhile.facts == ARTIFACTS["facts"]


def test_a_current_row_is_returned_without_a_commit_or_a_build(events) -> None:
    """CONTROL: the fingerprint short-circuit stays a pure read."""
    current = _row("new-fingerprint")
    db = _Session(events, {MediaFile: [_media()], FileFacts: [current]})

    assert service.generate_file_artifacts(cast(Session, db), 7) is current
    assert events == ["query:MediaFile", "query:FileFacts"]
