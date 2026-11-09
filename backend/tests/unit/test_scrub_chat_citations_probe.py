"""``scrub_chat_citations`` rewrites history only when a citation names the file (#1337, D2).

The UPDATE scanned every ``chat_message`` and matched nothing on 54 of 54 profiled calls
(325 ms mean). A GIN-backed containment probe now runs first; the UPDATE fires only on a hit.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy import text

from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.services.file_cleanup_service import scrub_chat_citations
from tests.query_budget import count_statements

pytestmark = pytest.mark.unit

_UPDATE = r"^\s*UPDATE chat_message"


def _message(db, owner, citations):
    conv = ChatConversation(user_id=owner.id, title="probe")
    db.add(conv)
    db.flush()
    msg = ChatMessage(conversation_id=conv.id, role="assistant", content="x", citations=citations)
    db.add(msg)
    db.flush()
    return msg


def test_no_update_when_no_citation_names_the_file(db_session, normal_user):
    _message(db_session, normal_user, [{"file_uuid": str(uuid_pkg.uuid4()), "snippet": "other"}])
    with count_statements(db_session.connection()) as log:
        rewritten = scrub_chat_citations(db_session, [str(uuid_pkg.uuid4())])
    assert rewritten == 0
    assert log.matching(_UPDATE) == [], log.describe()
    assert log.matching(r"^\s*SELECT chat_message.id"), "control: the probe did run"


def test_hit_rewrites_only_the_matching_citation(db_session, normal_user):
    target = str(uuid_pkg.uuid4())
    msg = _message(
        db_session,
        normal_user,
        [{"file_uuid": target, "snippet": "q"}, {"file_uuid": str(uuid_pkg.uuid4())}, {"s": 1}],
    )
    assert scrub_chat_citations(db_session, [target]) == 1
    db_session.refresh(msg)
    assert target not in str(msg.citations) and len(msg.citations) == 2


def test_probe_is_answered_by_the_gin_index(db_session):
    from app.db.chat_indexes import CITATIONS_GIN

    conn = db_session.connection()
    conn.execute(text(CITATIONS_GIN.create_sql().replace(" CONCURRENTLY", "")))
    conn.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        r[0]
        for r in conn.execute(
            text(
                "EXPLAIN SELECT 1 FROM chat_message WHERE citations @> "
                'CAST(\'[{"file_uuid": "x"}]\' AS jsonb) LIMIT 1'
            )
        )
    )
    assert CITATIONS_GIN.name in plan, plan
