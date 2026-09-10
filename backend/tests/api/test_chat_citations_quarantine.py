"""Chat citation replay must not leak a taken-down file's transcript text (#817).

A citation is persisted on the assistant message at ANSWER time and neither
``GET /api/chat/conversations/{uuid}/messages`` (thread replay) nor
``GET /api/chat/conversations/{uuid}/export`` (download) re-checks it against the
file's CURRENT quarantine state. A citation's ``snippet`` is unredacted transcript
text whenever the answering model was local
(``redaction.llm_guard.is_local_provider`` — see ``chat/CLAUDE.md``'s "Chat
retrieval trap"), so a file taken down AFTER a conversation cited it stayed fully
readable — title, snippet, and a deep link back into the recording — through that
conversation's history and every export of it, forever.

The fix (``chat/citation_takedown.py``) drops the WHOLE citation entry, not just its
snippet: ``title``/``file_uuid`` are themselves part of the takedown subject. The
dangling ``[n]`` marker this can leave in the assistant's ``content`` is an accepted,
already-documented property of the citation scheme (``chat/citations.py``) and
``message.content`` itself is deliberately left untouched — it is the model's own
already-masked prose, and rewriting stored history to scrub a marker is out of scope.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest
from fastapi import status

from app.models.chat import ROLE_ASSISTANT
from app.models.chat import ROLE_USER
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.media import MediaFile

HIDDEN_SNIPPET = "the quarterly numbers nobody outside legal should be reading"
HIDDEN_TITLE = "Board Meeting — Confidential"
VISIBLE_SNIPPET = "the weekly standup covered sprint velocity"
VISIBLE_TITLE = "Team Standup"
ANSWER = "Per [1] the board discussed it, and per [2] the team also covered it."

pytestmark = pytest.mark.xdist_group("chat_citations_quarantine")


def _make_file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    defaults = dict(
        uuid=file_uuid,
        user_id=owner.id,
        filename="citation-quarantine.wav",
        storage_path=f"x/{file_uuid.hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="completed",
    )
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _conversation_with_two_citations(
    db_session, owner, *, hidden_uuid: str, visible_uuid: str
) -> ChatConversation:
    """One answer citing a quarantined file and a clean one."""
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        title="citations817",
        context={},
    )
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)

    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_USER,
            content="What happened this week?",
        )
    )
    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_ASSISTANT,
            content=ANSWER,
            citations=[
                {
                    "id": 1,
                    "kind": "chunk",
                    "file_uuid": hidden_uuid,
                    "title": HIDDEN_TITLE,
                    "chunk_index": 3,
                    "start_time": 61.0,
                    "end_time": 92.0,
                    "speaker": "Priya",
                    "snippet": HIDDEN_SNIPPET,
                    "expanded": False,
                },
                {
                    "id": 2,
                    "kind": "chunk",
                    "file_uuid": visible_uuid,
                    "title": VISIBLE_TITLE,
                    "chunk_index": 1,
                    "start_time": 5.0,
                    "end_time": 20.0,
                    "speaker": "Alex",
                    "snippet": VISIBLE_SNIPPET,
                    "expanded": False,
                },
            ],
        )
    )
    db_session.commit()
    return conversation


def _messages(client, headers, conversation):
    return client.get(f"/api/chat/conversations/{conversation.uuid}/messages", headers=headers)


def _export(client, headers, conversation, fmt: str = "markdown"):
    return client.get(
        f"/api/chat/conversations/{conversation.uuid}/export",
        headers=headers,
        params={"format": fmt},
    )


# --------------------------------------------------------------------------- #
# 1. list_messages drops the quarantined citation
# --------------------------------------------------------------------------- #


def test_list_messages_drops_a_quarantined_citation(
    client, user_token_headers, normal_user, db_session
):
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    visible = _make_file(db_session, normal_user)
    conversation = _conversation_with_two_citations(
        db_session, normal_user, hidden_uuid=str(hidden.uuid), visible_uuid=str(visible.uuid)
    )

    response = _messages(client, user_token_headers, conversation)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert HIDDEN_SNIPPET not in response.text
    assert HIDDEN_TITLE not in response.text

    payload = response.json()
    assistant = next(m for m in payload["messages"] if m["role"] == ROLE_ASSISTANT)
    assert [c["id"] for c in assistant["citations"]] == [2]


# --------------------------------------------------------------------------- #
# 2 & 3. Export (markdown, json) drops the quarantined citation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_export_drops_a_quarantined_citation(
    client, user_token_headers, normal_user, db_session, fmt
):
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    visible = _make_file(db_session, normal_user)
    conversation = _conversation_with_two_citations(
        db_session, normal_user, hidden_uuid=str(hidden.uuid), visible_uuid=str(visible.uuid)
    )

    response = _export(client, user_token_headers, conversation, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert HIDDEN_SNIPPET not in response.text
    assert HIDDEN_TITLE not in response.text
    assert VISIBLE_TITLE in response.text


def test_the_json_export_citation_array_drops_only_the_quarantined_entry(
    client, user_token_headers, normal_user, db_session
):
    """Pinned on the parsed payload: the array shrinks by exactly one entry, and
    the surviving one is untouched."""
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    visible = _make_file(db_session, normal_user)
    visible_uuid = str(visible.uuid)  # captured before the request: `export_conversation`
    # closes the (shared, in this test) db session mid-handler, which would
    # otherwise detach `visible` and raise `DetachedInstanceError` on read.
    conversation = _conversation_with_two_citations(
        db_session, normal_user, hidden_uuid=str(hidden.uuid), visible_uuid=visible_uuid
    )

    response = _export(client, user_token_headers, conversation, "json")

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = json.loads(response.text)
    citations = [c for m in payload["messages"] for c in (m["citations"] or [])]
    assert len(citations) == 1
    assert citations[0]["file_uuid"] == visible_uuid
    assert citations[0]["snippet"] == VISIBLE_SNIPPET


# --------------------------------------------------------------------------- #
# 4. Control: a clean citation still renders, on both surfaces
# --------------------------------------------------------------------------- #


def test_a_clean_citation_still_renders_in_list_messages(
    client, user_token_headers, normal_user, db_session
):
    """Pure control: neither cited file is quarantined, so both citations survive."""
    visible = _make_file(db_session, normal_user)
    other = _make_file(db_session, normal_user)
    conversation = _conversation_with_two_citations(
        db_session, normal_user, hidden_uuid=str(other.uuid), visible_uuid=str(visible.uuid)
    )

    response = _messages(client, user_token_headers, conversation)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assistant = next(m for m in payload["messages"] if m["role"] == ROLE_ASSISTANT)
    assert {c["id"] for c in assistant["citations"]} == {1, 2}
    assert VISIBLE_SNIPPET in response.text
    assert HIDDEN_SNIPPET in response.text  # not actually quarantined in this control


def test_a_clean_citation_still_renders_in_export(
    client, user_token_headers, normal_user, db_session
):
    visible = _make_file(db_session, normal_user)
    other = _make_file(db_session, normal_user)
    conversation = _conversation_with_two_citations(
        db_session, normal_user, hidden_uuid=str(other.uuid), visible_uuid=str(visible.uuid)
    )

    response = _export(client, user_token_headers, conversation)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert VISIBLE_TITLE in response.text
    assert HIDDEN_TITLE in response.text  # not quarantined in this control


# --------------------------------------------------------------------------- #
# 5. Admin bypass control
# --------------------------------------------------------------------------- #


def test_an_admin_still_sees_a_quarantined_citation(
    client, admin_token_headers, admin_user, db_session
):
    hidden = _make_file(db_session, admin_user, is_quarantined=True)
    visible = _make_file(db_session, admin_user)
    conversation = _conversation_with_two_citations(
        db_session, admin_user, hidden_uuid=str(hidden.uuid), visible_uuid=str(visible.uuid)
    )

    messages_response = _messages(client, admin_token_headers, conversation)
    assert messages_response.status_code == status.HTTP_200_OK, messages_response.text
    assert HIDDEN_SNIPPET in messages_response.text

    export_response = _export(client, admin_token_headers, conversation)
    assert export_response.status_code == status.HTTP_200_OK, export_response.text
    assert HIDDEN_TITLE in export_response.text


# --------------------------------------------------------------------------- #
# 6. message.content is never rewritten — pins the decision
# --------------------------------------------------------------------------- #


def test_message_content_is_not_rewritten_when_a_citation_is_dropped(
    client, user_token_headers, normal_user, db_session
):
    """The assistant's prose keeps its dangling ``[1]`` marker rather than being
    rewritten — rewriting stored history is explicitly out of scope."""
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    visible = _make_file(db_session, normal_user)
    conversation = _conversation_with_two_citations(
        db_session, normal_user, hidden_uuid=str(hidden.uuid), visible_uuid=str(visible.uuid)
    )

    response = _messages(client, user_token_headers, conversation)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assistant = next(m for m in payload["messages"] if m["role"] == ROLE_ASSISTANT)
    assert assistant["content"] == ANSWER
