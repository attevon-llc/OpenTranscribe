"""One malformed stored citation must not 500 the whole conversation (scale plan 6, M2 #15)."""

from __future__ import annotations

import logging
import uuid as uuid_pkg

from fastapi import status

from app.models.chat import ROLE_ASSISTANT
from app.models.chat import ROLE_USER
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage

_GOOD = {
    "id": 2,
    "kind": "chunk",
    "file_uuid": "00000000-0000-0000-0000-000000000002",
    "title": "Standup",
    "snippet": "sprint velocity",
}
_NO_ID = {"kind": "chunk", "file_uuid": "00000000-0000-0000-0000-000000000001", "title": "x"}


def test_a_citation_without_an_id_is_dropped_and_the_list_still_answers(
    client, user_token_headers, normal_user, db_session, caplog
):
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(), user_id=normal_user.id, title="m2-15", context={}
    )
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)
    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(), conversation_id=conversation.id, role=ROLE_USER, content="q"
        )
    )
    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_ASSISTANT,
            content="a [1] [2]",
            citations=[_NO_ID, _GOOD],
        )
    )
    db_session.commit()

    with caplog.at_level(logging.WARNING, logger="app.api.endpoints.chat.messages"):
        response = client.get(
            f"/api/chat/conversations/{conversation.uuid}/messages", headers=user_token_headers
        )

    assert response.status_code == status.HTTP_200_OK, response.text
    messages = response.json()["messages"]
    assert len(messages) == 2
    assistant = next(m for m in messages if m["role"] == ROLE_ASSISTANT)
    assert [c["id"] for c in assistant["citations"]] == [2]
    assert len([r for r in caplog.records if "malformed stored citation" in r.message]) == 1
