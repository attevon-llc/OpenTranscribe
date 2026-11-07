"""The governance audit outbox (v459, issue #1329) against the real database.

* The event row commits and rolls back WITH the change it describes (a comment on a held
  file, a hold).
* A sink outage leaves rows pending with a backoff; a later relay delivers each exactly once,
  including after a crash between the sink write and the ``sent`` mark (``op_type=create``).
* A rejected document stops being retried; stuck rows raise ``audit_outbox_stuck_events``.

The sink is the real ``AuditLogger.deliver`` with an in-memory OpenSearch stand-in that honours
``op_type=create`` the way OpenSearch does (409 on an existing id). Relay passes run on their own
sessions bound to the test's connection (``join_transaction_mode="create_savepoint"``), so their
commits stay inside the test transaction and are rolled back with it. Every relay pass is scoped
to the rows the test created (``only=``), so rows another test committed cannot interfere.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import MagicMock

import pytest
from opensearchpy.exceptions import ConflictError
from opensearchpy.exceptions import RequestError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.core.config import settings
from app.models.media import Comment
from app.services import audit_outbox
from app.services import content_hold_service as holds
from app.services.content_audit import AuditedResource
from app.services.content_audit import stage_content_event

REASON = "Preserve for the pending matter"


class FakeOpenSearch:
    """Index writes keyed by (index, id); ``create`` refuses an existing id with a 409."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}
        self.writes = 0
        self.failure: Exception | None = None
        self.indices = SimpleNamespace(
            exists=lambda index: True,
            create=lambda **kw: None,
            put_mapping=lambda **kw: None,
        )

    def index(self, *, index: str, body: dict, op_type: str | None = None, **kw: Any):
        if self.failure is not None:
            raise self.failure
        doc_id = str(kw["id"])
        self.writes += 1
        if op_type == "create" and (index, doc_id) in self.docs:
            raise ConflictError(409, "version_conflict_engine_exception", {})
        self.docs[(index, doc_id)] = body


@pytest.fixture
def sink(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIT_LOG_TO_OPENSEARCH", True)
    monkeypatch.setattr(audit_logger, "_get_opensearch_client", lambda: fake)
    return fake


@pytest.fixture
def factory(db_session):
    conn = db_session.connection()
    return lambda: Session(bind=conn, join_transaction_mode="create_savepoint")


def _rows(db, uuids: list[str]) -> dict[str, Any]:
    rows = db.execute(
        text(
            "SELECT event_uuid::text AS u, status, attempts, last_error, sent_at, "
            "next_attempt_at > now() AS backing_off FROM audit_outbox "
            "WHERE event_uuid = ANY(CAST(:u AS uuid[]))"
        ),
        {"u": uuids},
    ).all()
    return {r.u: r for r in rows}


def _stage(db, actor, collection, n: int = 1) -> list[str]:
    uuids = []
    for i in range(n):
        stage_content_event(
            db,
            None,
            event=AuditEventType.COLLECTION_MEMBERSHIP_CHANGE,
            actor=actor,
            resource=AuditedResource.collection(collection),
            details={"added": 1, "removed": 0, "file_uuids": [], "source": f"test-{i}"},
        )
        uuids.append(db.info[audit_outbox._STAGED_KEY][-1])
    return uuids


def _make_due(db, uuids: list[str]) -> None:
    db.execute(
        text(
            "UPDATE audit_outbox SET next_attempt_at = now() - interval '1 second' "
            "WHERE event_uuid = ANY(CAST(:u AS uuid[]))"
        ),
        {"u": uuids},
    )


def _held_file(gov):
    media_file = gov.file(gov.alice)
    change = holds.place_file_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, files=[media_file], reason=REASON
    )
    gov.db.commit()
    return media_file, change


def _comment(gov, media_file) -> Comment:
    comment = Comment(media_file_id=media_file.id, user_id=gov.alice.id, text="an annotation")
    gov.db.add(comment)
    gov.db.flush()
    return comment


def _annotation_events(db, media_file) -> list[Any]:
    return list(
        db.execute(
            text(
                "SELECT status, payload FROM audit_outbox "
                "WHERE event_type = 'content.hold.annotation' "
                "AND payload->>'resource_uuid' = :f"
            ),
            {"f": str(media_file.uuid)},
        ).all()
    )


# --------------------------------------------------------------- one transaction, both or neither


def test_a_comment_on_a_held_file_and_its_annotation_event_commit_together(gov, sink):
    media_file, _ = _held_file(gov)

    comment = _comment(gov, media_file)
    staged = holds.record_hold_annotation(gov.db, None, gov.alice, media_file, str(comment.uuid))
    assert staged is True
    assert sink.writes == 0, "nothing reaches the sink before the commit"
    gov.db.commit()

    (row,) = _annotation_events(gov.db, media_file)
    assert row.status == "pending"
    assert row.payload["details"] == {
        "comment_uuid": str(comment.uuid),
        "held_by": ["organization"],
    }
    assert "an annotation" not in str(row.payload), "the comment's text is never recorded"


