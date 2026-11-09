"""The chat endpoints commit exactly as often as they did before queries moved to a service."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from app.models.chat import ChatConversation
from app.models.chat import ChatProject

PROJECTS = "/api/chat/projects"
CONVERSATIONS = "/api/chat/conversations"
SETTINGS = "/api/user-settings/chat"


@contextmanager
def _count_commits(db_session) -> Iterator[list[int]]:
    seen = [0]

    def on_commit(_session) -> None:
        seen[0] += 1

    event.listen(db_session, "after_commit", on_commit)
    try:
        yield seen
    finally:
        event.remove(db_session, "after_commit", on_commit)


def _overhead(client, headers, db_session) -> int:
    """Commits the auth layer makes for any authenticated request, measured on a read."""
    with _count_commits(db_session) as commits:
        assert client.get(PROJECTS, headers=headers).status_code == 200
    return commits[0]


def _project(db_session, owner) -> ChatProject:
    project = ChatProject(uuid=uuid.uuid4(), user_id=owner.id, name=f"p-{uuid.uuid4().hex[:6]}")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def test_delete_project_commits_once_and_detaches_its_conversations(
    client, user_token_headers, normal_user, db_session
):
    project = _project(db_session, normal_user)
    conversation = ChatConversation(
        uuid=uuid.uuid4(), user_id=normal_user.id, title="t", context={}, project_id=project.id
    )
    db_session.add(conversation)
    db_session.commit()
    overhead = _overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.delete(f"{PROJECTS}/{project.uuid}", headers=user_token_headers)
    assert response.status_code == 204
    assert commits[0] - overhead == 1
    db_session.refresh(conversation)
    assert conversation.project_id is None


def test_reset_chat_settings_commits_once(client, user_token_headers, db_session):
    assert (
        client.put(
            SETTINGS, json={"use_context_default": False}, headers=user_token_headers
        ).status_code
        == 200
    )
    overhead = _overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.delete(SETTINGS, headers=user_token_headers)
    assert response.status_code == 200
    assert commits[0] - overhead == 1


def test_save_chat_settings_commits_once(client, user_token_headers, db_session):
    overhead = _overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        response = client.put(
            SETTINGS, json={"use_context_default": False}, headers=user_token_headers
        )
    assert response.status_code == 200
    assert commits[0] - overhead == 1


def test_listing_conversations_and_projects_commits_nothing(
    client, user_token_headers, normal_user, db_session
):
    _project(db_session, normal_user)
    overhead = _overhead(client, user_token_headers, db_session)
    with _count_commits(db_session) as commits:
        assert client.get(CONVERSATIONS, headers=user_token_headers).status_code == 200
    assert commits[0] - overhead == 0
