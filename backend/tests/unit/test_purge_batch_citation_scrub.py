"""A purge batch scrubs chat citations in ONE pass, not once per destroyed file (issue #1337, V7).

``scrub_chat_citations`` scans ``chat_message`` with a JSONB expansion. Run per file it cost
O(files x messages) for retention, GDPR, trash and force-cleanup. Batch callers now pass a
shared ``defer_citation_scrub`` list and scrub once. Assertions are on statement counts that
must not grow with the batch, plus a control that the citations really were removed.
"""

import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.media import MediaFile
from app.services import file_cleanup_service as fcs
from app.services import gdpr_erasure_steps
from tests.query_budget import count_statements

_SCAN = r"jsonb_array_elements"


@pytest.fixture
def quiet_stores(monkeypatch):
    monkeypatch.setattr(fcs, "_purge_external_copies", lambda plan: [])
    monkeypatch.setattr(fcs, "_refresh_surviving_centroids", lambda db, owner_id, plan: [])


def _files(db_session, owner, n: int) -> list[MediaFile]:
    rows = [
        MediaFile(
            uuid=uuid_pkg.uuid4(),
            filename=f"batch-scrub-{uuid_pkg.uuid4().hex[:8]}.mp4",
            storage_path=f"batch-scrub/{uuid_pkg.uuid4().hex}.mp4",
            content_type="video/mp4",
            file_size=1,
            user_id=owner.id,
        )
        for _ in range(n)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return rows


def _quote(db_session, owner, files: list[MediaFile]) -> int:
    conversation = ChatConversation(user_id=owner.id, title="batch scrub")
    db_session.add(conversation)
    db_session.flush()
    message = ChatMessage(
        conversation_id=conversation.id,
        role="assistant",
        content="see [1]",
        citations=[{"file_uuid": str(f.uuid), "snippet": "quoted"} for f in files]
        + [{"snippet": "names no file"}],
    )
    db_session.add(message)
    db_session.commit()
    return int(message.id)


def _run(db_session, owner, n: int) -> tuple[Any, list[dict[str, Any]]]:
    files = _files(db_session, owner, n)
    message_id = _quote(db_session, owner, files)
    summary: dict[str, Any] = {"legal_holds_skipped": 0, "errors": [], "media_files_deleted": 0}
    with count_statements(db_session.connection()) as log:
        gdpr_erasure_steps._purge_files(db_session, files, summary)
    assert summary["media_files_deleted"] == n, summary
    db_session.expire_all()
    return log, list(db_session.get(ChatMessage, message_id).citations or [])


def test_citation_scan_count_does_not_grow_with_the_batch(db_session, normal_user, quiet_stores):
    small_log, small_left = _run(db_session, normal_user, 2)
    large_log, large_left = _run(db_session, normal_user, 7)

    assert small_left == large_left == [{"snippet": "names no file"}]  # control: really scrubbed
    assert len(small_log.matching(_SCAN)) == len(large_log.matching(_SCAN)) == 1, (
        large_log.describe()
    )  # exactly one UPDATE for the batch


def test_a_kill_between_the_deletes_and_the_scrub_is_recovered(
    db_session, normal_user, quiet_stores
):
    """Deletes committed, scrub never ran: the reconcile pass still removes the citations."""
    gone = _files(db_session, normal_user, 2)
    kept = _files(db_session, normal_user, 1)[0]
    message_id = _quote(db_session, normal_user, [*gone, kept])
    deferred: list[str] = []
    for f in gone:  # the "kill": rows deleted, the deferred scrub is never called
        assert fcs.purge_media_file(db_session, f, defer_citation_scrub=deferred)["deleted"]
    db_session.expire_all()
    assert len(db_session.get(ChatMessage, message_id).citations) == 4  # still dangling

    rewritten = fcs.scrub_orphaned_citations(db_session)
    db_session.commit()
    db_session.expire_all()

    assert rewritten == 1
    assert db_session.get(ChatMessage, message_id).citations == [
        {"file_uuid": str(kept.uuid), "snippet": "quoted"},
        {"snippet": "names no file"},
    ]
    assert fcs.scrub_orphaned_citations(db_session) == 0  # idempotent