def test_a_rolled_back_comment_leaves_no_event(gov, sink):
    media_file, _ = _held_file(gov)

    # The test session's own rollback would undo the whole test transaction, so the comment's
    # transaction is a savepoint here; rolling it back is what a failed request does.
    attempt = gov.db.begin_nested()
    comment = _comment(gov, media_file)
    comment_id = comment.id
    holds.record_hold_annotation(gov.db, None, gov.alice, media_file, str(comment.uuid))
    attempt.rollback()

    assert _annotation_events(gov.db, media_file) == []
    assert gov.db.get(Comment, comment_id) is None


def test_a_comment_on_a_free_file_stages_nothing(gov, sink):
    media_file = gov.file(gov.alice)
    comment = _comment(gov, media_file)
    assert (
        holds.record_hold_annotation(gov.db, None, gov.alice, media_file, str(comment.uuid))
        is False
    )
    gov.db.commit()
    assert _annotation_events(gov.db, media_file) == []


def test_placing_a_hold_commits_its_event_and_a_rollback_drops_it(gov, sink):
    first_file = gov.file(gov.alice)
    attempt = gov.db.begin_nested()
    rolled_back = holds.place_file_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, files=[first_file], reason=REASON
    )
    holds.record_hold_change(gov.db, None, gov.admin, rolled_back)
    rolled_back_uuid = str(rolled_back.hold.uuid)
    attempt.rollback()

    kept = holds.place_file_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, files=[gov.file(gov.alice)], reason=REASON
    )
    holds.record_hold_change(gov.db, None, gov.admin, kept)
    gov.db.commit()

    def placed(hold_uuid: str) -> int:
        return int(
            gov.db.execute(
                text(
                    "SELECT count(*) FROM audit_outbox WHERE event_type = 'content.hold.place' "
                    "AND payload->>'resource_uuid' = :h"
                ),
                {"h": hold_uuid},
            ).scalar()
        )

    assert placed(rolled_back_uuid) == 0
    assert placed(str(kept.hold.uuid)) == 1


# ----------------------------------------------------------------------- relay, retry, exactly once


def test_a_sink_outage_leaves_rows_pending_and_a_later_relay_sends_each_once(gov, sink, factory):
    collection = gov.collection(gov.alice)
    uuids = _stage(gov.db, gov.alice, collection, n=2)
    gov.db.commit()

    sink.failure = ConnectionError("opensearch is down")
    down = audit_outbox.relay(only=uuids, session_factory=factory)
    assert (down.claimed, down.sent, down.retrying, down.deferred) == (2, 0, 1, 1)
    rows = _rows(gov.db, uuids)
    first, second = rows[uuids[0]], rows[uuids[1]]
    assert first.status == second.status == "pending"
    assert first.attempts == 1 and "opensearch is down" in first.last_error
    assert second.attempts == 0, "the row after the failure was not attempted"
    assert first.backing_off and second.backing_off

    sink.failure = None
    early = audit_outbox.relay(only=uuids, session_factory=factory)
    assert early.claimed == 0, "a row is not retried before its backoff has passed"

    _make_due(gov.db, uuids)
    up = audit_outbox.relay(only=uuids, session_factory=factory)
    assert (up.sent, up.already_in_sink) == (2, 0)
    assert {doc_id for _, doc_id in sink.docs} == set(uuids), "the sink id is the event uuid"
    assert all(r.status == "sent" and r.sent_at is not None for r in _rows(gov.db, uuids).values())

    again = audit_outbox.relay(only=uuids, session_factory=factory)
    assert again.claimed == 0
    assert len(sink.docs) == 2


def test_a_resend_after_a_crash_before_the_sent_mark_does_not_duplicate(gov, sink, factory):
    collection = gov.collection(gov.alice)
    (event_uuid,) = _stage(gov.db, gov.alice, collection)
    gov.db.commit()
    assert audit_outbox.relay(only=[event_uuid], session_factory=factory).sent == 1

    # The relay wrote the document and died before marking the row: it is pending again.
    gov.db.execute(
        text(
            "UPDATE audit_outbox SET status = 'pending', sent_at = NULL, "
            "next_attempt_at = now() - interval '1 second' WHERE event_uuid = CAST(:u AS uuid)"
        ),
        {"u": event_uuid},
    )
    resent = audit_outbox.relay(only=[event_uuid], session_factory=factory)

    assert (resent.sent, resent.already_in_sink) == (1, 1)
    assert sink.writes == 2 and len(sink.docs) == 1, "the second write was refused, not added"
    assert _rows(gov.db, [event_uuid])[event_uuid].status == "sent"


