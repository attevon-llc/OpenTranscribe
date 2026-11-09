"""``chat.conversation_queries`` against the real database."""

import uuid as uuid_pkg

import pytest

from app.models.chat import STATUS_SUPERSEDED
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.chat import ChatProject
from app.models.prompt import UserSetting
from app.models.user import User
from app.services.chat import conversation_queries as cq


def _user(db, label="u"):
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=label,
        hashed_password="x",
        is_active=True,
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def _conversation(db, user, title="t", **extra):
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(), user_id=user.id, title=title, context={}, **extra
    )
    db.add(conversation)
    db.flush()
    return conversation


def _message(db, conversation, role, content="x", **extra):
    message = ChatMessage(
        uuid=uuid_pkg.uuid4(),
        conversation_id=conversation.id,
        role=role,
        content=content,
        **extra,
    )
    db.add(message)
    db.flush()
    return message


@pytest.fixture
def thread(db_session):
    user = _user(db_session)
    conversation = _conversation(db_session, user)
    first = _message(db_session, conversation, "user", "q1")
    answer = _message(db_session, conversation, "assistant", "a1")
    second = _message(db_session, conversation, "user", "q2")
    return user, conversation, [first, answer, second]


def test_find_user_conversation_is_scoped_to_the_owner(db_session, thread):
    user, conversation, _ = thread
    stranger = _user(db_session, "stranger")
    assert cq.find_user_conversation(db_session, str(conversation.uuid), user.id) == conversation
    assert cq.find_user_conversation(db_session, str(conversation.uuid), stranger.id) is None


def test_list_conversation_page_filters_tenant_archive_and_title(db_session):
    user = _user(db_session)
    wanted = _conversation(db_session, user, "Budget review")
    _conversation(db_session, user, "Other topic")
    _conversation(db_session, user, "Budget archived", is_archived=True)
    rows, total = cq.list_conversation_page(
        db_session,
        user_id=user.id,
        organization_id=None,
        archived=False,
        title_filter="budget",
        limit=10,
        offset=0,
    )
    assert [r.id for r in rows] == [wanted.id]
    assert total == 1
    rows, total = cq.list_conversation_page(
        db_session,
        user_id=user.id,
        organization_id=987654,
        archived=False,
        title_filter=None,
        limit=10,
        offset=0,
    )
    assert (rows, total) == ([], 0)


def test_list_conversation_page_pages_and_reports_unpaged_total(db_session):
    user = _user(db_session)
    made = [_conversation(db_session, user, f"c{i}") for i in range(3)]
    rows, total = cq.list_conversation_page(
        db_session,
        user_id=user.id,
        organization_id=None,
        archived=False,
        title_filter=None,
        limit=2,
        offset=0,
    )
    assert total == 3
    assert len(rows) == 2
    assert {r.id for r in rows} <= {c.id for c in made}


def test_message_counts_and_page(db_session, thread):
    _, conversation, messages = thread
    assert cq.message_counts(db_session, []) == {}
    assert cq.message_counts(db_session, [conversation.id]) == {conversation.id: 3}
    rows, total = cq.message_page(db_session, conversation.id, offset=1, limit=1)
    assert total == 3
    assert [r.id for r in rows] == [messages[1].id]


def test_supersede_helpers_hide_messages_from_visible_and_history(db_session, thread):
    _, conversation, (first, answer, second) = thread
    last = cq.last_user_message(db_session, conversation.id)
    assert last is not None
    assert last.id == second.id
    assert cq.newest_message_id(db_session, conversation.id) == second.id
    cq.supersede_assistant_replies_after(db_session, conversation.id, first.id)
    db_session.refresh(answer)
    assert answer.status == STATUS_SUPERSEDED
    assert [m.id for m in cq.visible_messages(db_session, conversation.id)] == [
        first.id,
        second.id,
    ]
    cq.supersede_message_range(db_session, conversation.id, first_id=second.id, last_id=second.id)
    db_session.refresh(second)
    assert second.status == STATUS_SUPERSEDED
    assert [m.id for m in cq.recent_history(db_session, conversation.id, 5)] == [first.id]


def test_find_user_message_requires_user_role(db_session, thread):
    _, conversation, (first, answer, _) = thread
    found = cq.find_user_message(db_session, conversation.id, str(first.uuid))
    assert found is not None
    assert found.id == first.id
    assert cq.find_user_message(db_session, conversation.id, str(answer.uuid)) is None


def test_message_owned_in_tenant_checks_owner_and_tenant(db_session, thread):
    user, _, (first, _, _) = thread
    stranger = _user(db_session, "stranger")
    assert cq.message_owned_in_tenant(db_session, first.uuid, user.id, None)
    assert not cq.message_owned_in_tenant(db_session, first.uuid, stranger.id, None)
    assert not cq.message_owned_in_tenant(db_session, first.uuid, user.id, 987654)


def test_projects_count_list_and_detach(db_session):
    user = _user(db_session)
    live = ChatProject(uuid=uuid_pkg.uuid4(), user_id=user.id, name="Bravo")
    archived = ChatProject(uuid=uuid_pkg.uuid4(), user_id=user.id, name="alpha", is_archived=True)
    db_session.add_all([live, archived])
    db_session.flush()
    conversation = _conversation(db_session, user, project_id=live.id)

    assert cq.find_user_project(db_session, str(live.uuid), user.id) == live
    assert cq.find_user_project(db_session, str(live.uuid), user.id + 10_000_000) is None
    assert cq.conversation_counts(db_session, []) == {}
    assert cq.conversation_counts(db_session, [live.id]) == {live.id: 1}
    assert [p.name for p in cq.list_user_projects(db_session, user.id, include_archived=True)] == [
        "alpha",
        "Bravo",
    ]
    assert [p.name for p in cq.list_user_projects(db_session, user.id, include_archived=False)] == [
        "Bravo"
    ]
    cq.detach_conversations(db_session, live.id)
    db_session.refresh(conversation)
    assert conversation.project_id is None


def test_settings_rows_and_delete_touch_only_the_requested_keys(db_session):
    user = _user(db_session)
    db_session.add_all(
        [
            UserSetting(user_id=user.id, setting_key="chat_a", setting_value="1"),
            UserSetting(user_id=user.id, setting_key="other", setting_value="2"),
        ]
    )
    db_session.flush()
    assert [r.setting_key for r in cq.setting_rows(db_session, user.id, ["chat_a"])] == ["chat_a"]
    cq.delete_settings(db_session, user.id, ["chat_a"])
    remaining = db_session.query(UserSetting).filter(UserSetting.user_id == user.id).all()
    assert [r.setting_key for r in remaining] == ["other"]
