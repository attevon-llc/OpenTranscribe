"""``_history_for_prompt`` never replays citations to the LLM provider (issue #817).

The #817 investigation's original claim — that prompt history re-sends a message's
citations, and so could leak a taken-down file's transcript text to a remote
provider on every later turn — does not hold against this function: it selects
only ``role``/``content`` from each row, so a citation's ``snippet`` (which can be
raw, unmasked transcript text on a local-model deployment) never reaches the
returned list at all. This is a cheap non-regression pin, not a fix — see the
comment on ``_history_for_prompt`` in ``chat/messages.py`` for the full argument.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.api.endpoints.chat.messages import _history_for_prompt
from app.models.chat import ROLE_ASSISTANT
from app.models.chat import ROLE_USER
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage

SECRET_SNIPPET = "text that must never reach a remote provider on the next turn"


def test_history_for_prompt_carries_no_citations_key(db_session, normal_user):
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(), user_id=normal_user.id, title="history817", context={}
    )
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)

    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_USER,
            content="What happened?",
        )
    )
    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_ASSISTANT,
            content="Here is what happened [1].",
            citations=[
                {
                    "id": 1,
                    "kind": "chunk",
                    "file_uuid": str(uuid_pkg.uuid4()),
                    "title": "Some Recording",
                    "snippet": SECRET_SNIPPET,
                }
            ],
        )
    )
    db_session.commit()

    history = _history_for_prompt(db_session, conversation.id, max_turns=10)

    assert len(history) == 2
    for turn in history:
        assert set(turn.keys()) == {"role", "content"}
        assert "citations" not in turn
    assert not any(SECRET_SNIPPET in turn["content"] for turn in history)