def test_the_document_goes_to_the_month_of_the_event_not_of_the_delivery(gov, sink, factory):
    collection = gov.collection(gov.alice)
    (event_uuid,) = _stage(gov.db, gov.alice, collection)
    gov.db.execute(
        text(
            "UPDATE audit_outbox SET payload = jsonb_set(payload, '{timestamp}', "
            "'\"2025-01-31T23:59:59+00:00\"') WHERE event_uuid = CAST(:u AS uuid)"
        ),
        {"u": event_uuid},
    )
    gov.db.commit()
    audit_outbox.relay(only=[event_uuid], session_factory=factory)
    assert list(sink.docs) == [("audit-logs-2025.01", event_uuid)]


def test_a_rejected_document_is_not_retried_and_counts_as_stuck(gov, sink, factory):
    collection = gov.collection(gov.alice)
    before = audit_outbox.count_stuck(gov.db)
    (event_uuid,) = _stage(gov.db, gov.alice, collection)
    gov.db.commit()

    sink.failure = RequestError(400, "mapper_parsing_exception", {})
    result = audit_outbox.relay(only=[event_uuid], session_factory=factory)

    assert result.rejected == 1
    row = _rows(gov.db, [event_uuid])[event_uuid]
    assert row.status == "rejected" and "mapper_parsing_exception" in row.last_error
    _make_due(gov.db, [event_uuid])
    assert audit_outbox.relay(only=[event_uuid], session_factory=factory).claimed == 0
    assert audit_outbox.count_stuck(gov.db) == before + 1


def test_a_row_pending_past_the_threshold_raises_the_gauge(gov, sink):
    from app.core.metrics import audit_outbox_stuck_events

    collection = gov.collection(gov.alice)
    baseline = audit_outbox.update_stuck_gauge(gov.db)
    assert baseline is not None
    (event_uuid,) = _stage(gov.db, gov.alice, collection)
    gov.db.commit()
    assert audit_outbox.update_stuck_gauge(gov.db) == baseline, "a fresh row is not stuck"

    gov.db.execute(
        text(
            "UPDATE audit_outbox SET created_at = now() - make_interval(secs => :s) "
            "WHERE event_uuid = CAST(:u AS uuid)"
        ),
        {"s": audit_outbox.AUDIT_OUTBOX_STUCK_AFTER_SECONDS + 60, "u": event_uuid},
    )
    assert audit_outbox.update_stuck_gauge(gov.db) == baseline + 1
    assert audit_outbox_stuck_events._value.get() == baseline + 1


def test_prune_deletes_only_old_sent_rows(gov, sink, factory):
    collection = gov.collection(gov.alice)
    old_sent, new_sent, old_pending = _stage(gov.db, gov.alice, collection, n=3)
    gov.db.commit()
    audit_outbox.relay(only=[old_sent, new_sent], session_factory=factory)
    gov.db.execute(
        text(
            "UPDATE audit_outbox SET sent_at = now() - interval '30 days', "
            "created_at = now() - interval '30 days' WHERE event_uuid = CAST(:a AS uuid)"
        ),
        {"a": old_sent},
    )
    gov.db.execute(
        text(
            "UPDATE audit_outbox SET created_at = now() - interval '30 days' "
            "WHERE event_uuid = CAST(:p AS uuid)"
        ),
        {"p": old_pending},
    )

    audit_outbox.prune_sent(gov.db)

    assert set(_rows(gov.db, [old_sent, new_sent, old_pending])) == {new_sent, old_pending}


# ------------------------------------------------------------------------------ no database


def test_deliver_staged_does_not_raise_a_database_failure(monkeypatch):
    """The change has committed; a relay that cannot reach the database leaves it pending."""

    def broken(**kwargs):
        raise OperationalError("UPDATE audit_outbox", {}, Exception("database gone"))

    monkeypatch.setattr(audit_outbox, "relay", broken)
    db = MagicMock()
    db.info = {audit_outbox._STAGED_KEY: ["x"]}
    audit_outbox.deliver_staged(db)
    assert audit_outbox._STAGED_KEY not in db.info


def test_retry_backoff_doubles_and_is_capped():
    assert [audit_outbox.retry_delay_seconds(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert audit_outbox.retry_delay_seconds(50) == audit_outbox.AUDIT_OUTBOX_RETRY_MAX_SECONDS


def test_an_opt_in_content_event_cannot_be_staged():
    with pytest.raises(ValueError, match="opt-in"):
        stage_content_event(
            MagicMock(),
            None,
            event=AuditEventType.CONTENT_FILE_VIEW,
            actor=cast("Any", SimpleNamespace(id=1, email="a@example.com")),
            resource=AuditedResource("media_file", "u", None, None),
        )
